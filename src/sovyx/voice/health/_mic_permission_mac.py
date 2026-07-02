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

Client matching (MACOS-2 remediation): TCC grants are PER-CLIENT and
do NOT inherit — a grant for Zoom or Chrome says nothing about the
Terminal / iTerm / IDE process tree that actually hosts Sovyx. The
probe therefore only trusts rows whose ``client`` matches a candidate
for THIS process:

* the responsible app's bundle identifier from the
  ``__CFBundleIdentifier`` environment variable (macOS app bundles —
  Terminal.app, iTerm2.app, a packaged Sovyx.app — export it to child
  processes; when present it names exactly the client TCC attributes
  this process to);
* known terminal / IDE bundle IDs derived from the process ancestry
  (:mod:`psutil` parent names mapped via
  :data:`_KNOWN_TERMINAL_BUNDLE_IDS` — a HEURISTIC, since NSWorkspace
  isn't available without pyobjc);
* path-form clients matching ``sys.executable`` (exact or
  basename-suffix — also heuristic).

Verdict semantics over the MATCHED rows: an explicit deny (0) wins
over every other matched row; otherwise allowed (2), then limited (3),
then not-yet-asked (1). When microphone rows exist but NONE match a
candidate, the probe returns UNKNOWN (never GRANTED) with a note
explaining the grants likely belong to other apps — the pre-MACOS-2
``MAX(auth_value) across ALL clients`` behaviour falsely reported
GRANTED off any unrelated app's grant and made DENIED practically
unreachable.

Full Disk Access caveat: reading TCC.db directly requires the
calling process to have FDA granted (or to be running as the user
who owns the file). Without FDA, ``sqlite3.connect`` raises
``OperationalError: unable to open database file``. We return
UNKNOWN with a structured note in that case so the dashboard can
recommend granting FDA without misclassifying the actual mic state.

Reference: F1 inventory mission task MA2; CLAUDE.md anti-pattern #9
(StrEnum for value-stable comparisons); anti-pattern #52 (the
pre-MACOS-2 comment claimed a TCC "inheritance" that does not exist);
Apple TCC documentation.
"""

from __future__ import annotations

import contextlib
import os
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
Big Sur (11), where the ``auth_value`` column was introduced.
Catalina (10.15) and earlier used the legacy ``allowed`` boolean
schema, which is no longer present on supported macOS versions."""

_TCC_CLIENT_TYPE_BUNDLE_ID = 0
_TCC_CLIENT_TYPE_PATH = 1
"""``access.client_type`` discriminator: 0 = the ``client`` column is
a bundle identifier, 1 = it is an absolute executable path."""


# ── Host-client candidates ────────────────────────────────────────


_RESPONSIBLE_BUNDLE_ID_ENV = "__CFBundleIdentifier"
"""Environment variable macOS app bundles export to their child
processes. For a shell inside Terminal.app it is
``com.apple.Terminal``; for a packaged Sovyx.app it would be Sovyx's
own bundle ID. When present it is the most reliable name for the TCC
"responsible application" of this process."""

_KNOWN_TERMINAL_BUNDLE_IDS: dict[str, str] = {
    "terminal": "com.apple.Terminal",
    "iterm2": "com.googlecode.iterm2",
    "iterm": "com.googlecode.iterm2",
    "warp": "dev.warp.Warp",
    "code": "com.microsoft.VSCode",
    "code helper": "com.microsoft.VSCode",
    "alacritty": "org.alacritty",
    "kitty": "net.kovidgoyal.kitty",
    "wezterm": "com.github.wez.wezterm",
    "wezterm-gui": "com.github.wez.wezterm",
    "ghostty": "com.mitchellh.ghostty",
    "hyper": "co.zeit.hyper",
}
"""HEURISTIC map from a lowercased ancestor-process name to the
terminal / IDE app's bundle identifier. Used as a fallback when
``__CFBundleIdentifier`` isn't in the environment (NSWorkspace's
authoritative responsible-app lookup would need pyobjc, which Sovyx
doesn't depend on). Unmapped ancestors simply contribute no
candidate — the probe then degrades to UNKNOWN, never to a false
GRANTED/DENIED."""


def _candidate_clients() -> tuple[set[str], set[str], list[str]]:
    """Collect TCC ``client`` candidates for the app hosting THIS process.

    Returns ``(bundle_id_candidates, path_candidates, notes)`` where
    bundle IDs are lowercased for case-insensitive comparison and
    paths are absolute executable paths (``sys.executable`` +
    realpath). Never raises — a failed ancestry walk collapses into a
    note."""
    bundle_ids: set[str] = set()
    paths: set[str] = set()
    notes: list[str] = []

    env_bundle = os.environ.get(_RESPONSIBLE_BUNDLE_ID_ENV, "").strip()
    if env_bundle:
        bundle_ids.add(env_bundle.lower())

    try:
        import psutil

        for parent in psutil.Process().parents():
            name = parent.name().lower().removesuffix(".app")
            mapped = _KNOWN_TERMINAL_BUNDLE_IDS.get(name)
            if mapped is not None:
                bundle_ids.add(mapped.lower())
    except Exception as exc:  # noqa: BLE001 — ancestry walk is best-effort
        notes.append(f"process-ancestry walk unavailable: {exc!r}")

    if sys.executable:
        paths.add(sys.executable)
        with contextlib.suppress(OSError):
            paths.add(os.path.realpath(sys.executable))

    return bundle_ids, paths, notes


def _row_matches_candidates(
    client: str,
    client_type: int,
    bundle_ids: set[str],
    paths: set[str],
) -> bool:
    """``True`` iff a TCC ``access`` row's client plausibly names the
    app hosting this process.

    Bundle-ID rows compare case-insensitively against the candidate
    set. Path-form rows (``client_type == 1`` or a ``/`` in the
    value) match exactly, or by basename suffix in either direction
    (heuristic — a TCC row for ``/usr/local/bin/python3.12`` should
    match a venv symlink resolving to the same interpreter name)."""
    client_norm = client.strip()
    if not client_norm:
        return False
    looks_like_path = client_type == _TCC_CLIENT_TYPE_PATH or "/" in client_norm
    if not looks_like_path:
        return client_norm.lower() in bundle_ids
    for candidate in paths:
        if client_norm == candidate:
            return True
        client_base = Path(client_norm).name
        candidate_base = Path(candidate).name
        if client_base and candidate.endswith(f"/{client_base}"):
            return True
        if candidate_base and client_norm.endswith(f"/{candidate_base}"):
            return True
    return False


# ── Probe ─────────────────────────────────────────────────────────


def query_macos_microphone_permission() -> tuple[int | None, list[str]]:
    """Read the TCC.db microphone authorisation state for the client
    hosting this process.

    Returns a tuple ``(auth_value, notes)`` where:

    * ``auth_value`` is the canonical TCCAccessAuthValue integer
      (0=denied, 1=unknown, 2=allowed, 3=limited) resolved over the
      rows whose ``client`` matches this process's hosting app
      (deny > allowed > limited > not-asked); ``None`` when:
        - The platform isn't darwin.
        - TCC.db can't be opened (no FDA / file missing).
        - No microphone row exists for any client (never asked).
        - Microphone rows exist but NONE match a candidate client —
          the grants likely belong to other apps (Zoom, Chrome, …)
          and say nothing about this process (MACOS-2).
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
        # user's permission database. ``as_posix()`` keeps the URI
        # form portable (backslash-free) for the test fixtures too.
        uri = f"file:{user_tcc.as_posix()}?mode=ro"
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
        cursor = conn.execute(
            "SELECT client, client_type, auth_value FROM access WHERE service = ?",
            (_TCC_SERVICE_MICROPHONE,),
        )
        rows = cursor.fetchall()
    except sqlite3.OperationalError as exc:
        notes.append(f"sqlite3 query failed: {exc!r}")
        return None, notes
    except Exception as exc:  # noqa: BLE001 — sqlite query boundary
        notes.append(f"unexpected sqlite query error: {exc!r}")
        return None, notes
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    if not rows:
        notes.append(
            f"no rows for service={_TCC_SERVICE_MICROPHONE} (Sovyx never asked)",
        )
        return None, notes

    bundle_ids, paths, candidate_notes = _candidate_clients()
    notes.extend(candidate_notes)

    matched_values: list[int] = []
    unmatched_clients: list[str] = []
    for raw_client, raw_client_type, raw_auth in rows:
        if raw_auth is None:
            continue
        client = str(raw_client or "")
        client_type = int(raw_client_type or _TCC_CLIENT_TYPE_BUNDLE_ID)
        if _row_matches_candidates(client, client_type, bundle_ids, paths):
            matched_values.append(int(raw_auth))
        else:
            unmatched_clients.append(client)

    if not matched_values:
        preview = ", ".join(unmatched_clients[:3]) or "<none>"
        notes.append(
            f"{len(rows)} microphone row(s) exist in TCC.db but none match this "
            f"process's hosting app (candidates: "
            f"{sorted(bundle_ids) + sorted(paths)}); the grant(s) may belong to "
            f"other apps ({preview}) and say nothing about Sovyx. If capture "
            "is silent, check System Settings → Privacy & Security → "
            "Microphone for the app hosting Sovyx (Terminal / iTerm2 / IDE).",
        )
        return None, notes

    # Explicit deny for a matched client wins over every other matched
    # row (a LIMITED or ALLOWED row for a second matched client must
    # never outvote the deny — that inversion was part of MACOS-2).
    if _TCC_AUTH_DENIED in matched_values:
        return _TCC_AUTH_DENIED, notes
    if _TCC_AUTH_ALLOWED in matched_values:
        return _TCC_AUTH_ALLOWED, notes
    if _TCC_AUTH_LIMITED in matched_values:
        return _TCC_AUTH_LIMITED, notes
    return _TCC_AUTH_UNKNOWN, notes


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
    ``_mic_permission`` module — keeps the dependency one-way.

    LIMITED (3) maps to ``"granted"``: ``MicPermissionStatus`` has no
    LIMITED member, LIMITED isn't applicable to microphone per Apple
    docs, and since MACOS-2 the row-level verdict already lets a
    matched deny win before LIMITED is ever considered — so this
    mapping can no longer mask a deny."""
    if auth_value is None:
        return "unknown"
    if auth_value in (_TCC_AUTH_ALLOWED, _TCC_AUTH_LIMITED):
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
