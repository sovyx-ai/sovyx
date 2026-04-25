"""GDPR-compliant consent ledger — append-only audit log per user (M3).

Ring 6 (Orchestration) compliance surface. Sovyx records every privacy-
relevant voice action (wake, listen, transcribe, store, share) in a
local-first append-only ledger so the user can:

* **See everything we have on them** via :meth:`ConsentLedger.history`
* **Be forgotten** via :meth:`ConsentLedger.forget` (records purged +
  tombstone written for audit trail)

The ledger is the local-first foundation for the GDPR Article 15 (Right
of Access), Article 17 (Right to Erasure), and Article 30 (Records of
Processing Activities) requirements. Sovyx's local-first architecture
means the ledger NEVER leaves the user's machine — so the right-of-
access call is a file read, not a vendor request, and the right-to-
erasure call is a file rewrite, not a 30-day vendor SLA.

Design invariants:

* **Append-only durable writes** — every ``append`` flushes + fsyncs
  before returning so a crash mid-call doesn't lose the record. The
  write is atomic at the line level (one ``write()`` per record;
  POSIX guarantees this is atomic for writes ≤ PIPE_BUF / page size,
  and JSONL records are tiny).
* **JSONL on disk** — one record per line, easy for operators to
  ``grep`` / ``jq`` / inspect without parsing tools. Each line is a
  complete self-describing JSON object.
* **No PII in the schema** — ``user_id`` is a hash (caller's
  responsibility to pass a stable opaque identifier; the ledger
  never sees raw user names). The ``context`` field is free-form for
  caller-relevant metadata but the caller MUST not leak PII into it.
* **Cross-process file lock** — concurrent processes (daemon +
  dashboard + doctor CLI) serialise via :func:`fcntl.flock` /
  :func:`msvcrt.locking` so the JSONL file never interleaves
  partial records.
* **Bounded growth** — the ledger rotates when it crosses
  :data:`_LEDGER_ROTATION_BYTES` (default 10 MiB). Old segments are
  archived with a timestamp suffix (``.{ts}.jsonl``) so the
  history-replay path can still walk them; only the active segment
  receives new appends.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §2.6
(Ring 6 consent ledger), §3.10 M3, GDPR Articles 15 / 17 / 30,
Speechmatics 2026 voice-AI compliance guide.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

logger = get_logger(__name__)


_LEDGER_ROTATION_BYTES = 10 * 1024 * 1024
"""Active-segment size threshold beyond which the ledger rotates to a
fresh file. 10 MiB ≈ 100 000 records at ~100 bytes each — enough for
years of low-traffic per-user history while staying easy to grep
without paginated tooling. The rotated segment is renamed to
``<basename>.<utc-timestamp>.jsonl`` so :meth:`ConsentLedger.history`
can still find it."""


class ConsentAction(StrEnum):
    """Privacy-relevant voice actions the ledger records.

    Closed enum — extending requires a deliberate ledger schema bump
    so dashboards / external auditors can rely on a stable taxonomy.

    Members:
        WAKE: Wake-word detector matched the configured trigger phrase.
        LISTEN: Audio capture started (the user's mic stream is
            being processed by the orchestrator).
        TRANSCRIBE: Speech-to-text engine produced a transcript from
            captured audio.
        STORE: Transcript / context was persisted to the brain
            (long-term memory).
        SHARE: Transcript / context was sent to an external service
            (cloud LLM, web tool, etc.). The ``context`` field
            should name the destination.
        DELETE: Right-to-erasure event — the ledger itself records
            when a user invokes :meth:`ConsentLedger.forget` so the
            audit trail survives the deletion (the tombstone is the
            ONLY record of that user remaining; everything else is
            purged).
    """

    WAKE = "wake"
    LISTEN = "listen"
    TRANSCRIBE = "transcribe"
    STORE = "store"
    SHARE = "share"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    """One entry in the consent ledger.

    Attributes:
        timestamp_utc: ISO-8601 UTC timestamp at append time. Second
            precision is sufficient for audit purposes (sub-second
            ordering is not legally meaningful and adds noise).
        user_id: Stable opaque identifier for the user. Caller must
            pass an already-hashed / pseudonymised value; the ledger
            never sees raw names. Empty string is permitted for
            anonymous-mode operation but disables history /
            forget per-user (would match every empty-id record).
        action: One of :class:`ConsentAction`.
        context: Free-form caller metadata. MUST NOT contain PII
            (raw transcript text, real names, exact timestamps with
            session-correlation potential). Validated by
            :func:`_assert_no_obvious_pii_in_context`.
    """

    timestamp_utc: str
    user_id: str
    action: ConsentAction
    context: Mapping[str, Any]

    def to_jsonl_line(self) -> str:
        """Serialise to a single JSONL line (no trailing newline)."""
        return json.dumps(
            {
                "timestamp_utc": self.timestamp_utc,
                "user_id": self.user_id,
                "action": self.action.value,
                "context": dict(self.context),
            },
            ensure_ascii=False,
            sort_keys=True,
        )


_OBVIOUS_PII_KEYS: frozenset[str] = frozenset(
    {
        "email",
        "phone",
        "address",
        "ssn",
        "credit_card",
        "ip_address",
        "real_name",
        "raw_transcript",
        "transcript",
    }
)
"""Context keys that obviously carry PII. Reject at append-time so a
caller bug doesn't leak personal data into the ledger. The list is
deliberately conservative — operators should NEVER need to put any
of these in context. Add a key here if a new PII class is
identified; never remove (loosens the contract)."""


def _assert_no_obvious_pii_in_context(context: Mapping[str, Any]) -> None:
    """Reject contexts with obviously-PII keys at append-time.

    Defensive — the ledger is local-first so leaked PII isn't
    exfiltrated, but the user's own ``forget`` call needs to actually
    forget, and PII in context defeats that contract. Raises ValueError
    naming the offending key so the caller fixes the call site.
    """
    for key in context:
        if key.lower() in _OBVIOUS_PII_KEYS:
            msg = (
                f"context contains obvious-PII key {key!r}; "
                f"hash / pseudonymise before passing to ConsentLedger.append "
                f"(see _OBVIOUS_PII_KEYS for the rejected catalog)"
            )
            raise ValueError(msg)


class ConsentLedger:
    """Append-only GDPR consent ledger backed by a local JSONL file.

    Single-process safe via an internal :class:`threading.Lock`;
    cross-process safe via per-write file lock (POSIX ``flock`` /
    Windows ``msvcrt.locking``). Tests inject a custom clock for
    deterministic timestamps.

    Args:
        path: Absolute path to the ledger file (e.g.
            ``data_dir/voice_consent.jsonl``). The parent directory
            is created on first append. The active segment lives at
            ``path``; rotated segments at ``<path>.<utc-timestamp>``.
        clock: Optional UTC ``datetime`` factory for deterministic
            timestamp testing. Defaults to ``datetime.now(UTC)``.
        rotation_bytes: Override the segment size threshold. Tests
            pass a tiny value to exercise rotation without producing
            10 MiB of records.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: Any = None,  # noqa: ANN401 — Callable[[], datetime] but Any keeps the test surface tiny
        rotation_bytes: int = _LEDGER_ROTATION_BYTES,
    ) -> None:
        self._path = Path(path)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._rotation_bytes = rotation_bytes
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """Absolute path to the active ledger segment."""
        return self._path

    def append(
        self,
        *,
        user_id: str,
        action: ConsentAction,
        context: Mapping[str, Any] | None = None,
    ) -> ConsentRecord:
        """Append one record + fsync. Returns the persisted record.

        The full path: validate context → format ISO timestamp →
        serialise → acquire process + file lock → write line + fsync
        → release locks → maybe-rotate. The fsync is durability
        critical for legal compliance (a record that loses to a
        crash is a missing audit entry).

        Raises:
            ValueError: ``context`` contains an obviously-PII key
                (see :data:`_OBVIOUS_PII_KEYS`).
            OSError: Underlying filesystem failure (disk full,
                permission denied). Logged + propagated so the
                caller can surface it.
        """
        ctx = dict(context or {})
        _assert_no_obvious_pii_in_context(ctx)
        record = ConsentRecord(
            timestamp_utc=self._clock().replace(microsecond=0).isoformat(),
            user_id=user_id,
            action=action,
            context=ctx,
        )
        line = record.to_jsonl_line() + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._write_with_file_lock(line)
            self._maybe_rotate()
        return record

    def history(self, user_id: str) -> list[ConsentRecord]:
        """Return every record for ``user_id`` across all segments.

        Walks the active segment + every rotated segment matching
        ``<basename>.*.jsonl`` (so a years-old archived segment is
        still discoverable for the GDPR Article 15 right-of-access
        call). Records are returned in chronological order across
        segments — rotated segments precede the active one (the
        rotated segment's timestamp suffix is monotonically
        increasing, so glob + sort by name approximates timestamp
        order without parsing).

        Empty list when the ledger doesn't exist yet (first call
        on a fresh data_dir) or the user has no records.
        """
        records: list[ConsentRecord] = []
        with self._lock:
            for segment in self._iter_segments():
                records.extend(self._read_segment_filtered(segment, user_id=user_id))
        return records

    def forget(self, user_id: str) -> int:
        """GDPR Article 17 — purge every record for ``user_id``.

        Walks every segment, rewrites in-place omitting any record
        whose ``user_id`` matches, then appends a single
        :data:`ConsentAction.DELETE` tombstone so the audit trail
        records that the deletion happened (without the
        tombstone, an external auditor couldn't distinguish
        "user was never recorded" from "user was forgotten").

        The rewrite is atomic per segment (write to ``<segment>.tmp``,
        then ``os.replace``) so a crash mid-rewrite leaves the
        original segment intact.

        Returns:
            Number of records purged (excludes the tombstone).
        """
        purged_total = 0
        with self._lock:
            for segment in self._iter_segments():
                purged_total += self._rewrite_segment_excluding(
                    segment,
                    user_id=user_id,
                )
        # Tombstone goes through the normal append path so it's also
        # subject to PII validation + fsync + locking.
        self.append(
            user_id=user_id,
            action=ConsentAction.DELETE,
            context={"purged_record_count": purged_total},
        )
        logger.warning(
            "voice.consent.user_forgotten",
            **{
                "voice.user_id_hash_prefix": user_id[:8] if user_id else "",
                "voice.purged_record_count": purged_total,
            },
        )
        return purged_total

    # ── internals ─────────────────────────────────────────────────────

    def _write_with_file_lock(self, line: str) -> None:
        """Append ``line`` to the active segment under a cross-process lock.

        On POSIX uses ``fcntl.flock`` (advisory but universally honoured
        by Sovyx daemon + dashboard + doctor CLI); on Windows uses
        ``msvcrt.locking`` against the byte we're about to write. Both
        styles release on file close. fsync is unconditional after
        the write — a crash before fsync would lose the most-recent
        record, which is unacceptable for audit semantics.
        """
        with open(self._path, "a", encoding="utf-8") as fh:  # noqa: PTH123, FURB101 — append+lock pattern
            self._acquire_file_lock(fh)
            try:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                self._release_file_lock(fh)
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                os.chmod(self._path, 0o600)

    @staticmethod
    def _acquire_file_lock(fh: Any) -> None:  # noqa: ANN401 — file handle protocol varies by platform
        if sys.platform == "win32":
            try:
                import msvcrt

                # Lock 1 byte at the current write position. Blocking call
                # — the daemon and dashboard write infrequently enough
                # that the wait is negligible.
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            except OSError:
                # On Windows, locking can fail in concurrent dev / test
                # scenarios; the per-write fsync still bounds data loss
                # to one in-flight record. Log + continue.
                logger.debug("voice.consent.win_lock_skipped")
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    @staticmethod
    def _release_file_lock(fh: Any) -> None:  # noqa: ANN401
        if sys.platform == "win32":
            try:
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _maybe_rotate(self) -> None:
        """Rename the active segment if it crosses the rotation threshold.

        Called after every append. A rotated segment is renamed in
        place to ``<basename>.<unix-ts-int>.jsonl`` — the integer
        timestamp suffix sorts lexicographically in the same order
        as chronologically (until 2286), so a glob+sort gives
        ordered segments without parsing.
        """
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size < self._rotation_bytes:
            return
        # Nanosecond resolution prevents collision when multiple
        # rotations happen within the same second (test scenarios with
        # tiny rotation_bytes; theoretically possible in production
        # under burst loads on a tiny ledger). Bare seconds would let
        # ``os.replace`` silently overwrite the previously-rotated
        # segment, losing all its records — a GDPR audit-trail bug.
        ts_ns = time.time_ns()
        rotated = self._path.with_suffix(f".{ts_ns}{self._path.suffix}")
        try:
            os.replace(self._path, rotated)
            logger.info(
                "voice.consent.segment_rotated",
                **{
                    "voice.from_path": str(self._path),
                    "voice.to_path": str(rotated),
                    "voice.size_bytes": size,
                    "voice.threshold_bytes": self._rotation_bytes,
                },
            )
        except OSError as exc:
            # Rotation failure is non-fatal — the active segment just
            # keeps growing. Operator can rotate manually via cron.
            logger.warning(
                "voice.consent.rotation_failed",
                **{
                    "voice.from_path": str(self._path),
                    "voice.error": str(exc),
                    "voice.error_type": type(exc).__name__,
                },
            )

    def _iter_segments(self) -> Iterator[Path]:
        """Yield the active segment + every rotated segment, sorted."""
        rotated = sorted(self._path.parent.glob(f"{self._path.stem}.*{self._path.suffix}"))
        yield from rotated
        if self._path.exists():
            yield self._path

    @staticmethod
    def _read_segment_filtered(segment: Path, *, user_id: str) -> list[ConsentRecord]:
        """Read ``segment`` and return records matching ``user_id``."""
        out: list[ConsentRecord] = []
        try:
            with open(segment, encoding="utf-8") as fh:  # noqa: PTH123
                for raw in fh:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        # Don't crash the whole replay on one corrupt
                        # line — log + skip. A corrupt line is itself
                        # an audit anomaly the operator should see.
                        logger.warning(
                            "voice.consent.corrupt_line_skipped",
                            **{
                                "voice.segment": str(segment),
                                "voice.line_prefix": line[:80],
                            },
                        )
                        continue
                    if data.get("user_id") != user_id:
                        continue
                    try:
                        out.append(
                            ConsentRecord(
                                timestamp_utc=str(data["timestamp_utc"]),
                                user_id=str(data["user_id"]),
                                action=ConsentAction(data["action"]),
                                context=dict(data.get("context", {})),
                            ),
                        )
                    except (KeyError, ValueError):
                        logger.warning(
                            "voice.consent.malformed_record_skipped",
                            **{
                                "voice.segment": str(segment),
                                "voice.line_prefix": line[:80],
                            },
                        )
        except OSError:
            return []
        return out

    def _rewrite_segment_excluding(
        self,
        segment: Path,
        *,
        user_id: str,
    ) -> int:
        """Rewrite ``segment`` omitting every record with ``user_id``.

        Returns the count of records EXCLUDED. Atomic via tempfile +
        os.replace so a crash mid-rewrite doesn't lose the original
        segment.
        """
        tmp = segment.with_suffix(segment.suffix + ".tmp")
        excluded = 0
        try:
            with (
                open(segment, encoding="utf-8") as src,  # noqa: PTH123
                open(tmp, "w", encoding="utf-8") as dst,  # noqa: PTH123
            ):
                for raw in src:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        # Preserve corrupt lines verbatim — the user
                        # is forgetting their own data, not the
                        # audit anomalies in someone else's record.
                        dst.write(raw)
                        continue
                    if data.get("user_id") == user_id:
                        excluded += 1
                        continue
                    dst.write(raw)
                dst.flush()
                os.fsync(dst.fileno())
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            return 0
        try:
            os.replace(tmp, segment)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            return 0
        return excluded


__all__ = [
    "ConsentAction",
    "ConsentLedger",
    "ConsentRecord",
]
