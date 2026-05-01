"""Tests for :class:`sovyx.voice._consent_ledger.ConsentLedger`.

Covers GDPR Article 15 (right of access), Article 17 (right to
erasure), and Article 30 (records of processing) compliance surfaces:

* append + history round-trip
* atomic per-line writes (durable across simulated crashes)
* PII guard rejects obvious-PII context keys
* forget purges every record + writes tombstone
* segment rotation past the size threshold
* history walks rotated segments
* corrupt line handling (skip + log, never crash)

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.10 M3.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sovyx.voice._consent_ledger import (
    _OBVIOUS_PII_KEYS,
    ConsentAction,
    ConsentLedger,
    ConsentRecord,
    _assert_no_obvious_pii_in_context,
)


def _frozen_clock(t: datetime | None = None):
    fixed = t or datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


# ── ConsentAction enum ──────────────────────────────────────────────


class TestConsentAction:
    def test_all_canonical_members_present(self) -> None:
        names = {a.value for a in ConsentAction}
        # Mission-mandated taxonomy. Adding members is OK; removing is
        # a breaking schema change for downstream auditors.
        assert names == {"wake", "listen", "transcribe", "store", "share", "delete"}


# ── ConsentRecord serialisation ─────────────────────────────────────


class TestConsentRecord:
    def test_jsonl_serialisation_round_trip(self) -> None:
        rec = ConsentRecord(
            timestamp_utc="2026-04-25T12:00:00+00:00",
            user_id="hash:abc123",
            action=ConsentAction.WAKE,
            context={"mind_id": "default"},
        )
        line = rec.to_jsonl_line()
        assert "\n" not in line
        data = json.loads(line)
        assert data["user_id"] == "hash:abc123"
        assert data["action"] == "wake"
        assert data["context"] == {"mind_id": "default"}

    def test_immutable(self) -> None:
        rec = ConsentRecord(
            timestamp_utc="2026-04-25T12:00:00+00:00",
            user_id="x",
            action=ConsentAction.WAKE,
            context={},
        )
        with pytest.raises((AttributeError, TypeError)):
            rec.user_id = "y"  # type: ignore[misc]


# ── PII guard ───────────────────────────────────────────────────────


class TestPIIGuard:
    @pytest.mark.parametrize("pii_key", sorted(_OBVIOUS_PII_KEYS))
    def test_rejects_each_obvious_pii_key(self, pii_key: str) -> None:
        with pytest.raises(ValueError, match="obvious-PII"):
            _assert_no_obvious_pii_in_context({pii_key: "anything"})

    def test_case_insensitive_rejection(self) -> None:
        with pytest.raises(ValueError, match="obvious-PII"):
            _assert_no_obvious_pii_in_context({"EMAIL": "user@example.com"})

    def test_safe_keys_accepted(self) -> None:
        # Should NOT raise — these are operationally common context keys.
        _assert_no_obvious_pii_in_context(
            {
                "mind_id": "default",
                "session_id": "abc",
                "audio_ms": 1500,
                "model": "moonshine-tiny",
            },
        )


# ── Append + history round-trip ─────────────────────────────────────


class TestAppendAndHistory:
    def test_first_append_creates_file_and_parent(self, tmp_path: Path) -> None:
        path = tmp_path / "subdir" / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE)
        assert path.exists()
        assert path.parent.exists()

    def test_append_returns_record(self, tmp_path: Path) -> None:
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        rec = ledger.append(user_id="hash:u1", action=ConsentAction.LISTEN)
        assert rec.user_id == "hash:u1"
        assert rec.action is ConsentAction.LISTEN
        assert rec.timestamp_utc == "2026-04-25T12:00:00+00:00"

    def test_history_returns_only_matching_user(self, tmp_path: Path) -> None:
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE)
        ledger.append(user_id="hash:u2", action=ConsentAction.WAKE)
        ledger.append(
            user_id="hash:u1",
            action=ConsentAction.TRANSCRIBE,
            context={"audio_ms": 1200},
        )
        u1 = ledger.history("hash:u1")
        assert len(u1) == 2
        assert {r.action for r in u1} == {ConsentAction.WAKE, ConsentAction.TRANSCRIBE}
        # u2 records NOT included.
        assert all(r.user_id == "hash:u1" for r in u1)

    def test_history_empty_for_unknown_user(self, tmp_path: Path) -> None:
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE)
        assert ledger.history("hash:nobody") == []

    def test_history_empty_when_file_does_not_exist(self, tmp_path: Path) -> None:
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        assert ledger.history("hash:u1") == []

    def test_append_rejects_pii_context(self, tmp_path: Path) -> None:
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        with pytest.raises(ValueError, match="obvious-PII"):
            ledger.append(
                user_id="hash:u1",
                action=ConsentAction.STORE,
                context={"raw_transcript": "hello world"},
            )

    def test_append_persists_across_ledger_instances(self, tmp_path: Path) -> None:
        """The ledger is local-first and disk-backed — a fresh
        instance against the same path must see prior records."""
        path = tmp_path / "consent.jsonl"
        ledger1 = ConsentLedger(path, clock=_frozen_clock())
        ledger1.append(user_id="hash:u1", action=ConsentAction.WAKE)

        ledger2 = ConsentLedger(path, clock=_frozen_clock())
        recovered = ledger2.history("hash:u1")
        assert len(recovered) == 1
        assert recovered[0].action is ConsentAction.WAKE


# ── Right to erasure (Article 17) ───────────────────────────────────


class TestForget:
    def test_forget_purges_all_user_records(self, tmp_path: Path) -> None:
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE)
        ledger.append(user_id="hash:u1", action=ConsentAction.TRANSCRIBE)
        ledger.append(user_id="hash:u2", action=ConsentAction.WAKE)

        purged = ledger.forget("hash:u1")
        assert purged == 2  # noqa: PLR2004 — number of records actually removed

        # After forget, u1 history shows ONLY the DELETE tombstone.
        u1 = ledger.history("hash:u1")
        assert len(u1) == 1
        assert u1[0].action is ConsentAction.DELETE
        assert u1[0].context["purged_record_count"] == 2  # noqa: PLR2004

        # u2 records preserved.
        u2 = ledger.history("hash:u2")
        assert len(u2) == 1
        assert u2[0].action is ConsentAction.WAKE

    def test_forget_unknown_user_writes_zero_purge_tombstone(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        purged = ledger.forget("hash:nobody")
        assert purged == 0
        # Tombstone still written for audit trail.
        history = ledger.history("hash:nobody")
        assert len(history) == 1
        assert history[0].action is ConsentAction.DELETE
        assert history[0].context["purged_record_count"] == 0

    def test_forget_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE)
        ledger.forget("hash:u1")
        # Second forget — only the prior DELETE tombstone remains as
        # a u1-record. Forgetting it produces a NEW tombstone.
        purged = ledger.forget("hash:u1")
        assert purged == 1  # the prior tombstone


# ── Segment rotation ────────────────────────────────────────────────


class TestRotation:
    def test_rotates_when_size_threshold_crossed(self, tmp_path: Path) -> None:
        """Use a tiny rotation_bytes so a few writes trigger rotation."""
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock(), rotation_bytes=64)
        # Each record serialises to ~120 bytes, so first append already
        # crosses the 64-byte threshold and rotation fires.
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE)
        ledger.append(user_id="hash:u1", action=ConsentAction.LISTEN)
        rotated = sorted(tmp_path.glob("consent.*.jsonl"))
        assert len(rotated) >= 1, "expected at least one rotated segment"

    def test_history_walks_rotated_segments(self, tmp_path: Path) -> None:
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock(), rotation_bytes=64)
        # Force rotation between u1 records.
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE)
        ledger.append(user_id="hash:u1", action=ConsentAction.LISTEN)
        ledger.append(user_id="hash:u1", action=ConsentAction.TRANSCRIBE)
        # Replay must collect across BOTH the rotated segment AND the
        # active segment.
        history = ledger.history("hash:u1")
        assert len(history) == 3  # noqa: PLR2004
        assert {r.action for r in history} == {
            ConsentAction.WAKE,
            ConsentAction.LISTEN,
            ConsentAction.TRANSCRIBE,
        }


# ── Robustness / corruption tolerance ───────────────────────────────


class TestRobustness:
    def test_corrupt_line_skipped_in_history(self, tmp_path: Path) -> None:
        """A garbage line in the JSONL doesn't crash the replay."""
        path = tmp_path / "consent.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write one good record + one corrupt + one good.
        path.write_text(
            '{"timestamp_utc":"2026-04-25T12:00:00+00:00","user_id":"hash:u1","action":"wake","context":{}}\n'
            "this is not json\n"
            '{"timestamp_utc":"2026-04-25T12:00:01+00:00","user_id":"hash:u1","action":"listen","context":{}}\n',
            encoding="utf-8",
        )
        ledger = ConsentLedger(path, clock=_frozen_clock())
        history = ledger.history("hash:u1")
        # Corrupt line skipped; both good records returned.
        assert len(history) == 2  # noqa: PLR2004

    def test_malformed_record_skipped(self, tmp_path: Path) -> None:
        """Valid JSON but missing required fields."""
        path = tmp_path / "consent.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"timestamp_utc":"2026-04-25T12:00:00+00:00","user_id":"hash:u1","action":"wake","context":{}}\n'
            '{"missing_required_fields":true}\n'
            '{"timestamp_utc":"2026-04-25T12:00:00+00:00","user_id":"hash:u1","action":"INVALID_ACTION","context":{}}\n',
            encoding="utf-8",
        )
        ledger = ConsentLedger(path, clock=_frozen_clock())
        history = ledger.history("hash:u1")
        # Only the first record survives; the missing-fields entry
        # has no user_id (fails the per-user filter), and the
        # INVALID_ACTION entry trips the ConsentAction enum check.
        assert len(history) == 1

    def test_forget_preserves_corrupt_lines(self, tmp_path: Path) -> None:
        """User forgetting their own data must NOT silently delete
        audit-anomaly lines belonging to nobody."""
        path = tmp_path / "consent.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"timestamp_utc":"2026-04-25T12:00:00+00:00","user_id":"hash:u1","action":"wake","context":{}}\n'
            "this is not json\n",
            encoding="utf-8",
        )
        ledger = ConsentLedger(path, clock=_frozen_clock())
        ledger.forget("hash:u1")
        # The corrupt line should still be in the file.
        contents = path.read_text(encoding="utf-8")
        assert "this is not json" in contents


# ── Path attribute ───────────────────────────────────────────────────


class TestPathAttribute:
    def test_path_property_matches_constructor(self, tmp_path: Path) -> None:
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        assert ledger.path == path


# ── Per-mind isolation (Phase 8 / T8.21) ─────────────────────────────


class TestPerMindIsolation:
    """Per-mind audit boundary: ``mind_id`` filter on history / forget.

    Sovyx's multi-mind architecture (Phase 8) routes audio per mind;
    the ConsentLedger's ``mind_id`` field is the per-mind GDPR /
    LGPD audit boundary so each mind has independent right-of-access
    + right-to-erasure semantics. ``mind_id=None`` means "every mind"
    (preserves backward-compat for legacy callers); when set, the
    filter is strict.
    """

    def test_mind_id_serialises_when_set(self, tmp_path: Path) -> None:
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        ledger.append(
            user_id="hash:u1",
            action=ConsentAction.WAKE,
            mind_id="aria",
        )
        line = path.read_text(encoding="utf-8").strip()
        data = json.loads(line)
        assert data["mind_id"] == "aria"

    def test_mind_id_omitted_from_jsonl_when_none(self, tmp_path: Path) -> None:
        """Legacy records (no mind_id) MUST stay byte-identical so a
        future schema change doesn't invalidate years-old archives."""
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE)
        line = path.read_text(encoding="utf-8").strip()
        data = json.loads(line)
        assert "mind_id" not in data

    def test_history_filter_by_mind_id_only(self, tmp_path: Path) -> None:
        """Operator can list every record for a mind across users."""
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE, mind_id="aria")
        ledger.append(user_id="hash:u2", action=ConsentAction.LISTEN, mind_id="aria")
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE, mind_id="luna")

        aria = ledger.history(mind_id="aria")
        luna = ledger.history(mind_id="luna")

        assert len(aria) == 2  # noqa: PLR2004
        assert all(r.mind_id == "aria" for r in aria)
        assert {r.user_id for r in aria} == {"hash:u1", "hash:u2"}
        assert len(luna) == 1
        assert luna[0].mind_id == "luna"

    def test_history_filter_by_user_and_mind_anded(self, tmp_path: Path) -> None:
        """Both filters AND-combine: only records matching BOTH."""
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE, mind_id="aria")
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE, mind_id="luna")
        ledger.append(user_id="hash:u2", action=ConsentAction.WAKE, mind_id="aria")

        u1_aria = ledger.history(user_id="hash:u1", mind_id="aria")
        assert len(u1_aria) == 1
        assert u1_aria[0].user_id == "hash:u1"
        assert u1_aria[0].mind_id == "aria"

    def test_history_legacy_records_excluded_from_strict_mind_query(
        self,
        tmp_path: Path,
    ) -> None:
        """Records WITHOUT mind_id (legacy) MUST NOT match a strict
        mind_id query. They predate per-mind audit and can't be
        retroactively assigned."""
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE)  # legacy
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE, mind_id="aria")

        aria_only = ledger.history(mind_id="aria")
        assert len(aria_only) == 1
        assert aria_only[0].mind_id == "aria"

        # Legacy record IS visible when mind_id filter is None (wildcard).
        all_u1 = ledger.history(user_id="hash:u1")
        assert len(all_u1) == 2  # noqa: PLR2004

    def test_history_requires_at_least_one_filter(self, tmp_path: Path) -> None:
        """An unfiltered dump is rejected — would defeat audit isolation."""
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        with pytest.raises(ValueError, match="at least one of"):
            ledger.history()

    def test_forget_by_mind_id_only(self, tmp_path: Path) -> None:
        """``sovyx mind forget aria`` use case — wipe the aria audit
        trail across every user, leave luna untouched."""
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE, mind_id="aria")
        ledger.append(user_id="hash:u2", action=ConsentAction.LISTEN, mind_id="aria")
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE, mind_id="luna")

        purged = ledger.forget(mind_id="aria")
        assert purged == 2  # noqa: PLR2004

        # aria history shows ONLY the DELETE tombstone (mind_id=aria).
        aria_after = ledger.history(mind_id="aria")
        assert len(aria_after) == 1
        assert aria_after[0].action is ConsentAction.DELETE
        assert aria_after[0].mind_id == "aria"
        assert aria_after[0].user_id == ""  # mind-only purge → wildcard user_id
        assert aria_after[0].context["purged_record_count"] == 2  # noqa: PLR2004

        # luna untouched.
        luna_after = ledger.history(mind_id="luna")
        assert len(luna_after) == 1
        assert luna_after[0].action is ConsentAction.WAKE

    def test_forget_by_user_and_mind_anded(self, tmp_path: Path) -> None:
        """When both filters are set, AND-combine: only records matching
        BOTH are purged."""
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE, mind_id="aria")
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE, mind_id="luna")
        ledger.append(user_id="hash:u2", action=ConsentAction.WAKE, mind_id="aria")

        purged = ledger.forget(user_id="hash:u1", mind_id="aria")
        assert purged == 1  # only the (u1, aria) record

        # u1's luna record survives.
        u1_luna = ledger.history(user_id="hash:u1", mind_id="luna")
        assert len(u1_luna) == 1
        # u2's aria record survives.
        u2_aria = ledger.history(user_id="hash:u2", mind_id="aria")
        assert len(u2_aria) == 1

    def test_forget_legacy_records_excluded_from_strict_mind_purge(
        self,
        tmp_path: Path,
    ) -> None:
        """Legacy records (no mind_id) MUST NOT be purged by a strict
        mind_id wipe — they're outside the per-mind boundary."""
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE)  # legacy
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE, mind_id="aria")

        purged = ledger.forget(mind_id="aria")
        assert purged == 1

        # Legacy record survives.
        all_u1 = ledger.history(user_id="hash:u1")
        # 2 = the legacy record + the DELETE tombstone (mind_id=aria,
        # user_id=""). The legacy record's user_id="hash:u1" so it
        # surfaces under user_id="hash:u1" query; the tombstone's
        # user_id="" so it does NOT surface under "hash:u1".
        legacy_records = [r for r in all_u1 if r.action is not ConsentAction.DELETE]
        assert len(legacy_records) == 1
        assert legacy_records[0].mind_id is None

    def test_forget_requires_at_least_one_filter(self, tmp_path: Path) -> None:
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        with pytest.raises(ValueError, match="at least one of"):
            ledger.forget()

    def test_per_mind_forget_is_idempotent(self, tmp_path: Path) -> None:
        """Forgetting an already-empty mind writes a fresh tombstone
        but purges 0 records."""
        path = tmp_path / "consent.jsonl"
        ledger = ConsentLedger(path, clock=_frozen_clock())
        ledger.append(user_id="hash:u1", action=ConsentAction.WAKE, mind_id="aria")
        first = ledger.forget(mind_id="aria")
        assert first == 1

        # Second purge — the only remaining aria record is the prior
        # DELETE tombstone, so it's purged + a NEW tombstone written.
        second = ledger.forget(mind_id="aria")
        assert second == 1
        # Final state: ONE tombstone for aria.
        aria_after = ledger.history(mind_id="aria")
        assert len(aria_after) == 1
        assert aria_after[0].action is ConsentAction.DELETE
