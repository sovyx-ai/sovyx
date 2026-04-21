"""CI gate — assert no PII pattern leaks into the rendered log file.

Stand-alone enforcement of §22.3 ("no real PII in fixtures, no raw PII
in test logs"). The gate is self-sufficient: it does NOT depend on the
Sovyx pytest suite running — instead it spins up the production
``setup_logging`` pipeline against a temporary log file with PII
redaction enabled, emits a battery of synthetic records carrying every
PII class the redactor is supposed to mask, drains the async writer,
and then greps the resulting JSON file for the raw patterns. If any
unredacted PII survived the processor chain, the gate fails the CI
job and lists every offending line.

Why a real-pipeline gate (not a unit test):

  * Unit tests on :class:`sovyx.observability.pii.PIIRedactor` exercise
    the processor in isolation. They prove the redactor *can* mask, not
    that the production wiring (``setup_logging`` → processor chain →
    JSON renderer → ``RotatingFileHandler``) actually routes records
    through it. A misconfiguration that drops PIIRedactor out of the
    chain (e.g., reordering processors, gating it behind a feature
    flag) silently passes unit tests but leaks PII in the daemon.
  * The grep step uses the same regexes operators would use to triage
    a leak post-incident, so a green gate is the same evidence the
    SecOps team would accept.

Patterns checked (all are §22.3 ban list):

  * Email — ``\\S+@\\S+\\.\\S+``
  * Brazilian CPF — ``\\d{3}\\.\\d{3}\\.\\d{3}-\\d{2}``
  * JWT (RFC 7519) — ``eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+``
  * Provider API key — ``sk[-_][A-Za-z0-9_-]{24,}`` (Anthropic, OpenAI, Stripe)
  * Brazilian phone — ``\\(\\d{2}\\) \\d{4,5}-\\d{4}``
  * IPv4 — ``(?:\\d{1,3}\\.){3}\\d{1,3}``

Wired into ``.github/workflows/ci.yml`` as the ``pii-leak-gate`` job
after ``exception-chain-gate``.
"""

from __future__ import annotations

import contextlib
import json
import logging as _stdlib_logging
import logging.handlers
import re
import sys
import tempfile
import time
from pathlib import Path

from sovyx.engine.config import (
    LoggingConfig,
    ObservabilityConfig,
    ObservabilityFeaturesConfig,
    ObservabilityPIIConfig,
)
from sovyx.observability import logging as _sovyx_logging

# ── Synthetic PII fixtures ─────────────────────────────────────────────
# Every value below is fake — generated for this gate. The CPF passes
# the official check-digit algorithm (so the gate exercises the same
# code path a real CPF would), but the entity it identifies does not
# exist. Do NOT replace any of these with a real value.
_FAKE_EMAIL = "synthetic.user@example-fake.test"
_FAKE_CPF = "529.982.247-25"  # well-formed test CPF — passes mod-11 check
_FAKE_PHONE_BR = "(11) 98765-4321"
_FAKE_IPV4 = "203.0.113.45"  # RFC 5737 documentation block
# JWT signed with a throwaway key over ``{"sub":"test"}``. Length is
# realistic (~140 chars) so the regex anchor ``eyJ`` + 3-segment shape
# matches a production-shaped token.
_FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiJ0ZXN0LXN1YmplY3QiLCJpYXQiOjE3MDAwMDAwMDB9"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
_FAKE_API_KEYS = (
    "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "sk-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "sk_live_-PLACEHOLDER-FIXTURE-FOR-TESTS-0",
)


# ── Detection patterns (post-redaction sweep) ──────────────────────────
# These mirror §22.3 but are intentionally a touch tighter than the
# production redactor regexes — the gate is the *belt* on top of the
# redactor's *braces*: it should fire if any new PII class enters the
# code without a matching redactor pattern.
_LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("cpf", re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    ("api_key", re.compile(r"\bsk[-_][A-Za-z0-9_-]{24,}\b")),
    (
        "phone_br",
        re.compile(r"\(\d{2}\)\s?9?\d{4}[\s-]?\d{4}\b"),
    ),
    (
        "ipv4",
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b"),
    ),
)


def _emit_synthetic_records() -> None:
    """Emit one log line per PII class across every redacted field-class.

    Mixing the same PII into every field name exercises both legs of
    :class:`sovyx.observability.pii.PIIRedactor` — the per-field-class
    verbosity table (``user_message``, ``transcript``, …) and the
    global regex sweep (``free_text``, ``custom_*``).
    """
    logger = _sovyx_logging.get_logger("sovyx.gate.pii_check")

    # Per-field-class verbosity table coverage.
    logger.info(
        "synthetic.user_message",
        user_message=f"hello, my email is {_FAKE_EMAIL} and CPF {_FAKE_CPF}",
    )
    logger.info(
        "synthetic.transcript",
        transcript=f"call from {_FAKE_PHONE_BR} about token {_FAKE_JWT}",
    )
    logger.info(
        "synthetic.prompt",
        prompt=f"please charge {_FAKE_API_KEYS[0]} from address {_FAKE_IPV4}",
    )
    logger.info(
        "synthetic.response",
        response=f"sent to {_FAKE_EMAIL}, key={_FAKE_API_KEYS[1]}",
    )
    logger.info(
        "synthetic.email_field",
        email=_FAKE_EMAIL,
    )
    logger.info(
        "synthetic.phone_field",
        phone=_FAKE_PHONE_BR,
    )

    # Free-form / custom fields — exercises the global sweep on keys
    # not listed in ``_PII_FIELD_CLASSES``.
    logger.info(
        "synthetic.free_text",
        narrative=(
            f"customer {_FAKE_EMAIL} called from {_FAKE_PHONE_BR} "
            f"with CPF {_FAKE_CPF} from IP {_FAKE_IPV4} carrying "
            f"JWT {_FAKE_JWT} and API key {_FAKE_API_KEYS[2]}"
        ),
    )
    logger.info(
        "synthetic.exception_field",
        custom_payload=f"upstream rejected token={_FAKE_JWT}",
    )


def _scan_log_file(path: Path) -> list[tuple[int, str, str]]:
    """Return every (line_no, pattern_name, raw_line) leak in *path*."""
    if not path.exists():
        return [(0, "missing-log-file", str(path))]
    leaks: list[tuple[int, str, str]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line_no, raw in enumerate(fh, start=1):
            stripped = raw.rstrip("\n")
            for name, pattern in _LEAK_PATTERNS:
                if pattern.search(stripped):
                    leaks.append((line_no, name, stripped))
                    break  # one violation per line is enough to fail
    return leaks


def _drain_logging() -> None:
    """Flush the async writer + close every file handler.

    The production pipeline uses a background queue, so log records
    written immediately before exit can still be in flight when the
    process tears down. We drain explicitly so the gate's grep runs
    against the final on-disk content, not a partial flush.
    """
    # 1) Stop the BackgroundLogWriter so its QueueListener thread
    #    drains the queue and stops touching the downstream file
    #    handler. ``drain_and_stop`` flushes the handlers but does
    #    NOT close them, which is why we still need step 3 below.
    writer = getattr(_sovyx_logging, "_async_writer", None)
    if writer is not None:
        with contextlib.suppress(Exception):
            writer.drain_and_stop(timeout=2.0)

    # 2) Run the production-shutdown path so the canonical close
    #    ordering executes (ring buffer flush, fast-path drain, ...).
    with contextlib.suppress(Exception):
        _sovyx_logging.shutdown_logging(timeout=2.0)

    # 3) Close every file-backed handler the writer (or shutdown_logging)
    #    may have left open. On Windows, an open RotatingFileHandler
    #    keeps an exclusive lock on the file and TemporaryDirectory
    #    cleanup raises PermissionError. We walk three sources:
    #      - root logger handlers (console + ring buffer, normally)
    #      - ``sovyx.audit`` (propagate=False child — root walk skips it)
    #      - the BackgroundLogWriter's downstream handlers (the actual
    #        RotatingFileHandler holding ``sovyx.log``; it is owned by
    #        the writer, not the root logger)
    handlers_to_close: list[_stdlib_logging.Handler] = []
    for parent in (
        _stdlib_logging.getLogger(),
        _stdlib_logging.getLogger("sovyx.audit"),
    ):
        for handler in list(parent.handlers):
            handlers_to_close.append(handler)
            with contextlib.suppress(Exception):
                parent.removeHandler(handler)
    if writer is not None:
        # ``_handlers`` is in ``__slots__`` — accessed directly.
        with contextlib.suppress(Exception):
            handlers_to_close.extend(writer._handlers)  # noqa: SLF001

    for handler in handlers_to_close:
        with contextlib.suppress(Exception):
            handler.flush()
        with contextlib.suppress(Exception):
            handler.close()


def _build_pipeline(log_file: Path) -> None:
    """Configure ``setup_logging`` with PII redaction targeted at *log_file*."""
    logging_cfg = LoggingConfig(
        level="INFO",
        console_format="json",
        log_file=log_file,
    )
    obs_cfg = ObservabilityConfig(
        features=ObservabilityFeaturesConfig(
            async_queue=True,
            pii_redaction=True,
            schema_validation=False,  # off so synthetic events bypass schema gate
        ),
        pii=ObservabilityPIIConfig(),  # default redaction modes
    )
    _sovyx_logging.setup_logging(logging_cfg, obs_cfg, data_dir=log_file.parent)


def main() -> int:
    """CLI entry point — returns 0 on clean run, 1 on any leak."""
    with tempfile.TemporaryDirectory(prefix="sovyx-pii-gate-") as tmp:
        tmp_path = Path(tmp)
        log_file = tmp_path / "logs" / "sovyx.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            _build_pipeline(log_file)
        except Exception as exc:  # noqa: BLE001
            print(
                f"FAIL: could not initialize logging pipeline: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1

        try:
            _emit_synthetic_records()
        except Exception as exc:  # noqa: BLE001
            print(
                f"FAIL: could not emit synthetic PII records: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        finally:
            # The async queue is drained on rotation/shutdown; force a
            # short settle window before the explicit drain so records
            # in flight at the moment of `_emit_synthetic_records()`
            # exit have a chance to land in the queue.
            time.sleep(0.2)
            _drain_logging()

        leaks = _scan_log_file(log_file)
        if not leaks:
            line_count = sum(1 for _ in log_file.open(encoding="utf-8", errors="replace"))
            print(
                f"OK: PII redactor pipeline emitted {line_count} records, zero raw PII leaked.",
            )
            return 0

        print(
            f"\nFAIL: {len(leaks)} PII leak(s) detected in rendered log file:",
            file=sys.stderr,
        )
        # Group leaks by pattern for a compact report.
        for line_no, name, raw in leaks[:50]:
            # JSON-decode if possible so the grep'ed value is shown
            # alongside its event name; fall back to the raw line.
            try:
                rec = json.loads(raw)
                event = rec.get("event", "?")
                print(
                    f"  line {line_no} [{name}] event={event!r} :: {raw[:200]}",
                    file=sys.stderr,
                )
            except json.JSONDecodeError:
                print(f"  line {line_no} [{name}] :: {raw[:200]}", file=sys.stderr)
        if len(leaks) > 50:
            print(f"  ... and {len(leaks) - 50} more", file=sys.stderr)
        print(
            "\n  Fix: ensure PIIRedactor handles the leaked pattern class "
            "and that the field is not in `_PROTECTED_KEYS` "
            "(see src/sovyx/observability/pii.py).",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
