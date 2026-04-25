#!/usr/bin/env python3
"""tcc-mic-reader — lê TCC.db (system + user) procurando microphone consents.

Saída JSON em stdout:
{
  "ok": true,
  "fda_status": "granted" | "denied" | "unknown",
  "user_db_path": "...",
  "system_db_path": "...",
  "user_consents": [ { client, auth_value_name, auth_reason_name, last_modified_iso, ... } ],
  "system_consents": [ ... ],
  "errors": []
}

TCC.db schema (macOS 10.15+):
  Table: access
  Columns relevant: service (TEXT), client (TEXT), client_type (INTEGER),
                    auth_value (INTEGER), auth_reason (INTEGER),
                    last_modified (INTEGER, unix epoch)

  service = 'kTCCServiceMicrophone'
  auth_value: 0=denied, 1=unknown, 2=allowed (Catalina+)
              older: 0=denied, 1=allowed (pre-Catalina)
  auth_reason: complex enum, see Apple docs

Requires Full Disk Access for the running terminal/script — without it,
SQLite open() returns "permission denied". We detect FDA via probe and
report graceful failure with instructions.

Reference:
  https://www.rainforestqa.com/blog/macos-tcc-db-deep-dive
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

TOOL_VERSION = "1.0"

AUTH_VALUE_NAMES = {
    0: "denied",
    1: "unknown",
    2: "allowed",
    3: "limited",
    4: "add_only",
}

AUTH_REASON_NAMES = {
    0: "none",
    1: "error",
    2: "user_consent",
    3: "user_set",
    4: "system_set",
    5: "service_policy",
    6: "mdm_policy",
    7: "override_policy",
    8: "missing_usage_string",
    9: "prompt_timeout",
    10: "preflight_unknown",
    11: "entitled",
    12: "app_type_policy",
}


def _probe_fda(test_path: Path) -> str:
    """Returns 'granted', 'denied' (FDA needed), or 'unknown' (DB missing)."""
    if not test_path.exists():
        return "unknown"
    try:
        conn = sqlite3.connect(f"file:{test_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM access LIMIT 1")
        cur.fetchone()
        conn.close()
        return "granted"
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "permission" in msg or "unable to open" in msg or "authorization" in msg:
            return "denied"
        return "unknown"
    except Exception:
        return "unknown"


def _read_consents(db_path: Path) -> tuple[list[dict], str | None]:
    """Returns (consents_list, error_message_or_None)."""
    if not db_path.exists():
        return [], f"db_not_found: {db_path}"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        # Try newer schema first (Catalina+).
        try:
            cur.execute("""
                SELECT service, client, client_type, auth_value, auth_reason,
                       last_modified
                FROM access
                WHERE service = 'kTCCServiceMicrophone'
                ORDER BY last_modified DESC
            """)
        except sqlite3.OperationalError:
            # Older schema: 'allowed' instead of 'auth_value'.
            cur.execute("""
                SELECT service, client, client_type, allowed, NULL, last_modified
                FROM access
                WHERE service = 'kTCCServiceMicrophone'
                ORDER BY last_modified DESC
            """)
        rows = cur.fetchall()
        conn.close()
        consents = []
        for row in rows:
            service, client, client_type, auth_value, auth_reason, last_modified = row
            consents.append({
                "service": service,
                "client": client,
                "client_type": client_type,
                "auth_value": auth_value,
                "auth_value_name": AUTH_VALUE_NAMES.get(auth_value, f"unknown({auth_value})"),
                "auth_reason": auth_reason,
                "auth_reason_name": (AUTH_REASON_NAMES.get(auth_reason, f"unknown({auth_reason})")
                                     if auth_reason is not None else None),
                "last_modified_unix": last_modified,
                "last_modified_iso": (
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_modified))
                    if last_modified else None
                ),
            })
        return consents, None
    except Exception as e:
        return [], f"read_failed: {type(e).__name__}: {e}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="-",
                   help="output path or '-' for stdout (default)")
    args = p.parse_args()

    user_db = Path.home() / "Library" / "Application Support" / "com.apple.TCC" / "TCC.db"
    system_db = Path("/Library/Application Support/com.apple.TCC/TCC.db")

    fda_status = _probe_fda(user_db)
    if fda_status == "unknown":
        # Try system db as fallback for FDA probe.
        fda_status = _probe_fda(system_db)

    output = {
        "ok": True,
        "tool_version": TOOL_VERSION,
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fda_status": fda_status,
        "user_db_path": str(user_db),
        "system_db_path": str(system_db),
        "user_consents": [],
        "system_consents": [],
        "errors": [],
    }

    if fda_status != "granted":
        output["errors"].append(
            "FDA (Full Disk Access) NOT granted to this terminal. "
            "Grant it in System Settings > Privacy & Security > Full Disk Access "
            "(add Terminal.app or iTerm2.app), then re-run."
        )
        # Still emit empty result — analyst sees the gap explicitly.
        print(json.dumps(output, indent=2))
        return 0

    user_consents, user_err = _read_consents(user_db)
    output["user_consents"] = user_consents
    if user_err:
        output["errors"].append(f"user_db: {user_err}")

    system_consents, system_err = _read_consents(system_db)
    output["system_consents"] = system_consents
    if system_err:
        output["errors"].append(f"system_db: {system_err}")

    # Heuristics: surface python/sovyx/terminal entries (most likely to be
    # the chain Sovyx runs through).
    interesting_keywords = ["python", "sovyx", "terminal", "iterm", "vscode",
                             "cursor", "warp", "tmux"]
    flagged = []
    for c in user_consents + system_consents:
        client_lower = (c.get("client") or "").lower()
        for kw in interesting_keywords:
            if kw in client_lower:
                flagged.append({
                    "client": c["client"],
                    "auth": c["auth_value_name"],
                    "reason": c.get("auth_reason_name"),
                    "modified": c.get("last_modified_iso"),
                    "matched_keyword": kw,
                })
                break
    output["interesting_chain_clients"] = flagged

    out_str = json.dumps(output, indent=2)
    if args.out == "-":
        print(out_str)
    else:
        Path(args.out).write_text(out_str)
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
