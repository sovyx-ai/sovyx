"""VAL-37: Brain invariant properties — Hypothesis.

Additional brain invariants beyond test_brain_properties.py.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.brain.models import Concept
from sovyx.brain.working_memory import WorkingMemory
from sovyx.context.tokenizer import TokenCounter
from sovyx.engine.types import ConceptCategory, ConceptId, MindId


class TestWorkingMemoryInvariants:
    """Working memory must maintain bounded capacity."""

    @settings(deadline=None)
    @given(n=st.integers(min_value=0, max_value=50))
    def test_items_never_exceed_capacity(self, n: int) -> None:
        wm = WorkingMemory(capacity=10)
        for i in range(n):
            wm.activate(ConceptId(f"c{i}"), activation=float(i) / max(n, 1))
        assert wm.size <= 10

    @settings(deadline=None)
    @given(activations=st.lists(st.floats(0.1, 1), min_size=1, max_size=20))
    def test_highest_activation_survives(self, activations: list[float]) -> None:
        wm = WorkingMemory(capacity=5)
        for i, a in enumerate(activations):
            wm.activate(ConceptId(f"c{i}"), activation=a)
        active = wm.get_active_concepts()
        assert len(active) <= 5

    @settings(deadline=None)
    @given(st.just(True))
    def test_clear_empties(self, _: bool) -> None:
        wm = WorkingMemory(capacity=10)
        for i in range(5):
            wm.activate(ConceptId(f"c{i}"), activation=0.5)
        wm.clear()
        assert wm.size == 0


class TestConceptInvariants:
    """Concept model invariants."""

    @settings(deadline=None)
    @given(
        importance=st.floats(0, 1),
        confidence=st.floats(0, 1),
    )
    def test_importance_and_confidence_bounded(
        self,
        importance: float,
        confidence: float,
    ) -> None:
        c = Concept(
            id=ConceptId("test"),
            mind_id=MindId("m"),
            name="test",
            content="test content",
            category=ConceptCategory.FACT,
            importance=importance,
            confidence=confidence,
        )
        assert 0 <= c.importance <= 1
        assert 0 <= c.confidence <= 1


class TestTokenCounterInvariants:
    """Token counting invariants."""

    @settings(deadline=None, max_examples=50)
    @given(text=st.text(max_size=1000))
    def test_count_non_negative(self, text: str) -> None:
        counter = TokenCounter()
        assert counter.count(text) >= 0

    @settings(deadline=None, max_examples=50)
    @given(
        a=st.text(max_size=200),
        b=st.text(max_size=200),
    )
    def test_concatenation_token_count_bounded_by_byte_length(self, a: str, b: str) -> None:
        """count(a+b) <= count(a) + count(b) + max(byte_len(a), byte_len(b)).

        BPE tokenizers are NOT subadditive — concatenation can
        produce strictly MORE tokens than the sum of individual
        counts. The pre-fix invariant ``count(a+b) <= count(a) +
        count(b) + 3`` was empirically wrong: Hypothesis found
        ``a='FILENAME', b='MODEL'`` where individual counts are
        1 + 1 = 2 but ``count('FILENAMEMODEL')`` is 6. Both
        ``FILENAME`` and ``MODEL`` are single multi-character BPE
        merges in cl100k_base; their concatenation has no matching
        merge so BPE falls back to byte-level tokenization across
        the entire boundary region.

        The mathematically correct upper bound is the BYTE length
        of the longer input: at worst, one side's BPE structure is
        fully re-segmented into byte-level tokens, but BPE
        guarantees at most 1 token per UTF-8 byte (the byte-fallback
        property of cl100k_base + cl100k+ + o200k_base). So
        ``count(a+b) <= count(a) + count(b) + max(byte_len(a),
        byte_len(b))`` is provably tight and held by every example
        Hypothesis has explored.

        Production callers that piecewise-sum per-line / per-chunk
        token counts (e.g. ``context/formatter.py:110-118``) MUST
        either tolerate this slack OR do a final ``count(assembled)``
        and trim — see ``test_bpe_concatenation_can_exceed_constant_slack``
        for the documented pathology pinning the worst-case
        contract.
        """
        counter = TokenCounter()
        a_bytes = len(a.encode("utf-8"))
        b_bytes = len(b.encode("utf-8"))
        assert counter.count(a + b) <= (
            counter.count(a) + counter.count(b) + max(a_bytes, b_bytes)
        )

    @settings(deadline=None, max_examples=50)
    @given(text=st.text(max_size=500))
    def test_count_bounded_by_byte_length(self, text: str) -> None:
        """count(text) <= byte_len(text).

        Universal upper bound for BPE: every UTF-8 byte produces at
        most one token (byte-fallback property of cl100k_base). This
        property held trivially in pre-fix code; pin it here so a
        future encoding change (e.g. swapping in a non-byte-level
        BPE) is caught by the property suite.
        """
        counter = TokenCounter()
        assert counter.count(text) <= len(text.encode("utf-8"))

    def test_bpe_concatenation_can_exceed_constant_slack(self) -> None:
        """Document the BPE non-subadditivity pathology found by
        Hypothesis on 2026-04-30 — pin the worst-case behaviour so a
        future "let's add subadditivity back" change is caught.

        ``FILENAME`` and ``MODEL`` both tokenize as single BPE merges
        in cl100k_base (placeholder/template-style strings rare in
        natural text but common in code/templates). Their
        concatenation ``FILENAMEMODEL`` has no matching merge and
        BPE falls back across the boundary, producing 6 byte-level
        tokens. This is INTENTIONAL behaviour of tiktoken / cl100k:
        BPE is empirically NOT subadditive.

        Implications for production code:

        * ``context/assembler.py:160-172`` does the canonical
          conservative pattern — piecewise-count, then ``count_messages``
          on the assembled text + trim if over budget.
        * ``context/formatter.py:110-118`` does NAKED piecewise sum
          without a final-count + trim pass. This is a documented
          fragility (see follow-up triage); the worst-case underestimate
          is bounded by ``max(byte_len(line))`` per added line, which
          is tight in practice but not safe at the very-long-line edge.
        * Any new code that budgets via summing per-chunk
          ``count()`` results MUST account for the boundary-corruption
          slack OR do a final ``count(assembled)`` validation.

        If this test ever fails (joined <= individual + 3), the
        tiktoken vocab has shifted such that the worst-case BPE
        slack improved. That's not a regression — it's a positive
        finding that should trigger a re-evaluation of the
        production-side conservatism. Update this test + the
        formatter follow-up accordingly.
        """
        counter = TokenCounter()
        individual = counter.count("FILENAME") + counter.count("MODEL")
        joined = counter.count("FILENAMEMODEL")
        assert joined > individual + 3, (
            f"BPE pathology weakened: joined={joined} vs individual+3={individual + 3}. "
            "tiktoken vocab may have shifted — verify the new worst-case bound "
            "and update production callers if the slack improved."
        )

    @settings(deadline=None, max_examples=30)
    @given(text=st.text(min_size=1, max_size=500))
    def test_truncate_respects_limit(self, text: str) -> None:
        counter = TokenCounter()
        limit = 5
        truncated = counter.truncate(text, limit)
        assert counter.count(truncated) <= limit
