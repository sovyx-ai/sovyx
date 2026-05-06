"""Property tests for the P5 calibration migration registry (v0.30.33).

Idempotency invariant: running ``migrate_to_current`` twice on the
same input MUST produce byte-identical output. Since each migration
step is a PURE FUNCTION (no IO, no time, no randomness), the chain is
deterministic + idempotent.

Why a property test on top of the unit tests: the unit tests pin
specific shapes; Hypothesis generates 50 random v1-ish dicts and
exercises the walker against each, catching shape-dependent bugs
unit tests miss (e.g. a future migration that mutates a nested list
in a non-idempotent way, or a migration that adds a key conditionally
on the value of another key the first run already changed).
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.voice.calibration._migrations import migrate_to_current

# Constrain the JSON-ish leaves to keep the search space tractable +
# matching the actual calibration profile field types (str/int/float/bool/None
# at the leaves; nested dict/list one level deep is enough to surface
# in-place mutation bugs).
_LEAF: st.SearchStrategy[Any] = st.one_of(
    st.text(min_size=0, max_size=32),
    st.integers(min_value=-1_000, max_value=1_000),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.booleans(),
    st.none(),
)
_NESTED: st.SearchStrategy[Any] = st.recursive(
    _LEAF,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(min_size=1, max_size=8), children, max_size=5),
    ),
    max_leaves=20,
)


@given(
    extra=st.dictionaries(
        st.text(min_size=1, max_size=8).filter(lambda k: k != "schema_version"),
        _NESTED,
        max_size=8,
    ),
)
@settings(max_examples=50, deadline=None)
def test_migrate_to_current_is_idempotent(extra: dict[str, Any]) -> None:
    """Twice-migrating yields byte-identical output for any v1 dict.

    Construct a synthetic v1 profile from a Hypothesis-generated bag
    of fields plus the required ``schema_version: 1`` discriminator.
    Migrate once, then migrate the result again with the same target;
    the second pass MUST be a no-op (same-version branch returns a
    copy of the input). If a future migration step mutates state in a
    way that depends on already-migrated state, this property test
    surfaces the bug.
    """
    raw: dict[str, Any] = {**extra, "schema_version": 1}
    once = migrate_to_current(raw, target_version=2)
    twice = migrate_to_current(once, target_version=2)
    assert once == twice
    assert once["schema_version"] == 2


@given(
    extra=st.dictionaries(
        st.text(min_size=1, max_size=8).filter(lambda k: k != "schema_version"),
        _NESTED,
        max_size=8,
    ),
)
@settings(max_examples=30, deadline=None)
def test_migrate_to_current_does_not_mutate_input(extra: dict[str, Any]) -> None:
    """The walker's input dict is preserved byte-identical post-call.

    Defensive against callers handing off a dict they intend to re-use.
    The walker copies the input + mutates the copy; this test pins
    that contract.
    """
    raw: dict[str, Any] = {**extra, "schema_version": 1}
    snapshot = {k: v for k, v in raw.items()}
    migrate_to_current(raw, target_version=2)
    assert raw == snapshot


@given(
    target_version=st.integers(min_value=1, max_value=1),
    extra=st.dictionaries(
        st.text(min_size=1, max_size=8).filter(lambda k: k != "schema_version"),
        _NESTED,
        max_size=4,
    ),
)
@settings(max_examples=20, deadline=None)
def test_same_version_is_byte_identical_copy(target_version: int, extra: dict[str, Any]) -> None:
    """target_version == current_version returns an equal copy.

    Pinning this is important: the chain walker has a fast-path return
    BEFORE the main migration loop, and an accidental in-place mutation
    in that branch would slip past the unit tests.
    """
    raw: dict[str, Any] = {**extra, "schema_version": target_version}
    result = migrate_to_current(raw, target_version=target_version)
    assert result == raw
    assert result is not raw  # walker returns a defensive copy
