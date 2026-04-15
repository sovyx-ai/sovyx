"""Tests for ``vault_import._tags``."""

from __future__ import annotations

from sovyx.upgrade.vault_import._models import RawTag
from sovyx.upgrade.vault_import._tags import (
    expand_nested,
    extract_body_tags,
    merge_tags,
)


class TestExtractBodyTags:
    def test_simple_tag(self) -> None:
        tags = extract_body_tags("This is #linguistics stuff.")
        assert tags == (RawTag(name="linguistics"),)

    def test_multiple_unique_tags(self) -> None:
        tags = extract_body_tags("#a #b #c")
        assert tags == (RawTag("a"), RawTag("b"), RawTag("c"))

    def test_dedup_preserves_first_order(self) -> None:
        tags = extract_body_tags("#study notes #study again #study")
        assert tags == (RawTag("study"),)

    def test_nested_tag_preserved_as_full_name(self) -> None:
        tags = extract_body_tags("#project/alpha working")
        assert tags == (RawTag("project/alpha"),)

    def test_deeply_nested(self) -> None:
        tags = extract_body_tags("#project/alpha/beta")
        assert tags == (RawTag("project/alpha/beta"),)

    def test_url_fragment_not_matched_as_tag(self) -> None:
        """``http://x.com#anchor`` must NOT produce ``RawTag("anchor")``."""
        tags = extract_body_tags("See https://example.com#section for details.")
        assert tags == ()

    def test_hash_inside_word_not_a_tag(self) -> None:
        """``foo#bar`` is not a tag — tag must be at word boundary."""
        tags = extract_body_tags("foo#bar #real")
        assert tags == (RawTag("real"),)

    def test_hash_digit_not_matched(self) -> None:
        """Markdown headings at line start shouldn't become tags."""
        tags = extract_body_tags("##heading\n# h1\n#123 not a tag")
        assert tags == ()

    def test_tag_inside_code_fence_excluded(self) -> None:
        tags = extract_body_tags("```\n#not-a-tag\n```\n#real")
        assert tags == (RawTag("real"),)

    def test_tag_inside_inline_code_excluded(self) -> None:
        tags = extract_body_tags("code `#fake` vs #real")
        assert tags == (RawTag("real"),)

    def test_punctuation_after_tag(self) -> None:
        tags = extract_body_tags("Ending sentence with #tag. New sentence.")
        assert tags == (RawTag("tag"),)

    def test_tag_with_hyphen_and_underscore(self) -> None:
        tags = extract_body_tags("#language-learning #foo_bar")
        assert tags == (RawTag("language-learning"), RawTag("foo_bar"))


class TestMergeTags:
    def test_empty(self) -> None:
        assert merge_tags() == ()
        assert merge_tags((), (), ()) == ()

    def test_single_source(self) -> None:
        assert merge_tags((RawTag("a"), RawTag("b"))) == (RawTag("a"), RawTag("b"))

    def test_multiple_sources_dedup_preserve_order(self) -> None:
        body = (RawTag("a"), RawTag("b"))
        frontmatter = (RawTag("b"), RawTag("c"))
        assert merge_tags(body, frontmatter) == (RawTag("a"), RawTag("b"), RawTag("c"))

    def test_first_occurrence_wins(self) -> None:
        s1 = (RawTag("x"),)
        s2 = (RawTag("y"), RawTag("x"))
        assert merge_tags(s1, s2) == (RawTag("x"), RawTag("y"))


class TestExpandNested:
    def test_flat_tag(self) -> None:
        assert expand_nested("linguistics") == ("linguistics",)

    def test_two_levels(self) -> None:
        assert expand_nested("project/alpha") == ("project", "project/alpha")

    def test_three_levels(self) -> None:
        assert expand_nested("project/alpha/beta") == (
            "project",
            "project/alpha",
            "project/alpha/beta",
        )

    def test_empty_string(self) -> None:
        assert expand_nested("") == ()

    def test_strips_empty_parts(self) -> None:
        """Double-slash ``project//alpha`` should yield ``project/alpha`` chain without ghosts."""
        assert expand_nested("project//alpha") == ("project", "project/alpha")
