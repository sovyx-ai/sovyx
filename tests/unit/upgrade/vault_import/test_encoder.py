"""Tests for ``vault_import._encoder.encode_note``.

Mock ``BrainService`` so tests exercise the encoder logic (category
inference, stub creation for forward wikilinks, tag hierarchy
expansion, relation emission) without touching SQLite.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.engine.types import ConceptCategory, ConceptId, MindId, RelationType
from sovyx.upgrade.vault_import._encoder import EncodeResult, encode_note
from sovyx.upgrade.vault_import._models import RawLink, RawNote, RawTag


def _make_note(
    *,
    title: str = "Note A",
    body: str = "body",
    path: str = "a.md",
    aliases: tuple[str, ...] = (),
    tags: tuple[RawTag, ...] = (),
    links: tuple[RawLink, ...] = (),
) -> RawNote:
    return RawNote(
        path=path,
        title=title,
        body=body,
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
        aliases=aliases,
        tags=tags,
        links=links,
    )


def _make_brain(learn_ids: list[str] | None = None) -> AsyncMock:
    """BrainService mock — ``learn_concept`` returns a unique id per call.

    Using string ids counted per ``name`` lets tests check reinforcement
    (dedup path): same name → same id across calls.
    """
    counter = iter(learn_ids or [])
    name_to_id: dict[str, str] = {}
    default_counter = iter(range(1000))

    async def _learn(*_args: object, **kwargs: object) -> ConceptId:
        name = str(kwargs.get("name", ""))
        if name not in name_to_id:
            try:
                new_id = next(counter)
            except StopIteration:
                new_id = f"cid-{next(default_counter)}"
            name_to_id[name] = new_id
        return ConceptId(name_to_id[name])

    brain = AsyncMock()
    brain.learn_concept = AsyncMock(side_effect=_learn)
    brain.strengthen_connection = AsyncMock(return_value=None)
    return brain


MIND = MindId("m1")


class TestEncodeNoteBasics:
    @pytest.mark.asyncio()
    async def test_creates_one_concept_for_the_note(self) -> None:
        brain = _make_brain()
        note = _make_note(title="Hello", body="body")
        result = await encode_note(note, brain, MIND)

        assert isinstance(result, EncodeResult)
        assert result.concepts_created == 1
        assert result.relations_created == 0
        brain.learn_concept.assert_awaited()
        # First call is the note concept.
        call = brain.learn_concept.await_args_list[0]
        assert call.kwargs["name"] == "Hello"
        assert call.kwargs["source"] == "obsidian:note"
        assert call.kwargs["confidence"] == 0.7

    @pytest.mark.asyncio()
    async def test_body_becomes_concept_content(self) -> None:
        brain = _make_brain()
        note = _make_note(body="The full note body text.")
        await encode_note(note, brain, MIND)
        call = brain.learn_concept.await_args_list[0]
        assert call.kwargs["content"] == "The full note body text."

    @pytest.mark.asyncio()
    async def test_empty_body_falls_back_to_title(self) -> None:
        brain = _make_brain()
        note = _make_note(title="X", body="")
        await encode_note(note, brain, MIND)
        assert brain.learn_concept.await_args_list[0].kwargs["content"] == "X"


class TestCategoryInference:
    @pytest.mark.asyncio()
    async def test_person_tag_maps_to_entity(self) -> None:
        brain = _make_brain()
        note = _make_note(tags=(RawTag("person"),))
        await encode_note(note, brain, MIND)
        note_call = brain.learn_concept.await_args_list[0]
        assert note_call.kwargs["category"] is ConceptCategory.ENTITY

    @pytest.mark.asyncio()
    async def test_nested_person_tag_maps_to_entity(self) -> None:
        """`#person/alice` still routes via the ``person`` segment."""
        brain = _make_brain()
        note = _make_note(tags=(RawTag("person/alice"),))
        await encode_note(note, brain, MIND)
        note_call = brain.learn_concept.await_args_list[0]
        assert note_call.kwargs["category"] is ConceptCategory.ENTITY

    @pytest.mark.asyncio()
    async def test_unknown_tag_falls_back_to_fact(self) -> None:
        brain = _make_brain()
        note = _make_note(tags=(RawTag("randomword"),))
        await encode_note(note, brain, MIND)
        note_call = brain.learn_concept.await_args_list[0]
        assert note_call.kwargs["category"] is ConceptCategory.FACT

    @pytest.mark.asyncio()
    async def test_skill_tag_maps_to_skill(self) -> None:
        brain = _make_brain()
        note = _make_note(tags=(RawTag("skill"),))
        await encode_note(note, brain, MIND)
        assert brain.learn_concept.await_args_list[0].kwargs["category"] is ConceptCategory.SKILL


class TestWikilinks:
    @pytest.mark.asyncio()
    async def test_forward_link_creates_stub(self) -> None:
        brain = _make_brain()
        note = _make_note(links=(RawLink("Foo"),))
        result = await encode_note(note, brain, MIND)

        # Note concept + stub = 2 concepts created.
        assert result.concepts_created == 2
        assert result.relations_created == 1
        # Second learn_concept call is the stub with source=obsidian:stub.
        stub_call = brain.learn_concept.await_args_list[1]
        assert stub_call.kwargs["source"] == "obsidian:stub"
        assert stub_call.kwargs["name"] == "Foo"

    @pytest.mark.asyncio()
    async def test_reverse_resolution_uses_existing_concept(self) -> None:
        """Second note that links to an already-encoded target reuses its id."""
        brain = _make_brain()
        concept_by_name: dict[str, ConceptId] = {}
        tag_by_name: dict[str, ConceptId] = {}

        await encode_note(
            _make_note(title="A", path="a.md"),
            brain,
            MIND,
            concept_by_name=concept_by_name,
            tag_by_name=tag_by_name,
        )
        learn_count_after_a = brain.learn_concept.await_count

        # Encode B which links to A — should NOT create a stub for A.
        await encode_note(
            _make_note(title="B", path="b.md", links=(RawLink("A"),)),
            brain,
            MIND,
            concept_by_name=concept_by_name,
            tag_by_name=tag_by_name,
        )
        # B's note concept: +1 learn_concept. No stub.
        assert brain.learn_concept.await_count == learn_count_after_a + 1
        brain.strengthen_connection.assert_awaited()

    @pytest.mark.asyncio()
    async def test_embed_uses_part_of_relation(self) -> None:
        brain = _make_brain()
        note = _make_note(links=(RawLink("Target", is_embed=True),))
        await encode_note(note, brain, MIND)
        call = brain.strengthen_connection.await_args_list[0]
        relation_types = call.kwargs["relation_types"]
        assert RelationType.PART_OF.value in relation_types.values()

    @pytest.mark.asyncio()
    async def test_plain_link_uses_related_to(self) -> None:
        brain = _make_brain()
        note = _make_note(links=(RawLink("Target", is_embed=False),))
        await encode_note(note, brain, MIND)
        call = brain.strengthen_connection.await_args_list[0]
        assert RelationType.RELATED_TO.value in call.kwargs["relation_types"].values()

    @pytest.mark.asyncio()
    async def test_self_link_ignored(self) -> None:
        """A note linking to itself shouldn't emit a reflexive relation."""
        brain = _make_brain()
        note = _make_note(title="A", links=(RawLink("A"),))
        await encode_note(note, brain, MIND)
        # Only the note concept was learned; strengthen_connection never called.
        brain.strengthen_connection.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_repeated_links_dedup_target_but_bump_weight(self) -> None:
        """Three ``[[Foo]]`` references = one relation with bumped weight."""
        brain = _make_brain()
        note = _make_note(
            title="A",
            links=(RawLink("Foo"), RawLink("Foo"), RawLink("Foo")),
        )
        result = await encode_note(note, brain, MIND)
        assert result.relations_created == 1


class TestTagHierarchy:
    @pytest.mark.asyncio()
    async def test_flat_tag_creates_one_tag_concept(self) -> None:
        brain = _make_brain()
        note = _make_note(tags=(RawTag("linguistics"),))
        result = await encode_note(note, brain, MIND)

        # 1 note + 1 tag concept = 2; 1 relation (note -> tag).
        assert result.concepts_created == 2
        assert result.relations_created == 1
        tag_call = brain.learn_concept.await_args_list[1]
        assert tag_call.kwargs["name"] == "#linguistics"
        assert tag_call.kwargs["source"] == "obsidian:tag"
        assert tag_call.kwargs["category"] is ConceptCategory.SKILL

    @pytest.mark.asyncio()
    async def test_nested_tag_creates_chain(self) -> None:
        brain = _make_brain()
        note = _make_note(tags=(RawTag("project/alpha/beta"),))
        result = await encode_note(note, brain, MIND)

        # 1 note + 3 tag concepts (project, project/alpha, project/alpha/beta).
        assert result.concepts_created == 4
        # 2 parent-chain relations (alpha->project, beta->alpha) + 1 note->beta.
        assert result.relations_created == 3

    @pytest.mark.asyncio()
    async def test_repeated_tag_across_notes_reuses_concept(self) -> None:
        brain = _make_brain()
        concept_by_name: dict[str, ConceptId] = {}
        tag_by_name: dict[str, ConceptId] = {}

        for title in ("A", "B", "C"):
            await encode_note(
                _make_note(title=title, tags=(RawTag("study"),)),
                brain,
                MIND,
                concept_by_name=concept_by_name,
                tag_by_name=tag_by_name,
            )
        # 1 tag concept + 3 note concepts = 4 total learn_concept for *creation*.
        # Same-name learn_concept calls still hit brain.learn_concept (dedup
        # happens at BrainService level); we just check the shared state.
        assert "study" in tag_by_name


class TestErrorResilience:
    @pytest.mark.asyncio()
    async def test_strengthen_connection_failure_does_not_abort_note(self) -> None:
        """A relation creation error just drops that relation."""
        brain = _make_brain()
        brain.strengthen_connection = AsyncMock(side_effect=RuntimeError("db blew up"))

        note = _make_note(links=(RawLink("Target"),))
        result = await encode_note(note, brain, MIND)

        # Note + stub still created; relations_created=0 (all failed).
        assert result.concepts_created == 2
        assert result.relations_created == 0

    @pytest.mark.asyncio()
    async def test_mocked_encoder_runs_without_actual_brain(self) -> None:
        """Sanity check that encode_note never reaches non-mocked APIs."""
        brain = MagicMock()
        brain.learn_concept = AsyncMock(return_value=ConceptId("x"))
        brain.strengthen_connection = AsyncMock()

        note = _make_note()
        result = await encode_note(note, brain, MIND)
        assert result.note_concept_id == ConceptId("x")
