"""Tests for ``vault_import._wikilinks`` — the ``[[link]]`` regex."""

from __future__ import annotations

from sovyx.upgrade.vault_import._wikilinks import extract_links, strip_code


class TestStripCode:
    def test_removes_fenced_code(self) -> None:
        text = "a\n```\n[[not a link]]\n```\nb"
        scrubbed = strip_code(text)
        assert "[[not a link]]" not in scrubbed
        assert "a" in scrubbed and "b" in scrubbed

    def test_removes_inline_code(self) -> None:
        text = "call `[[Foo]]` then link to [[Bar]]"
        scrubbed = strip_code(text)
        assert "[[Foo]]" not in scrubbed  # was inside backticks
        assert "[[Bar]]" in scrubbed

    def test_preserves_length(self) -> None:
        """Replacement is spaces of equal length — keeps future offsets valid."""
        text = "```code```XXX"
        scrubbed = strip_code(text)
        assert len(scrubbed) == len(text)
        assert scrubbed.endswith("XXX")


class TestExtractLinks:
    def test_plain_wikilink(self) -> None:
        links = extract_links("See [[Foo]] for details.")
        assert len(links) == 1
        assert links[0].target == "Foo"
        assert not links[0].is_embed

    def test_multiple_links(self) -> None:
        links = extract_links("[[Foo]] and [[Bar Baz]] and [[Qux]]")
        assert [link.target for link in links] == ["Foo", "Bar Baz", "Qux"]

    def test_pipe_alias_keeps_target(self) -> None:
        links = extract_links("See [[Portuguese Grammar|PT Grammar]].")
        assert len(links) == 1
        assert links[0].target == "Portuguese Grammar"

    def test_heading_fragment_dropped(self) -> None:
        links = extract_links("jump to [[Note#SomeHeading]].")
        assert links[0].target == "Note"

    def test_pipe_and_fragment(self) -> None:
        links = extract_links("[[Target#Heading|Display]]")
        assert links[0].target == "Target"

    def test_embed_syntax_detected(self) -> None:
        links = extract_links("![[image.png]]")
        assert links[0].is_embed is True
        assert links[0].target == "image.png"

    def test_embed_and_link_mixed(self) -> None:
        links = extract_links("![[Embed]] then [[Link]]")
        assert links[0].is_embed is True
        assert links[1].is_embed is False

    def test_duplicates_preserved_for_weight_counting(self) -> None:
        """Encoder uses repeat count to bump relation weight — don't dedup here."""
        links = extract_links("[[Foo]] [[Foo]] [[Foo]]")
        assert len(links) == 3
        assert all(link.target == "Foo" for link in links)

    def test_code_fence_excludes_links(self) -> None:
        """Links inside fenced code are NOT extracted."""
        text = "```\n[[NotExtracted]]\n```\n[[Real]]"
        links = extract_links(text)
        assert [link.target for link in links] == ["Real"]

    def test_inline_code_excludes_links(self) -> None:
        links = extract_links("example: `[[NotReal]]` but [[Real]]")
        assert [link.target for link in links] == ["Real"]

    def test_empty_brackets_ignored(self) -> None:
        links = extract_links("[[]] and [[  ]]")
        # Whitespace-only target is dropped by _clean_target strip.
        assert all(link.target for link in links)

    def test_unclosed_bracket_ignored(self) -> None:
        """``[[Foo`` without ``]]`` → no match."""
        links = extract_links("broken [[Foo and [[Real]]")
        assert [link.target for link in links] == ["Real"]

    def test_nested_brackets_rejected(self) -> None:
        """A target containing ``[`` is invalid — match starts over at the inner link."""
        links = extract_links("[[Outer [[Inner]] Rest]]")
        # The regex rejects ``[`` inside the target, so the outer
        # candidate ``[[Outer [[Inner]]`` fails and matching restarts
        # at the inner ``[[Inner]]``.
        assert [link.target for link in links] == ["Inner"]

    def test_multiline_body(self) -> None:
        body = "line 1 [[A]]\nline 2\nline 3 [[B]]\n"
        links = extract_links(body)
        assert [link.target for link in links] == ["A", "B"]
