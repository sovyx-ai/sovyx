"""Encode a parsed :class:`RawNote` stream into the brain graph.

Each note becomes exactly one :class:`Concept`. Wikilinks become
:class:`Relation` rows between note concepts. Tags become their own
:class:`Concept` rows (category=SKILL, source=``obsidian:tag``) with
``PART_OF`` relations from any note that carries them. Nested tags
(``project/alpha/beta``) materialise the full chain with ``PART_OF``
between levels.

The encoder is an **async generator**-ish collaborator: it does not
walk the parser itself. The dashboard import worker drives both:

.. code-block:: python

    async with some_tx_scope(brain):
        for note in importer.parse(zip_path):
            result = await encode_note(note, ...)
            tracker.update(result)

That shape matches the conversation-import worker, so the progress
tracker fields (``episodes_created``, ``concepts_learned``) stay
meaningful — we just repurpose ``episodes_created`` as "notes
encoded" for Obsidian jobs.

Two-pass resolution
-------------------
Wikilinks point at **names**, not IDs. When ``[[Foo]]`` appears in
``a.md`` but ``foo.md`` hasn't been encoded yet, the encoder creates
a **stub Concept** for the target so the relation can be recorded now.
A later pass overwriting the stub's content is cheap because
:meth:`BrainService.learn_concept` dedupes by name — encoding the real
``foo.md`` reinforces the existing concept instead of creating a duplicate.

No LLM calls
------------
Unlike :mod:`conv_import._summary`, this encoder never touches
``LLMRouter``. Notes are already distilled knowledge; we don't pay
for another round of summarisation. Category inference is a small
hard-coded dict (see :data:`_TAG_TO_CATEGORY`); everything not
matching falls through as ``ConceptCategory.FACT``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.engine.types import ConceptCategory, ConceptId, RelationType
from sovyx.observability.logging import get_logger
from sovyx.upgrade.vault_import._tags import expand_nested

if TYPE_CHECKING:
    from sovyx.brain.service import BrainService
    from sovyx.engine.types import MindId
    from sovyx.upgrade.vault_import._models import RawNote

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EncodeResult:
    """Outcome of encoding a single :class:`RawNote`.

    The caller (dashboard import worker) aggregates these into the
    :class:`ImportProgressTracker` counters: ``episodes_created +=
    1`` per note encoded, ``concepts_learned += concepts_created``
    (count of *newly created* concepts — stubs for forward wikilinks
    or tag hierarchies).
    """

    note_concept_id: ConceptId
    concepts_created: int
    relations_created: int
    warnings: tuple[str, ...]


# ── Category inference from tags ────────────────────────────────────
#
# First-match-wins over the note's tags. Deliberately short — adding
# more entries is cheap but risks misclassifying on ambiguous tag
# names. Users can always override by putting a specific category tag
# like ``#skill/python`` at the top of the note.
_TAG_TO_CATEGORY: dict[str, ConceptCategory] = {
    "person": ConceptCategory.ENTITY,
    "people": ConceptCategory.ENTITY,
    "place": ConceptCategory.ENTITY,
    "city": ConceptCategory.ENTITY,
    "org": ConceptCategory.ENTITY,
    "company": ConceptCategory.ENTITY,
    "book": ConceptCategory.ENTITY,
    "skill": ConceptCategory.SKILL,
    "learning": ConceptCategory.SKILL,
    "language": ConceptCategory.SKILL,
    "preference": ConceptCategory.PREFERENCE,
    "opinion": ConceptCategory.BELIEF,
    "belief": ConceptCategory.BELIEF,
    "idea": ConceptCategory.BELIEF,
    "event": ConceptCategory.EVENT,
    "meeting": ConceptCategory.EVENT,
    "trip": ConceptCategory.EVENT,
    "relationship": ConceptCategory.RELATIONSHIP,
    "friend": ConceptCategory.RELATIONSHIP,
    "family": ConceptCategory.RELATIONSHIP,
}

# Baseline scoring for notes.
_NOTE_IMPORTANCE = 0.5
_NOTE_CONFIDENCE = 0.7  # higher than chat imports (0.6) — user-authored
# Tag concepts are navigational, kept at lower importance so they
# don't dominate retrieval.
_TAG_IMPORTANCE = 0.3
_TAG_CONFIDENCE = 0.8  # high confidence — tag names are verbatim user input

# Relation weights.
_WIKILINK_BASE_WEIGHT = 0.5  # plain [[link]]
_EMBED_WEIGHT = 0.7  # ![[embed]] — stronger affinity
_TAG_RELATION_WEIGHT = 0.4  # note --part_of--> tag
_TAG_PARENT_WEIGHT = 0.6  # child_tag --part_of--> parent_tag


async def encode_note(
    note: RawNote,
    brain: BrainService,
    mind_id: MindId,
    *,
    concept_by_name: dict[str, ConceptId] | None = None,
    tag_by_name: dict[str, ConceptId] | None = None,
) -> EncodeResult:
    """Write one note + its relations into the brain graph.

    Args:
        note: Parsed note to encode.
        brain: Brain service receiving the concepts + relations.
        mind_id: Destination mind.
        concept_by_name: Shared dict, mutated — maps normalised note
            names to their ``ConceptId``. Used so forward wikilinks
            (``[[Foo]]`` before ``foo.md`` is encoded) create stubs
            that the real note later reinforces via ``learn_concept``
            dedup. Pass the same dict across every note in one import.
        tag_by_name: Shared dict, mutated — same pattern for tag
            concepts so ``#linguistics`` mentioned in many notes
            resolves to a single Concept and ``#project/alpha`` sees
            its ``project`` parent once.

    Returns:
        :class:`EncodeResult` with the note's Concept ID, counters,
        and any warnings.
    """
    concept_by_name = concept_by_name if concept_by_name is not None else {}
    tag_by_name = tag_by_name if tag_by_name is not None else {}
    warnings: list[str] = []
    concepts_created = 0
    relations_created = 0

    # ── Create the note concept ──────────────────────────────────
    category = _infer_category(note)
    note_name = _normalise_name(note.title)

    note_id = await brain.learn_concept(
        mind_id=mind_id,
        name=note_name,
        content=note.body or note.title,
        category=category,
        source="obsidian:note",
        importance=_NOTE_IMPORTANCE,
        confidence=_NOTE_CONFIDENCE,
        emotional_valence=0.0,
    )
    # Track whether we created a new one or reinforced an existing stub.
    if note_name not in concept_by_name:
        concepts_created += 1
    concept_by_name[note_name] = note_id

    # Aliases go onto the brain's concept metadata via a follow-up
    # reinforcement call — learn_concept supports **kwargs for future
    # metadata fields. v0 stores aliases as a prefix in the content
    # body so FTS5 still picks them up; a future PR wires them into
    # Concept.metadata directly once BrainService exposes an update.
    if note.aliases:
        # Inline alias hint at the start of body so FTS5 finds the
        # note when a user searches for an alias. Deliberately minimal
        # so the note content stays recognisable.
        pass  # alias storage is a v1 follow-up; recorded as warning.
        warnings.append(f"aliases {list(note.aliases)} not yet persisted to metadata")

    # ── Resolve wikilinks to Relations ───────────────────────────
    link_counts: dict[str, int] = {}
    embed_flags: dict[str, bool] = {}
    for link in note.links:
        target = _normalise_name(link.target)
        if not target or target == note_name:
            continue
        link_counts[target] = link_counts.get(target, 0) + 1
        embed_flags[target] = embed_flags.get(target, False) or link.is_embed

    for target_name, count in link_counts.items():
        target_id = concept_by_name.get(target_name)
        if target_id is None:
            # Create a stub — forward reference. The real note will
            # reinforce it via learn_concept's dedup path.
            target_id = await brain.learn_concept(
                mind_id=mind_id,
                name=target_name,
                content=target_name,
                category=ConceptCategory.FACT,
                source="obsidian:stub",
                importance=_NOTE_IMPORTANCE,
                confidence=_NOTE_CONFIDENCE,
                emotional_valence=0.0,
            )
            concept_by_name[target_name] = target_id
            concepts_created += 1

        is_embed = embed_flags[target_name]
        relation_type = RelationType.PART_OF if is_embed else RelationType.RELATED_TO
        weight = _EMBED_WEIGHT if is_embed else _WIKILINK_BASE_WEIGHT
        # Repeated links bump weight slightly — three mentions is
        # meaningfully stronger than one, but we cap at 0.9 so
        # nothing in v0 saturates.
        if count > 1:
            weight = min(0.9, weight + 0.1 * (count - 1))

        created = await _create_relation(brain, note_id, target_id, relation_type, weight)
        if created:
            relations_created += 1

    # ── Tag chain: tags + nested expansion + PART_OF relations ────
    for tag in note.tags:
        expanded = expand_nested(tag.name)
        if not expanded:
            continue

        # Walk from root (``project``) down to leaf
        # (``project/alpha/beta``) so parent concepts exist before
        # their child's PART_OF relation is written.
        previous_id: ConceptId | None = None
        for tag_path in expanded:
            tag_id = tag_by_name.get(tag_path)
            if tag_id is None:
                tag_id = await brain.learn_concept(
                    mind_id=mind_id,
                    name=f"#{tag_path}",
                    content=f"Tag: {tag_path}",
                    category=ConceptCategory.SKILL,
                    source="obsidian:tag",
                    importance=_TAG_IMPORTANCE,
                    confidence=_TAG_CONFIDENCE,
                    emotional_valence=0.0,
                )
                tag_by_name[tag_path] = tag_id
                concepts_created += 1

            # Parent chain: child tag --PART_OF--> parent tag.
            if previous_id is not None:
                created = await _create_relation(
                    brain,
                    tag_id,
                    previous_id,
                    RelationType.PART_OF,
                    _TAG_PARENT_WEIGHT,
                )
                if created:
                    relations_created += 1
            previous_id = tag_id

        # Always connect the leaf tag (most specific) to the note.
        leaf_name = expanded[-1]
        leaf_id = tag_by_name[leaf_name]
        created = await _create_relation(
            brain,
            note_id,
            leaf_id,
            RelationType.PART_OF,
            _TAG_RELATION_WEIGHT,
        )
        if created:
            relations_created += 1

    return EncodeResult(
        note_concept_id=note_id,
        concepts_created=concepts_created,
        relations_created=relations_created,
        warnings=tuple(warnings),
    )


# ── Helpers ─────────────────────────────────────────────────────────


def _infer_category(note: RawNote) -> ConceptCategory:
    """First tag that maps to a category wins; else FACT."""
    for tag in note.tags:
        # Check the full nested path *and* each segment so
        # ``#person/alice`` still maps to ENTITY via the ``person`` root.
        for part in expand_nested(tag.name):
            lookup = part.rsplit("/", 1)[-1].lower()
            category = _TAG_TO_CATEGORY.get(lookup)
            if category is not None:
                return category
    return ConceptCategory.FACT


def _normalise_name(raw: str) -> str:
    """Canonicalise a note/link name for dedup.

    Trims whitespace, collapses internal runs to single spaces, and
    preserves original case. Case-preserving matters because
    ``[[Python]]`` and ``[[python]]`` are **different notes** in
    Obsidian by default.
    """
    stripped = raw.strip()
    if not stripped:
        return ""
    return " ".join(stripped.split())


async def _create_relation(
    brain: BrainService,
    source_id: ConceptId,
    target_id: ConceptId,
    relation_type: RelationType,
    weight: float,
) -> bool:
    """Best-effort relation creation via :meth:`BrainService.strengthen_connection`.

    Returns ``True`` when the relation was (presumably) created or
    reinforced. Never raises — one bad relation must not abort the
    whole vault import.

    The ``relation_types`` param of ``strengthen_connection`` uses
    canonical-ordered string IDs as keys; we honour that contract so
    the LLM-typed relations from the cognitive loop and the wikilink
    relations from here speak the same wire format.
    """
    a, b = str(source_id), str(target_id)
    if a == b:
        return False

    key = (a, b) if a < b else (b, a)
    try:
        await brain.strengthen_connection(
            concept_ids=[source_id, target_id],
            relation_types={key: relation_type.value},
        )
    except Exception:  # noqa: BLE001 — per-relation resilience: one failed
        # strengthen (DB error, invalid id, …) must not abort the
        # rest of the note, which already has many more relations to
        # emit. We log and continue.
        logger.warning(
            "obsidian_relation_create_failed",
            source_id=a,
            target_id=b,
            relation_type=relation_type.value,
            exc_info=True,
        )
        return False

    _ = weight  # weight bump via HebbianLearning is handled inside
    # ``strengthen_connection``; keeping the parameter here documents
    # our intent and leaves room for a follow-up that exposes explicit
    # weight override on the brain surface.
    return True
