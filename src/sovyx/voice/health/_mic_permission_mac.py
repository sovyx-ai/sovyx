"""macOS microphone-permission probe via TCC.db (MA2).

Replaces the UNKNOWN stub in :mod:`sovyx.voice.health._mic_permission`
for the ``darwin`` branch. Reads the user's TCC database directly via
:mod:`sqlite3` (Python stdlib — no third-party dependency required).

TCC = Apple's Transparency, Consent, and Control framework. Per-user
permission state lives in
``~/Library/Application Support/com.apple.TCC/TCC.db``; the system-
wide MDM-policy state lives in
``/Library/Application Support/com.apple.TCC/TCC.db``.

Schema (Big Sur 11+ canonical):

* Table ``access`` with columns:
  - ``service`` — TEXT (e.g. ``"kTCCServiceMicrophone"``)
  - ``client`` — TEXT (bundle ID or executable path)
  - ``client_type`` — INTEGER (0 = bundle ID, 1 = path)
  - ``auth_value`` — INTEGER (0 = denied, 1 = unknown, 2 = allowed,
    3 = limited)
  - ``auth_reason`` — INTEGER (audit reason code)

Authorisation values (Apple's TCCAccessAuthValue):

* ``0`` — Denied
* ``1`` — Unknown (never asked)
* ``2`` — Allowed
* ``3`` — Limited (partial — used for things like Full Disk Access
  scope; not applicable to microphone)

Full Disk Access caveat: reading TCC.db directly requires the
calling process to have FDA granted (or to be running as the user
who owns the file). Without FDA, ``sqlite3.connect`` raises
``OperationalError: unable to open database file``. We return
UNKNOWN with a structured note in that case so the dashboard can
recommend granting FDA without misclassifying the actual mic state.

Reference: F1 inventory mission task MA2; CLAUDE.md anti-pattern #9
(StrEnum for value-stable comparisons); Apple TCC documentation.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from pathlib import Path

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── TCC constants ─────────────────────────────────────────────────


_TCC_SERVICE_MICROPHONE = "kTCCServiceMicrophone"
"""Apple's stable service identifier for microphone access. Used as
the lookup key in the ``access`` table."""

_TCC_AUTH_DENIED = 0
_TCC_AUTH_UNKNOWN = 1
_TCC_AUTH_ALLOWED = 2
_TCC_AUTH_LIMITED = 3
"""TCCAccessAuthValue constants from Apple's
``CoreServices/TCC.h``. Values stable across macOS releases since
Big Sur 11. Bigger Sur (10.15) used a different schema (``allowed``
boolean) which is no longer present on supported macOS versions."""


# ── Probe ─────────────────────────────────────────────────────────


def query_macos_microphone_permission() -> tuple[int | None, list[str]]:
    """Read the TCC.db microphone authorisation state.

    Returns a tuple ``(auth_value, notes)`` where:

    * ``auth_value`` is the canonical TCCAccessAuthValue integer
      (0=denied, 1=unknown, 2=allowed, 3=limited) when the lookup
      succeeded; ``None`` when:
        - The platform isn't darwin.
        - TCC.db can't be opened (no FDA / file missing).
        - No microphone row exists for any client (Sovyx never asked).
    * ``notes`` is a list of structured diagnostic strings explaining
      why ``auth_value`` is None (or empty when the lookup succeeded).

    Never raises — sqlite3 / FS / permission failures collapse into
    ``(None, notes)`` so the higher-level
    :func:`~sovyx.voice.health._mic_permission.check_microphone_permission`
    can map them to MicPermissionStatus.UNKNOWN cleanly.
    """
    notes: list[str] = []
    if sys.platform != "darwin":
        notes.append(f"non-darwin platform: {sys.platform}")
        return None, notes

    user_tcc = _user_tcc_path()
    if not user_tcc.exists():
        notes.append(f"user TCC.db not found at {user_tcc}")
        return None, notes

    try:
        # ``mode=ro`` opens read-only — never accidentally mutate the
        # user's permission database.
        uri = f"file:{user_tcc}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        notes.append(
            f"sqlite3 open failed (likely needs Full Disk Access): {exc!r}",
        )
        return None, notes
    except Exception as exc:  # noqa: BLE001 — sqlite open boundary
        notes.append(f"unexpected sqlite open error: {exc!r}")
        return None, notes

    try:
        # Look up the highest-privilege auth_value across ALL clients
        # for the microphone service. Rationale: if ANY client (e.g.
        # Terminal, Python, Sovyx itself) has been granted, the
        # process running THIS check (which inherits from one of those
        # parents) is highly likely to also be allowed. False-DENIED
        # is the worse failure mode (blocks valid setups); MAX favours
        # ALLOWED when the truth is ambiguous.
        cursor = conn.execute(
            "SELECT MAX(auth_value), COUNT(*) FROM access WHERE service = ?",
            (_TCC_SERVICE_MICROPHONE,),
        )
        row = cursor.fetchone()
    except sqlite3.OperationalError as exc:
        notes.append(f"sqlite3 query failed: {exc!r}")
        return None, notes
    except Exception as exc:  # noqa: BLE001 — sqlite query boundary
        notes.append(f"unexpected sqlite query error: {exc!r}")
        return None, notes
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    if row is None:
        notes.append("query returned no rows (table empty?)")
        return None, notes
    max_auth, count = row
    if count == 0 or max_auth is None:
        notes.append(
            f"no rows for service={_TCC_SERVICE_MICROPHONE} (Sovyx never asked)",
        )
        return None, notes
    return int(max_auth), notes


def _user_tcc_path() -> Path:
    """``$HOME/Library/Application Support/com.apple.TCC/TCC.db``.

    Path-only — no I/O. Caller checks .exists() before opening."""
    return Path.home() / "Library" / "Application Support" / "com.apple.TCC" / "TCC.db"


# ── High-level translation ────────────────────────────────────────


def auth_value_to_status_token(
    auth_value: int | None,
) -> str:
    """Map a TCCAccessAuthValue to the
    :class:`~sovyx.voice.health._mic_permission.MicPermissionStatus`
    enum's value string.

    Returns the string literal (not the enum member) so this module
    has zero import dependency on the higher-level
    ``_mic_permission`` module — keeps the dependency one-way."""
    if auth_value is None:
        return "unknown"
    if auth_value in (_TCC_AUTH_ALLOWED, _TCC_AUTH_LIMITED):
        # LIMITED isn't applicable to mic per Apple docs but if it
        # ever appeared we'd treat as allowed (better than false DENY).
        return "granted"
    if auth_value == _TCC_AUTH_DENIED:
        return "denied"
    # AUTH_UNKNOWN (1) — Sovyx hasn't been asked yet. The system will
    # prompt on first capture attempt; treat as UNKNOWN here.
    return "unknown"


__all__ = [
    "auth_value_to_status_token",
    "query_macos_microphone_permission",
]
