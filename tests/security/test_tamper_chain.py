"""End-to-end integrity tests for :class:`HashChainHandler`.

The unit suite already covers ``verify_chain`` in isolation. These
tests assert the *full* contract from the operator's perspective:
write through the handler, then run :func:`verify_chain` on the
on-disk file and confirm any plausible tampering is detected with
the correct broken-line index.

Failures here mean a tamper-evident log can be silently rewritten
without the verifier noticing — that's a compliance-grade incident
for any deployment that opted into ``features.tamper_chain``.

Aligned with IMPL-OBSERVABILITY-001 §15 (Phase 9, Task 9.6).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from sovyx.observability.tamper import (
    GENESIS_HASH,
    HashChainHandler,
    verify_chain,
)


class _JsonMsgFormatter(logging.Formatter):
    """Minimal formatter — ``record.msg`` is a dict, emit it as JSON.

    The chain handler only needs the formatted output to be a JSON
    object. We deliberately keep this trivial so the tests focus on
    chain semantics rather than envelope shape.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload = record.msg if isinstance(record.msg, dict) else {"msg": str(record.msg)}
        return json.dumps(payload, ensure_ascii=False)


def _make_handler(path: Path) -> HashChainHandler:
    """Build a chain handler bound to *path* with a JSON formatter attached."""
    handler = HashChainHandler(path, max_bytes=10 * 1024 * 1024, backup_count=3)
    handler.setFormatter(_JsonMsgFormatter())
    return handler


def _emit(handler: HashChainHandler, payload: dict[str, Any]) -> None:
    """Push *payload* through the handler as a single LogRecord."""
    record = logging.LogRecord(
        name="tamper.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=payload,
        args=None,
        exc_info=None,
    )
    handler.emit(record)


def _read_records(path: Path) -> list[dict[str, Any]]:
    """Parse every non-empty line in *path* as a JSON record."""
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


@pytest.fixture()
def chain_log(tmp_path: Path) -> Generator[Path, None, None]:
    """Yield a fresh chain-log path; tests close their own handlers."""
    path = tmp_path / "audit.jsonl"
    yield path


class TestChainShape:
    """Every emitted record carries the chain triplet with the documented shape."""

    def test_records_carry_chain_fields(self, chain_log: Path) -> None:
        handler = _make_handler(chain_log)
        try:
            for i in range(3):
                _emit(handler, {"event": "audit.row", "i": i})
        finally:
            handler.close()

        records = _read_records(chain_log)
        assert len(records) == 3
        for rec in records:
            assert "prev_hash" in rec
            assert "chain_hash" in rec
            assert "chain_id" in rec
            assert isinstance(rec["chain_hash"], str)
            assert len(rec["chain_hash"]) == 64

    def test_first_record_prev_hash_is_genesis(self, chain_log: Path) -> None:
        handler = _make_handler(chain_log)
        try:
            _emit(handler, {"event": "audit.first"})
        finally:
            handler.close()

        records = _read_records(chain_log)
        assert records[0]["prev_hash"] == GENESIS_HASH

    def test_subsequent_prev_hash_equals_previous_chain_hash(self, chain_log: Path) -> None:
        handler = _make_handler(chain_log)
        try:
            for i in range(5):
                _emit(handler, {"event": "audit.row", "i": i})
        finally:
            handler.close()

        records = _read_records(chain_log)
        for prev, cur in zip(records, records[1:], strict=False):
            assert cur["prev_hash"] == prev["chain_hash"]

    def test_chain_id_constant_within_one_segment(self, chain_log: Path) -> None:
        handler = _make_handler(chain_log)
        try:
            for i in range(4):
                _emit(handler, {"event": "audit.row", "i": i})
        finally:
            handler.close()

        records = _read_records(chain_log)
        chain_ids = {rec["chain_id"] for rec in records}
        assert len(chain_ids) == 1


class TestVerifyHonest:
    """A clean, honest chain verifies cleanly."""

    def test_fresh_chain_verifies(self, chain_log: Path) -> None:
        handler = _make_handler(chain_log)
        try:
            for i in range(7):
                _emit(handler, {"event": "audit.row", "i": i, "label": f"r-{i}"})
        finally:
            handler.close()

        intact, idx = verify_chain(chain_log)
        assert intact is True
        assert idx == -1

    def test_unicode_payload_verifies(self, chain_log: Path) -> None:
        # ``ensure_ascii=False`` in canonicalisation means raw glyphs survive
        # to disk; the verifier MUST canonicalise the same way to match.
        handler = _make_handler(chain_log)
        try:
            _emit(handler, {"event": "audit.row", "note": "olá — ção"})
        finally:
            handler.close()

        intact, idx = verify_chain(chain_log)
        assert intact is True
        assert idx == -1

    def test_empty_lines_are_skipped(self, chain_log: Path) -> None:
        handler = _make_handler(chain_log)
        try:
            for i in range(3):
                _emit(handler, {"event": "audit.row", "i": i})
        finally:
            handler.close()

        # Splice a blank line in the middle — the verifier must skip it.
        original = chain_log.read_text(encoding="utf-8").splitlines()
        chain_log.write_text(
            original[0] + "\n\n" + "\n".join(original[1:]) + "\n",
            encoding="utf-8",
        )

        intact, idx = verify_chain(chain_log)
        assert intact is True
        assert idx == -1


class TestTamperDetection:
    """Every plausible tamper produces a precise (False, idx) result."""

    def test_single_line_edit_detected_at_edit_index(self, chain_log: Path) -> None:
        handler = _make_handler(chain_log)
        try:
            for i in range(5):
                _emit(handler, {"event": "audit.row", "i": i})
        finally:
            handler.close()

        # Surgically rewrite line index 2 with a different ``i`` while
        # keeping the chain fields intact. The recomputed chain_hash for
        # line 2 will not match the stored one, so the verifier flags 2.
        lines = chain_log.read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[2])
        rec["i"] = 999  # silently changed payload
        lines[2] = json.dumps(rec, ensure_ascii=False)
        chain_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        intact, idx = verify_chain(chain_log)
        assert intact is False
        assert idx == 2

    def test_single_line_deletion_detected(self, chain_log: Path) -> None:
        handler = _make_handler(chain_log)
        try:
            for i in range(5):
                _emit(handler, {"event": "audit.row", "i": i})
        finally:
            handler.close()

        lines = chain_log.read_text(encoding="utf-8").splitlines()
        # Remove line index 2 — the prev_hash on (now) line 2 (originally
        # line 3) no longer matches line 1's chain_hash.
        del lines[2]
        chain_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        intact, idx = verify_chain(chain_log)
        assert intact is False
        assert idx == 2

    def test_inserted_foreign_line_detected(self, chain_log: Path) -> None:
        handler_a = _make_handler(chain_log)
        try:
            for i in range(3):
                _emit(handler_a, {"event": "audit.row", "i": i})
        finally:
            handler_a.close()

        # Build a plausible-but-foreign record by hand. Its chain_hash is
        # computed with the *real* prev_hash so the line itself "looks"
        # consistent, but the prev_hash on the original line 2 then no
        # longer matches our injected chain_hash.
        lines = chain_log.read_text(encoding="utf-8").splitlines()
        anchor = json.loads(lines[1])
        chain_id = anchor["chain_id"]
        foreign = {"event": "audit.injected", "chain_id": chain_id}
        canonical = json.dumps(
            {k: v for k, v in foreign.items() if k != "chain_id"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        prev_hash = anchor["chain_hash"]
        new_chain_hash = hashlib.sha256(prev_hash.encode("ascii") + canonical).hexdigest()
        foreign["prev_hash"] = prev_hash
        foreign["chain_hash"] = new_chain_hash
        injected = json.dumps(foreign, ensure_ascii=False)
        lines.insert(2, injected)
        chain_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        intact, idx = verify_chain(chain_log)
        assert intact is False
        # The injected line itself verifies (we hashed honestly), so the
        # verifier flags the original line that followed (now at index 3)
        # whose prev_hash no longer matches the injected chain_hash.
        assert idx == 3

    def test_mixed_chain_id_detected(self, chain_log: Path) -> None:
        handler = _make_handler(chain_log)
        try:
            for i in range(4):
                _emit(handler, {"event": "audit.row", "i": i})
        finally:
            handler.close()

        lines = chain_log.read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[2])
        rec["chain_id"] = "ffffffffffffffffffffffffffffffff"
        lines[2] = json.dumps(rec, ensure_ascii=False)
        chain_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        intact, idx = verify_chain(chain_log)
        assert intact is False
        assert idx == 2

    def test_non_json_line_detected(self, chain_log: Path) -> None:
        handler = _make_handler(chain_log)
        try:
            for i in range(3):
                _emit(handler, {"event": "audit.row", "i": i})
        finally:
            handler.close()

        lines = chain_log.read_text(encoding="utf-8").splitlines()
        lines[1] = "this is not json at all"
        chain_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        intact, idx = verify_chain(chain_log)
        assert intact is False
        assert idx == 1

    def test_missing_chain_field_detected(self, chain_log: Path) -> None:
        handler = _make_handler(chain_log)
        try:
            for i in range(3):
                _emit(handler, {"event": "audit.row", "i": i})
        finally:
            handler.close()

        lines = chain_log.read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[1])
        del rec["chain_hash"]  # tamper: strip the integrity field
        lines[1] = json.dumps(rec, ensure_ascii=False)
        chain_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        intact, idx = verify_chain(chain_log)
        assert intact is False
        assert idx == 1


class TestRolloverStartsFreshChain:
    """``doRollover`` resets ``chain_id`` and re-anchors ``prev_hash`` to GENESIS."""

    def test_rollover_assigns_new_chain_id(self, chain_log: Path) -> None:
        handler = _make_handler(chain_log)
        try:
            _emit(handler, {"event": "audit.row", "i": 0})
            chain_id_before = handler._chain_id  # noqa: SLF001 — covered intentionally.
            handler.doRollover()
            chain_id_after = handler._chain_id  # noqa: SLF001
            _emit(handler, {"event": "audit.row", "i": 1})
        finally:
            handler.close()

        assert chain_id_after != chain_id_before
        # After rotation, the active file holds the new segment whose
        # first record must anchor at GENESIS again.
        records = _read_records(chain_log)
        assert records[0]["prev_hash"] == GENESIS_HASH
        assert records[0]["chain_id"] == chain_id_after

    def test_post_rotation_segment_verifies(self, chain_log: Path) -> None:
        handler = _make_handler(chain_log)
        try:
            _emit(handler, {"event": "audit.row", "i": 0})
            handler.doRollover()
            for i in range(3):
                _emit(handler, {"event": "audit.row", "i": i + 1})
        finally:
            handler.close()

        intact, idx = verify_chain(chain_log)
        assert intact is True
        assert idx == -1


class TestHandlerResilience:
    """A formatter explosion must never raise out of ``emit``."""

    def test_format_failure_is_swallowed(self, chain_log: Path) -> None:
        class _BoomFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:  # noqa: A003
                msg = "format failed"
                raise RuntimeError(msg)

        handler = HashChainHandler(chain_log)
        handler.setFormatter(_BoomFormatter())
        # ``handleError`` is the stdlib path; suppress the stderr noise.

        def _silent(record: logging.LogRecord) -> None:
            del record

        handler.handleError = _silent  # type: ignore[method-assign]
        try:
            # Must not propagate the formatter error.
            _emit(handler, {"event": "audit.row"})
        finally:
            handler.close()

        # Nothing written → file may be empty (or absent), and the
        # in-memory chain pointer never advanced past GENESIS.
        assert handler._prev_hash == GENESIS_HASH  # noqa: SLF001 — invariant under test.
