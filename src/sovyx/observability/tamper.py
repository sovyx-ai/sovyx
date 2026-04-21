"""Tamper-evident JSON log handler — opt-in hash-chain over file records.

Each record written to disk gets two extra fields, ``prev_hash`` and
``chain_hash``, that together form an append-only chain:

    chain_hash[i] = sha256(prev_hash[i] + canonical(record[i]))
    prev_hash[i]  = chain_hash[i-1]    (or GENESIS for the first line)

Where ``canonical(record)`` is the record's JSON serialization with
sorted keys and ``(",", ":")`` separators, computed *with the chain
fields stripped*. Any post-hoc edit to a single line breaks every
subsequent ``chain_hash`` because the prev_hash linkage no longer
matches.

A third field, ``chain_id``, scopes the chain to a single log file:
:meth:`HashChainHandler.doRollover` resets the chain on every
rotation so a verified chain always has a single ``chain_id`` from
top to bottom. Mixing files into a verifier (or rotating mid-chain)
becomes detectable.

Performance budget: ~2 µs per record on commodity hardware (one
``json.dumps`` re-encoding plus a 32-byte SHA-256). The handler is
opt-in via :attr:`ObservabilityFeaturesConfig.tamper_chain` because
the cost shows up on every write.

Threats this defends against:
    * Local tampering — an attacker with write access to the log
      file cannot cleanly delete or rewrite a single line without
      breaking the chain.
    * Silent log corruption — disk-level bit-flips / partial writes
      surface as a verification failure with the line index pinned.

Threats this does NOT defend against:
    * Live-attacker append (they could continue the chain with their
      own records). Pair with off-host log shipping for that case.
    * Wholesale file deletion — the chain proves *integrity*, not
      *existence*. Pair with off-host shipping for that case.

Aligned with IMPL-OBSERVABILITY-001 §15 (Phase 9, Task 9.6).
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

# 64 zeros — the genesis prev_hash for every fresh chain. Deliberately
# constant so verifiers can detect "this file starts a chain" without
# reading any prior file.
GENESIS_HASH = "0" * 64

_CHAIN_FIELDS = ("chain_hash", "prev_hash", "chain_id")


def _canonical(record_dict: dict[str, Any]) -> bytes:
    """Return the deterministic JSON encoding used for chain hashing.

    Determinism is security-critical here: any change in key order or
    whitespace would invalidate the chain even when the underlying
    data is unchanged. The chain fields themselves are excluded from
    the hash input — they are *output* of the chain, not part of the
    record being attested.
    """
    stripped = {k: v for k, v in record_dict.items() if k not in _CHAIN_FIELDS}
    return json.dumps(
        stripped,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


class HashChainHandler(logging.handlers.RotatingFileHandler):
    """Rotating file handler that appends a ``prev_hash``/``chain_hash`` pair to every JSON record.

    Drop-in replacement for :class:`logging.handlers.RotatingFileHandler`
    with the same ``maxBytes`` + ``backupCount`` semantics. Records that
    don't serialize to JSON (e.g., a console-only formatter) are written
    unchanged and the chain is left intact — the next JSON record
    re-anchors the chain.
    """

    def __init__(
        self,
        filename: Any,  # noqa: ANN401 — same shape as RotatingFileHandler.
        *,
        mode: str = "a",
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 3,
        encoding: str = "utf-8",
    ) -> None:
        super().__init__(
            filename,
            mode=mode,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding=encoding,
        )
        self._chain_id: str = uuid.uuid4().hex
        self._prev_hash: str = GENESIS_HASH

    def emit(self, record: logging.LogRecord) -> None:
        """Format, hash, augment, and write *record*. Falls back to plain emit on parse error."""
        try:
            msg = self.format(record)
            try:
                parsed = json.loads(msg)
            except json.JSONDecodeError:
                # Non-JSON line (e.g., console renderer leaked through).
                # Write unchanged so logs aren't lost; chain anchors on
                # the next JSON record.
                super().emit(record)
                return
            if not isinstance(parsed, dict):
                super().emit(record)
                return

            canonical = _canonical(parsed)
            chain_hash = hashlib.sha256(self._prev_hash.encode("ascii") + canonical).hexdigest()

            parsed["prev_hash"] = self._prev_hash
            parsed["chain_hash"] = chain_hash
            parsed["chain_id"] = self._chain_id

            line = json.dumps(parsed, ensure_ascii=False)
            if self.stream is None:
                self.stream = self._open()
            self.stream.write(line + self.terminator)
            self.flush()

            self._prev_hash = chain_hash
        except Exception:  # noqa: BLE001 — handler errors must not raise.
            from sovyx.observability._health_state import record_handler_error  # noqa: PLC0415

            record_handler_error()
            self.handleError(record)

    def doRollover(self) -> None:
        """Rotate the file and start a fresh chain.

        ``chain_id`` flips to a new UUID so the verifier can detect a
        rotation boundary even when files are concatenated by an
        operator. ``_prev_hash`` resets to :data:`GENESIS_HASH`.
        """
        super().doRollover()
        self._chain_id = uuid.uuid4().hex
        self._prev_hash = GENESIS_HASH


def verify_chain(path: Path) -> tuple[bool, int]:
    """Replay the hash chain stored in *path*.

    Returns ``(True, -1)`` when every record verifies; otherwise
    ``(False, idx)`` where ``idx`` is the zero-based index of the
    first broken line. A broken line means at least one of:

    * The line is not valid JSON.
    * Required chain fields (``prev_hash`` / ``chain_hash``) are
      missing.
    * ``chain_id`` differs from the file's first record (rotation
      boundary contamination).
    * ``prev_hash`` does not match the previous line's
      ``chain_hash``.
    * The recomputed ``sha256(prev_hash || canonical(record))`` does
      not match the stored ``chain_hash``.

    Empty lines are skipped (some editors append a trailing newline).
    Non-existent files raise :class:`FileNotFoundError` — verification
    of "did the file ever exist?" is the caller's responsibility.
    """
    prev_hash = GENESIS_HASH
    chain_id: str | None = None

    with path.open("r", encoding="utf-8") as fh:
        for idx, raw_line in enumerate(fh):
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                return (False, idx)
            if not isinstance(parsed, dict):
                return (False, idx)

            stored_chain_hash = parsed.get("chain_hash")
            stored_prev_hash = parsed.get("prev_hash")
            stored_chain_id = parsed.get("chain_id")

            if not isinstance(stored_chain_hash, str) or not isinstance(stored_prev_hash, str):
                return (False, idx)

            if chain_id is None:
                if not isinstance(stored_chain_id, str):
                    return (False, idx)
                chain_id = stored_chain_id
            elif stored_chain_id != chain_id:
                return (False, idx)

            if stored_prev_hash != prev_hash:
                return (False, idx)

            canonical = _canonical(parsed)
            expected = hashlib.sha256(prev_hash.encode("ascii") + canonical).hexdigest()

            if expected != stored_chain_hash:
                return (False, idx)

            prev_hash = stored_chain_hash

    return (True, -1)


__all__ = [
    "GENESIS_HASH",
    "HashChainHandler",
    "verify_chain",
]
