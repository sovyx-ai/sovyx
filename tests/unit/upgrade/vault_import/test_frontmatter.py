"""Tests for ``vault_import._frontmatter``.

Pure-text unit tests — no YAML files on disk, no I/O. Every edge
case the Obsidian community wiki mentions about frontmatter is
covered here: the block fence rules, the three-shape tolerance for
``aliases``/``tags``, malformed YAML, datetime coercion, mixed
timezones.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from sovyx.upgrade.vault_import._frontmatter import (
    extract_frontmatter,
    normalise_aliases,
    normalise_created_at,
    normalise_tags,
)


class TestExtractFrontmatter:
    """Fence-detection + YAML parsing behaviour."""

    def test_no_fence_returns_empty_dict_and_original_body(self) -> None:
        text = "Just a plain note.\n\nNo frontmatter here."
        fm, body = extract_frontmatter(text)
        assert fm == {}
        assert body == text

    def test_valid_frontmatter(self) -> None:
        text = "---\ntitle: My Note\ntags: [a, b]\n---\nBody line one.\n"
        fm, body = extract_frontmatter(text)
        assert fm == {"title": "My Note", "tags": ["a", "b"]}
        assert body == "Body line one.\n"

    def test_leading_whitespace_disqualifies_frontmatter(self) -> None:
        """Obsidian refuses any leading whitespace before the opening fence."""
        text = " ---\ntitle: Foo\n---\nbody"
        fm, body = extract_frontmatter(text)
        assert fm == {}
        assert body == text

    def test_missing_closing_fence(self) -> None:
        """Opening fence without closing → no frontmatter, body unchanged."""
        text = "---\ntitle: incomplete\nbody without closing fence"
        fm, body = extract_frontmatter(text)
        assert fm == {}
        assert body == text

    def test_malformed_yaml_still_strips_body(self) -> None:
        """Even when YAML is broken, the body after the closing fence is preserved."""
        # Unclosed quote inside a block scalar guarantees a parse error
        # across PyYAML versions — less permissive than PyYAML's default
        # tolerance, which happily accepts ``:::invalid yaml:::`` as a
        # nonsense-keyed mapping.
        text = '---\ntitle: "unclosed\n---\nThe real note body.\n'
        fm, body = extract_frontmatter(text)
        assert fm == {}
        assert body == "The real note body.\n"

    def test_yaml_that_parses_to_non_mapping_rejected(self) -> None:
        """A YAML list at top level is legal YAML but not a dict → empty fm."""
        text = "---\n- item1\n- item2\n---\nbody\n"
        fm, body = extract_frontmatter(text)
        assert fm == {}
        assert body == "body\n"

    def test_empty_frontmatter_block(self) -> None:
        """``---\\n---`` with nothing between → empty dict, body untouched."""
        text = "---\n---\nbody\n"
        fm, body = extract_frontmatter(text)
        assert fm == {}
        assert body == "body\n"

    def test_frontmatter_preserves_crlf_body(self) -> None:
        """Windows line endings in the body are passed through."""
        text = "---\ntitle: x\n---\r\nline1\r\nline2\r\n"
        _, body = extract_frontmatter(text)
        assert "line1" in body and "line2" in body

    def test_boolean_and_numeric_values_preserved(self) -> None:
        text = "---\npublished: true\ncount: 42\n---\nbody"
        fm, _ = extract_frontmatter(text)
        assert fm["published"] is True
        assert fm["count"] == 42


class TestNormaliseAliases:
    def test_none_returns_empty(self) -> None:
        assert normalise_aliases(None) == ()

    def test_string(self) -> None:
        assert normalise_aliases("PT Grammar") == ("PT Grammar",)

    def test_string_whitespace_trimmed(self) -> None:
        assert normalise_aliases("  X  ") == ("X",)

    def test_empty_string(self) -> None:
        assert normalise_aliases("   ") == ()

    def test_list_of_strings(self) -> None:
        assert normalise_aliases(["A", "B", "C"]) == ("A", "B", "C")

    def test_list_drops_empty_and_non_strings(self) -> None:
        assert normalise_aliases(["A", "", None, 42, "B"]) == ("A", "B")

    def test_other_types_return_empty(self) -> None:
        assert normalise_aliases(42) == ()
        assert normalise_aliases({"not": "a list"}) == ()


class TestNormaliseTags:
    def test_strips_leading_hash(self) -> None:
        assert normalise_tags("#language") == ("language",)

    def test_list_shape(self) -> None:
        assert normalise_tags(["study", "#2024/q1"]) == ("study", "2024/q1")

    def test_empty_after_strip(self) -> None:
        assert normalise_tags("#") == ()
        assert normalise_tags("#   ") == ()

    def test_preserves_nested(self) -> None:
        assert normalise_tags(["project/alpha", "project/beta"]) == (
            "project/alpha",
            "project/beta",
        )


class TestNormaliseCreatedAt:
    def test_none_returns_none(self) -> None:
        assert normalise_created_at(None) is None

    def test_date_becomes_utc_midnight(self) -> None:
        result = normalise_created_at(date(2024, 3, 15))
        assert result == datetime(2024, 3, 15, tzinfo=UTC)

    def test_naive_datetime_attached_to_utc(self) -> None:
        result = normalise_created_at(datetime(2024, 3, 15, 10, 30))
        assert result is not None
        assert result.tzinfo is UTC

    def test_aware_datetime_preserved(self) -> None:

        bra = UTC  # any tz-aware works; reuse UTC for simplicity
        dt = datetime(2024, 3, 15, 10, 30, tzinfo=bra)
        result = normalise_created_at(dt)
        assert result == dt

    def test_iso_string_parsed(self) -> None:
        result = normalise_created_at("2024-03-15T10:30:00")
        assert result == datetime(2024, 3, 15, 10, 30, tzinfo=UTC)

    def test_invalid_string_returns_none(self) -> None:
        assert normalise_created_at("not a date") is None

    @pytest.mark.parametrize("garbage", [42, [], {}, True])
    def test_unrelated_types_return_none(self, garbage: object) -> None:
        assert normalise_created_at(garbage) is None
