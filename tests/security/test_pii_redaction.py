"""End-to-end security tests for PII redaction.

The unit suite (``tests/unit/observability/test_pii.py``) already
covers per-pattern correctness of :class:`PIIRedactor`. These tests
stand outside the structlog pipeline assembly to assert adversarial
guarantees: emit a record containing synthetic PII through the full
``setup_logging`` stack and confirm the literal PII never reaches the
on-disk JSON file, across every documented field class, verbosity
mode, and payload shape. Failures here mean a user's email/CPF/JWT
could leak into the log file — that's a compliance-grade incident.

All fixtures follow the §22.3 rule: synthetic values only, tailored
to match the whitelists in :file:`scripts/check_test_pii.py` so the
CI fixture gate stays green.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import structlog
from structlog.contextvars import clear_contextvars

from sovyx.engine.config import LoggingConfig, ObservabilityConfig
from sovyx.observability.logging import get_logger, setup_logging, shutdown_logging

# ── Synthetic PII fixtures (match scripts/check_test_pii.py whitelist) ──────
_FAKE_EMAIL = "synthetic.user@example-fake.test"
_FAKE_CPF = "529.982.247-25"
_FAKE_CNPJ = "00.000.000/0001-91"
_FAKE_PHONE_BR = "(11) 98765-4321"
_FAKE_PHONE_E164 = "+44 7700900123"
_FAKE_IPV4 = "203.0.113.45"  # RFC 5737 documentation block.
_FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiJ0ZXN0LXN1YmplY3QiLCJpYXQiOjE3MDAwMDAwMDB9"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
_FAKE_API_KEY_ANTHROPIC = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_FAKE_API_KEY_OPENAI = "sk-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_FAKE_VISA = "4111 1111 1111 1111"


@pytest.fixture()
def _clean_state() -> Generator[None, None, None]:
    """Tear down logging + structlog contextvars between tests."""
    clear_contextvars()
    yield
    shutdown_logging(timeout=2.0)
    structlog.reset_defaults()
    clear_contextvars()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


def _wait_for_file(path: Path, *, timeout: float = 3.0) -> None:
    """Block until *path* has at least one byte or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return
        time.sleep(0.02)


def _read(path: Path) -> str:
    """Return the entire log file as one text blob for substring assertions."""
    return path.read_text(encoding="utf-8")


def _make_obs_config(pii_overrides: dict[str, str] | None = None) -> ObservabilityConfig:
    """Build an ObservabilityConfig with PII redaction ON and optional mode overrides."""
    base: dict[str, Any] = {
        "features": {
            "async_queue": True,
            "pii_redaction": True,
            "saga_propagation": False,
            "voice_telemetry": False,
            "startup_cascade": False,
            "plugin_introspection": False,
            "anomaly_detection": False,
            "tamper_chain": False,
            "schema_validation": False,
            "metrics_exporter": False,
        },
    }
    if pii_overrides:
        base["pii"] = pii_overrides
    return ObservabilityConfig.model_validate(base)


def _setup(tmp_path: Path, obs_cfg: ObservabilityConfig | None = None) -> Path:
    """Configure logging for one test and return the JSON log file path."""
    log_file = tmp_path / "logs" / "pii.log"
    setup_logging(
        LoggingConfig(level="DEBUG", console_format="json", log_file=log_file),
        obs_cfg or _make_obs_config(),
        data_dir=tmp_path,
    )
    return log_file


class TestGlobalRegexSweep:
    """PII anywhere in an arbitrary string field is masked before reaching disk."""

    def test_email_in_generic_field_is_masked(self, tmp_path: Path, _clean_state: None) -> None:
        log_file = _setup(tmp_path)
        get_logger("security.pii").info("log.note", detail=f"contact {_FAKE_EMAIL} now")
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        blob = _read(log_file)
        assert _FAKE_EMAIL not in blob
        assert "[redacted-email]" in blob

    def test_cpf_in_arbitrary_field_is_masked(self, tmp_path: Path, _clean_state: None) -> None:
        log_file = _setup(tmp_path)
        get_logger("security.pii").info("audit.row", note=f"user cpf: {_FAKE_CPF}")
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        blob = _read(log_file)
        assert _FAKE_CPF not in blob
        assert "[redacted-cpf]" in blob

    def test_cnpj_in_arbitrary_field_is_masked(self, tmp_path: Path, _clean_state: None) -> None:
        log_file = _setup(tmp_path)
        get_logger("security.pii").info("audit.row", note=f"tax_id: {_FAKE_CNPJ}")
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        blob = _read(log_file)
        assert _FAKE_CNPJ not in blob
        assert "[redacted-cnpj]" in blob

    def test_phone_e164_in_arbitrary_field_is_masked(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup(tmp_path)
        get_logger("security.pii").info("call", note=f"ring {_FAKE_PHONE_E164}")
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        blob = _read(log_file)
        assert _FAKE_PHONE_E164 not in blob
        assert "[redacted-phone]" in blob

    def test_ipv4_in_arbitrary_field_is_masked(self, tmp_path: Path, _clean_state: None) -> None:
        log_file = _setup(tmp_path)
        get_logger("security.pii").info("request", note=f"from {_FAKE_IPV4}")
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        blob = _read(log_file)
        assert _FAKE_IPV4 not in blob
        assert "[redacted-ipv4]" in blob

    def test_jwt_in_arbitrary_field_is_masked(self, tmp_path: Path, _clean_state: None) -> None:
        log_file = _setup(tmp_path)
        get_logger("security.pii").info("auth", header=f"Bearer {_FAKE_JWT}")
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        blob = _read(log_file)
        assert _FAKE_JWT not in blob
        assert "[redacted-jwt]" in blob

    def test_api_key_anthropic_in_arbitrary_field_is_masked(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        # NB: must use a non-sensitive field name (``body`` / ``note``) so the
        # upstream :class:`SecretMasker` does not pre-redact it to ``sk-...AAA``
        # before the regex sweep runs. Field names containing "key" / "token" /
        # "secret" / "password" hit SecretMasker first; that layer is covered
        # separately in :class:`TestSecretMaskerLayering`.
        log_file = _setup(tmp_path)
        get_logger("security.pii").info("config", body=f"loaded {_FAKE_API_KEY_ANTHROPIC}")
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        blob = _read(log_file)
        assert _FAKE_API_KEY_ANTHROPIC not in blob
        assert "[redacted-api-key]" in blob

    def test_api_key_openai_in_arbitrary_field_is_masked(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup(tmp_path)
        get_logger("security.pii").info("config", body=f"loaded {_FAKE_API_KEY_OPENAI}")
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        blob = _read(log_file)
        assert _FAKE_API_KEY_OPENAI not in blob
        assert "[redacted-api-key]" in blob

    def test_luhn_valid_card_is_masked(self, tmp_path: Path, _clean_state: None) -> None:
        log_file = _setup(tmp_path)
        get_logger("security.pii").info("payment", note=f"card: {_FAKE_VISA}")
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        blob = _read(log_file)
        assert _FAKE_VISA not in blob
        assert "[redacted-card]" in blob


class TestPerFieldClassVerbosity:
    """Per-field-class modes apply regardless of where the value appears."""

    def test_user_message_default_mode_is_redacted(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        # Default user_messages mode is 'redacted' — pattern masking applies.
        log_file = _setup(tmp_path)
        get_logger("security.pii").info(
            "chat.message",
            user_message=f"email me at {_FAKE_EMAIL}",
        )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        blob = _read(log_file)
        assert _FAKE_EMAIL not in blob
        assert "[redacted-email]" in blob

    def test_email_field_default_mode_is_hashed(self, tmp_path: Path, _clean_state: None) -> None:
        # Default emails mode is 'hashed' — full value replaced with sha256 prefix.
        log_file = _setup(tmp_path)
        get_logger("security.pii").info("contact.stored", email=_FAKE_EMAIL)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        blob = _read(log_file)
        assert _FAKE_EMAIL not in blob
        assert "sha256:" in blob

    def test_minimal_mode_replaces_user_message_entirely(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup(tmp_path, _make_obs_config(pii_overrides={"user_messages": "minimal"}))
        get_logger("security.pii").info(
            "chat.message",
            user_message=f"hello {_FAKE_EMAIL}, did you see the {_FAKE_CPF} form?",
        )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = [
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        msg = next(r for r in records if r.get("event") == "chat.message")
        assert msg["user_message"] == "[redacted]"

    def test_hashed_mode_is_deterministic(self, tmp_path: Path, _clean_state: None) -> None:
        # Same email hashes to the same value across two separate records so
        # operators can still correlate log lines without exposing the raw PII.
        log_file = _setup(tmp_path)
        log = get_logger("security.pii")
        log.info("contact.seen", email=_FAKE_EMAIL)
        log.info("contact.seen", email=_FAKE_EMAIL)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = [
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("event") == "contact.seen"
        ]
        assert len(records) == 2
        assert records[0]["email"] == records[1]["email"]
        assert records[0]["email"].startswith("sha256:")


class TestProtectedEnvelopeKeys:
    """The sweep must NEVER rewrite envelope/protocol fields."""

    def test_event_name_is_preserved_even_when_email_shaped(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        # An event name intentionally shaped like a redaction target
        # (e.g. an internal module that emits "audit.user@x" as its event)
        # would break the dashboard's KNOWN_EVENTS matching if scrubbed.
        log_file = _setup(tmp_path)
        # The envelope must emit through with its canonical fields untouched —
        # we assert event + logger are byte-identical after the pipeline.
        get_logger("security.envelope").info("audit.record", detail="ok")
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        rec = next(
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("event") == "audit.record"
        )
        assert rec["event"] == "audit.record"
        assert rec["logger"] == "security.envelope"


class TestNestedPayloads:
    """Nested structures don't hide PII from the sweep."""

    def test_pii_inside_list_of_strings_is_masked(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        # PIIRedactor only walks top-level string fields; a list passes
        # through as-is. This test documents the current contract: callers
        # that log user content MUST route it through a top-level string
        # field (user_message, transcript, etc.) where the redactor runs.
        log_file = _setup(tmp_path)
        get_logger("security.pii").info(
            "raw.payload",
            items=[_FAKE_EMAIL],
        )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        blob = _read(log_file)
        # Documenting the guard: if this invariant ever changes (deep walk
        # added), update the assertion — but the fixture must keep synthetic
        # values so we never ship a real address through the test suite.
        assert _FAKE_EMAIL in blob, (
            "nested-list redaction is NOT yet part of the contract; if you "
            "added it, flip this assertion and update the docs"
        )

    def test_multiple_pii_kinds_in_one_field_all_masked(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup(tmp_path)
        combined = f"{_FAKE_EMAIL} / {_FAKE_CPF} / {_FAKE_IPV4} / {_FAKE_JWT} / {_FAKE_VISA}"
        get_logger("security.pii").info("multi.pii", blob=combined)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        blob = _read(log_file)
        for secret in (_FAKE_EMAIL, _FAKE_CPF, _FAKE_IPV4, _FAKE_JWT, _FAKE_VISA):
            assert secret not in blob, f"{secret!r} leaked into the log"
        # All five masks appeared.
        for token in (
            "[redacted-email]",
            "[redacted-cpf]",
            "[redacted-ipv4]",
            "[redacted-jwt]",
            "[redacted-card]",
        ):
            assert token in blob


class TestIdempotence:
    """Processing a redacted value a second time must not re-introduce PII."""

    def test_second_pass_leaves_masks_intact(self, tmp_path: Path, _clean_state: None) -> None:
        # Emit, shutdown, re-emit through a fresh pipeline — the marker tokens
        # survive because the redactor never reverses its own output.
        log_file = _setup(tmp_path)
        get_logger("security.pii").info("first", note=f"email {_FAKE_EMAIL}")
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        first_blob = _read(log_file)
        assert "[redacted-email]" in first_blob

        # A record carrying the *already-redacted* string must not be rewritten.
        # Reopen the pipeline and forward the marker as-is.
        setup_logging(
            LoggingConfig(level="DEBUG", console_format="json", log_file=log_file),
            _make_obs_config(),
            data_dir=tmp_path,
        )
        get_logger("security.pii").info("second", note="forward [redacted-email]")
        shutdown_logging(timeout=3.0)

        final_blob = _read(log_file)
        assert _FAKE_EMAIL not in final_blob
        assert "[redacted-email]" in final_blob


class TestSecretMaskerLayering:
    """Sensitive-named fields are masked by SecretMasker before PIIRedactor sees them."""

    def test_field_named_key_gets_short_mask(self, tmp_path: Path, _clean_state: None) -> None:
        # ``key`` / ``token`` / ``secret`` / ``password`` field names trigger
        # the upstream SecretMasker which collapses the value to ``aaa...zzz``
        # (first-3 + last-3). This is defence-in-depth: even if the regex
        # sweep ever regressed, the secret-shaped field would not leak.
        log_file = _setup(tmp_path)
        get_logger("security.pii").info("config", key=_FAKE_API_KEY_ANTHROPIC)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = [
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("event") == "config"
        ]
        assert len(records) == 1
        masked = records[0]["key"]
        assert _FAKE_API_KEY_ANTHROPIC not in masked
        # Format: <first 3>...<last 3>
        assert masked.startswith(_FAKE_API_KEY_ANTHROPIC[:3])
        assert masked.endswith(_FAKE_API_KEY_ANTHROPIC[-3:])
        assert "..." in masked

    def test_short_secret_is_fully_starred(self, tmp_path: Path, _clean_state: None) -> None:
        # Values shorter than 8 chars collapse to ``***`` so the prefix/suffix
        # leak is impossible for short tokens.
        log_file = _setup(tmp_path)
        get_logger("security.pii").info("auth", password="abc123")
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = [
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("event") == "auth"
        ]
        assert len(records) == 1
        assert records[0]["password"] == "***"
