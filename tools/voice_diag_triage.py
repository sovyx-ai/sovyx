#!/usr/bin/env python3
"""voice_diag_triage — analyst-side automated triage for sovyx-voice-diag tarballs.

Ingests a tarball/zip from any of the 3 toolkits (Linux v4.3, Windows v2,
macOS v1), validates schema, cross-correlates artifacts, and emits a
ranked RCA report in markdown.

Usage:
    python tools/voice_diag_triage.py <tarball_path> [--out report.md]
    python tools/voice_diag_triage.py --extract-dir <already_extracted_dir>

Output:
    Markdown report to stdout (or --out file) with:
    - Schema validation result
    - Top-N hypotheses ranked by evidence strength
    - Cross-correlation findings
    - Specific actionable recommendations

Exit codes:
    0 — analysis complete (regardless of voice bug status)
    1 — schema validation failed
    2 — tarball extract/read failed
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import tarfile
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

# Windows console (cp1252) doesn't handle Unicode glyphs; force UTF-8.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


REQUIRED_SUMMARY_FIELDS = [
    "schema_version",
]
# v4.3 toolkit fields (preferred) OR D1-spec fields (alternative naming).
RECOMMENDED_SUMMARY_FIELDS = [
    "tool|script_version",  # tool xor script_version
    "host|hostname",
    "captured_at_utc|started_utc_ns",
    "status",
]


def _has_either(summary: dict, key_alts: str) -> bool:
    return any(k in summary for k in key_alts.split("|"))


# ====================================================================
# Extract / load
# ====================================================================


def _safe_member_path(name: str) -> bool:
    """Reject path-traversal attempts (CWE-22 mitigation)."""
    if not name:
        return False
    if name.startswith("/") or name.startswith("\\"):
        return False
    parts = Path(name).parts
    return ".." not in parts and not any(p.startswith("/") for p in parts)


def extract_archive(archive: Path) -> Path:
    """Extract tarball or zip to a temp dir, return root path.

    Member paths are validated to prevent path traversal (CWE-22). Tar
    extraction also uses Python 3.12+ ``filter='data'`` when available,
    which adds a second layer of safety on metadata.
    """
    tmp = Path(tempfile.mkdtemp(prefix="voice_diag_triage_"))
    if archive.suffix in (".gz", ".tgz") or archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as tar:
            safe_members = [m for m in tar.getmembers() if _safe_member_path(m.name)]
            if len(safe_members) != len(tar.getmembers()):
                rejected = [m.name for m in tar.getmembers() if not _safe_member_path(m.name)]
                raise ValueError(f"tarball contains unsafe paths: {rejected[:5]}")
            try:
                tar.extractall(tmp, members=safe_members, filter="data")  # type: ignore[arg-type]
            except TypeError:
                # Python <3.12: members already validated above by _safe_member_path.
                tar.extractall(tmp, members=safe_members)  # nosec B202
    elif archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            safe_names = [n for n in z.namelist() if _safe_member_path(n)]
            if len(safe_names) != len(z.namelist()):
                rejected = [n for n in z.namelist() if not _safe_member_path(n)]
                raise ValueError(f"zip contains unsafe paths: {rejected[:5]}")
            for name in safe_names:
                z.extract(name, tmp)
    else:
        raise ValueError(f"unknown archive type: {archive.suffix}")
    # Find root dir (only one entry expected at top level).
    entries = list(tmp.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return tmp


def find_summary(root: Path) -> Path | None:
    """Locate SUMMARY.json (sovyx-voice-diag*) or sovyx-voice-diagnostic.json (Windows)."""
    for name in ("SUMMARY.json", "sovyx-voice-diagnostic.json"):
        candidates = list(root.rglob(name))
        if candidates:
            return candidates[0]
    return None


def load_jsonl(path: Path) -> list[dict]:
    """Load JSONL file (one JSON object per line)."""
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            out.append(json.loads(line))
    return out


# ====================================================================
# Schema validation
# ====================================================================


def validate_schema(summary: dict) -> dict:
    result = {
        "ok": True,
        "missing_required": [],
        "missing_recommended": [],
        "warnings": [],
    }
    for field in REQUIRED_SUMMARY_FIELDS:
        if field not in summary:
            result["missing_required"].append(field)
            result["ok"] = False
    for field in RECOMMENDED_SUMMARY_FIELDS:
        if not _has_either(summary, field):
            result["missing_recommended"].append(field)
    if summary.get("schema_version") != 1:
        result["warnings"].append(f"schema_version={summary.get('schema_version')} — expected 1")
    return result


def _summary_get(summary: dict, *keys: str, default: str = "?") -> str:
    """Get first available key from summary (handles v4.3 vs D1-spec naming)."""
    for k in keys:
        v = summary.get(k)
        if v is not None:
            return str(v)
    return default


def detect_toolkit(summary: dict) -> str:
    tool = (summary.get("tool", "") or summary.get("script_name", "") or "").lower()
    if "windows" in tool or "voice-diagnostic" in tool:
        return "windows"
    if "mac" in tool:
        return "macos"
    # v4.3 Linux: detect via host_capability_summary.kernel_line.
    kline = (summary.get("host_capability_summary", {}) or {}).get("kernel_line", "")
    if "Darwin" in kline:
        return "macos"
    if "Microsoft" in kline or "WSL" in kline:
        return "linux"  # WSL still Linux semantics
    return "linux"


# ====================================================================
# Hypothesis ranking
# ====================================================================


class Hypothesis:
    def __init__(self, hid: str, title: str) -> None:
        self.hid = hid
        self.title = title
        self.evidence_for: list[str] = []
        self.evidence_against: list[str] = []
        self.confidence = 0.0  # 0.0 to 1.0
        self.actionable = ""

    def add_for(self, evidence: str, weight: float = 0.2) -> None:
        self.evidence_for.append(evidence)
        self.confidence = min(1.0, self.confidence + weight)

    def add_against(self, evidence: str, weight: float = 0.2) -> None:
        self.evidence_against.append(evidence)
        self.confidence = max(0.0, self.confidence - weight)


def evaluate_hypotheses(
    root: Path, summary: dict, toolkit: str, alerts: list[dict]
) -> list[Hypothesis]:
    hyps: dict[str, Hypothesis] = {}

    def get(hid: str, title: str) -> Hypothesis:
        if hid not in hyps:
            hyps[hid] = Hypothesis(hid, title)
        return hyps[hid]

    # --- H1: Mic dead/muted/APO-destroyed (cross-OS) ---
    h1 = get(
        "H1", "Microphone signal destroyed upstream of user-space (APO/HAL plug-in/mute/hardware)"
    )
    silence_alerts = [
        a
        for a in alerts
        if "silence_across" in a.get("message", "")
        or "voice_clarity_destroying" in a.get("message", "")
    ]
    for a in silence_alerts:
        h1.add_for(f"alert: {a['message'][:200]}", weight=0.5)
    # Look at capture analysis files.
    capture_files = list(root.rglob("analysis.json"))
    voice_captures = []
    for cf in capture_files:
        if "silence" in str(cf).lower() or "W14" in cf.name:
            continue
        try:
            d = json.loads(cf.read_text())
            voice_captures.append((cf, d.get("rms_dbfs"), d.get("classification")))
        except Exception:  # noqa: BLE001
            pass
    silent_count = sum(1 for _, rms, _ in voice_captures if rms is not None and rms < -85.0)
    if voice_captures and silent_count == len(voice_captures) and silent_count >= 3:
        h1.add_for(
            f"all {silent_count}/{len(voice_captures)} voice captures silent (rms < -85 dBFS)",
            weight=0.4,
        )
    elif voice_captures and silent_count == 0:
        h1.add_against(f"{len(voice_captures)} captures non-silent — mic alive", weight=0.4)

    # --- H2: Voice Clarity APO active (Windows-specific) ---
    if toolkit == "windows":
        h2 = get("H2", "Windows Voice Clarity APO active and destroying signal (anti-pattern #21)")
        # Check audio_endpoints in summary or sovyx-voice-diagnostic.json.
        endpoints = summary.get("audio_endpoints", [])
        active_with_apo = [
            e for e in endpoints if e.get("is_active") and e.get("voice_clarity_active")
        ]
        for e in active_with_apo:
            h2.add_for(
                f"endpoint '{e.get('friendly_name')}' has voice_clarity_active=true", weight=0.4
            )
        # Comparator verdict.
        live = summary.get("live_captures", {})
        if isinstance(live, dict):
            v = live.get("verdict", "")
            if v == "voice_clarity_destroying_apo_confirmed":
                h2.add_for(
                    f"WASAPI comparator verdict: {v} "
                    f"(delta_rms={live.get('delta_rms_dbfs')}, "
                    f"delta_vad={live.get('delta_vad')})",
                    weight=0.6,
                )
            elif v == "apo_not_culprit":
                h2.add_against(f"WASAPI comparator verdict: {v}", weight=0.6)

    # --- H3: macOS HAL interceptor (Krisp/Loopback rerouted default) ---
    if toolkit == "macos":
        h3 = get("H3", "macOS HAL plug-in/AU intercepted default mic (Krisp/Loopback/BlackHole)")
        hal_classifier = root / "D_coreaudiod" / "hal_classifier.json"
        if hal_classifier.exists():
            try:
                d = json.loads(hal_classifier.read_text())
                interceptors = d.get("interceptors_detected", [])
                for i in interceptors:
                    h3.add_for(
                        f"detected: {i.get('matched_vendor')} at {i.get('path')}", weight=0.4
                    )
            except Exception:  # noqa: BLE001
                pass

    # --- H4: ALSA/PulseAudio destructive filter loaded (Linux) ---
    if toolkit == "linux":
        h4 = get(
            "H4",
            "Linux PipeWire/PulseAudio destructive filter loaded (echo-cancel/rnnoise/webrtc)",
        )
        for a in alerts:
            if (
                "destructive pactl modules" in a.get("message", "")
                or "destructive filter" in a.get("message", "")
                or "PipeWire DSP filters" in a.get("message", "")
            ):
                h4.add_for(f"alert: {a['message'][:200]}", weight=0.4)

    # --- H5: Mic permission denied (cross-OS) ---
    h5 = get("H5", "Microphone permission denied to Sovyx process")
    if toolkit == "windows":
        consent = summary.get("consent_store", {})
        if isinstance(consent, dict):
            global_val = consent.get("user_global_value")
            if global_val == 0:
                h5.add_for("ConsentStore user_global=0 (denied)", weight=0.5)
            for app in consent.get("nonpackaged_apps", []):
                if "python" in (app.get("app_path_enc") or "").lower() and app.get("value") == 0:
                    h5.add_for(f"NonPackaged python denied: {app.get('app_path_enc')}", weight=0.5)
    if toolkit == "macos":
        tcc = root / "F_session" / "tcc_mic_consents.json"
        if tcc.exists():
            try:
                d = json.loads(tcc.read_text())
                if d.get("fda_status") != "granted":
                    h5.add_for(
                        f"TCC.db not readable (fda_status={d.get('fda_status')}); "
                        "cannot rule out denial",
                        weight=0.1,
                    )
                for c in d.get("interesting_chain_clients", []):
                    if c.get("auth") == "denied":
                        h5.add_for(f"TCC denied: {c.get('client')}", weight=0.5)
                    elif c.get("auth") == "allowed":
                        h5.add_against(f"TCC allowed: {c.get('client')}", weight=0.2)
            except Exception:  # noqa: BLE001
                pass

    # --- H6: Selftest failed (cross-OS, mid-confidence in EVERYTHING) ---
    h6 = get("H6", "Analyzer selftest failed — downstream metrics suspect")
    if summary.get("analyzer_selftest_status") == "fail":
        h6.add_for("analyzer_selftest_status=fail — analysis pipeline contaminated", weight=1.0)

    # --- H7: Network blocked LLM provider (cross-OS) ---
    h7 = get("H7", "Network blocked LLM/STT/TTS provider — voice pipeline stalls on cloud call")
    network_data = summary.get("network_llm", [])
    # NOTE: Linux toolkit currently doesn't put network_llm in SUMMARY.json;
    # extending H7 to scan I_network/* artifacts would be a future improvement.
    failed_endpoints = [n for n in network_data if not n.get("dns_ok") or not n.get("tcp_ok")]
    for n in failed_endpoints:
        h7.add_for(
            f"unreachable: {n.get('host')} dns={n.get('dns_ok')} tcp={n.get('tcp_ok')}", weight=0.3
        )

    # --- H8: Sovyx daemon crash / unhandled exception (cross-OS) ---
    h8 = get("H8", "Sovyx daemon crashed or threw unhandled exception in voice pipeline")
    sovyx_log_paths = list(root.rglob("sovyx.log")) + list(root.rglob("sovyx_log_tail.txt"))
    for log_path in sovyx_log_paths[:3]:  # cap to avoid huge scans
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        # Heuristic patterns for Python crashes / Sovyx-specific failures.
        crash_patterns = [
            (r"Traceback \(most recent call last\)", 0.4, "Python traceback"),
            (r"ERROR.*voice", 0.2, "voice-tagged error log line"),
            (r"InputStream.*[Ee]rror", 0.3, "InputStream error"),
            (r"PortAudioError", 0.4, "PortAudio error"),
            (r"sounddevice.*PortAudioError", 0.4, "sounddevice PortAudio error"),
            (r"VAD.*onnx.*[Ee]rror", 0.3, "Silero VAD ONNX error"),
            (r"ImportError.*sounddevice", 0.5, "sounddevice import failed"),
            (r"FileNotFoundError.*\.onnx", 0.4, "ONNX model file missing"),
        ]
        for pattern, weight, label in crash_patterns:
            import re

            if re.search(pattern, content):
                h8.add_for(f"{label} found in {log_path.name}", weight=weight)
                break  # one match per log file is enough

    # --- H9: Hardware capability gap (no capture-capable card / device dead) ---
    h9 = get("H9", "Hardware capability gap — no capture-capable audio device detected")
    if toolkit == "linux":
        target_card_path = root / "C_alsa" / "target_card.txt"
        if target_card_path.exists():
            content = target_card_path.read_text(errors="replace").strip()
            if content == "UNRESOLVED":
                h9.add_for(
                    "ALSA target_card UNRESOLVED — no card with PCM capture detected", weight=0.7
                )
    if toolkit == "macos":
        coreaudio_dump = root / "C_coreaudio" / "coreaudio_dump.json"
        if coreaudio_dump.exists():
            try:
                d = json.loads(coreaudio_dump.read_text())
                input_devs = [
                    dev
                    for dev in d.get("devices", [])
                    if dev.get("stream_format_input") and dev.get("is_alive")
                ]
                if not input_devs:
                    h9.add_for("CoreAudio enumerated 0 alive input devices", weight=0.7)
            except Exception:  # noqa: BLE001
                pass
    if toolkit == "windows":
        endpoints = summary.get("audio_endpoints", [])
        active_input = [e for e in endpoints if e.get("is_active")]
        if endpoints and not active_input:
            h9.add_for(
                "Windows MMDevices Capture has 0 ACTIVE endpoints (all disabled/unplugged)",
                weight=0.7,
            )

    # --- H10: Linux ALSA mixer attenuated (Mic Boost zero + Capture low) ---
    # First seen: VAIO VJFE69F11X-B0221H pilot (2026-04-25). Sovyx detects
    # this via `sovyx doctor voice` step 9 (linux_mixer_saturated code with
    # attenuation_warning=true in details). Fix shipped in the same release:
    # `sovyx doctor voice --fix` lifts the attenuated controls.
    if toolkit == "linux":
        h10 = get(
            "H10",
            "Linux ALSA mixer attenuated — capture+boost below Silero VAD floor "
            "(`sovyx doctor voice --fix` remediates)",
        )
        # Look for doctor_voice output files in the tarball.
        doctor_voice_files = list(root.rglob("doctor_voice.txt"))
        for dv_file in doctor_voice_files:
            try:
                content = dv_file.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            if "linux_mixer_saturated" in content and "attenuated" in content.lower():
                h10.add_for(
                    f"`sovyx doctor voice` reports linux_mixer_saturated (attenuation "
                    f"regime) in {dv_file.name}",
                    weight=0.9,
                )
                break
        # Also scan amixer dumps for the signature pattern.
        amixer_files = list(root.rglob("amixer_card*_scontents.txt"))
        for ax_file in amixer_files[:2]:
            try:
                content = ax_file.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            # Pattern: "Mic Boost" with current 0 (zeroed) AND "Capture" present.
            import re

            # 'Mic Boost',0  ... Front Left: 0 [0%]
            mic_boost_zero = bool(
                re.search(
                    r"'Mic Boost'.*?Front Left:\s*0\s*\[0%\]",
                    content,
                    re.DOTALL,
                )
            )
            if mic_boost_zero:
                h10.add_for(
                    f"amixer dump in {ax_file.name} shows 'Mic Boost' = 0/3 (zeroed)",
                    weight=0.4,
                )
                break

    return list(hyps.values())


# ====================================================================
# Report rendering
# ====================================================================


def render_report(
    summary: dict,
    toolkit: str,
    validation: dict,
    alerts: list[dict],
    hyps: list[Hypothesis],
    root: Path,
) -> str:
    lines = []
    lines.append("# Voice Diagnostic Triage Report")
    lines.append("")
    lines.append(f"**Source:** `{root}`")
    tool_name = _summary_get(summary, "tool", "script_name", default="sovyx-voice-diag")
    tool_ver = _summary_get(summary, "tool_version", "script_version")
    lines.append(f"**Toolkit:** {tool_name} v{tool_ver}")
    kline = (summary.get("host_capability_summary", {}) or {}).get("kernel_line", "")
    os_str = _summary_get(summary, "os", "os_version", default=toolkit)
    if kline and os_str == toolkit:
        os_str = f"{toolkit} ({kline.split()[0:6] if kline else ''})"
    lines.append(f"**OS:** {os_str}")
    lines.append(f"**Host:** {_summary_get(summary, 'host', 'hostname')}")
    lines.append(f"**Captured:** {_summary_get(summary, 'captured_at_utc', 'started_utc_ns')}")
    status = summary.get("status", "?")
    exit_code = summary.get("final_exit_code", "?")
    selftest = (summary.get("calibration", {}) or {}).get(
        "analyzer_selftest_status", summary.get("analyzer_selftest_status", "?")
    )
    steps = summary.get("steps", {})
    lines.append(f"**Status:** {status} (exit={exit_code}) | selftest={selftest} | steps={steps}")
    flags = summary.get("flags", {})
    if flags.get("skip_captures"):
        lines.append("")
        lines.append(
            "⚠️  **Phase 1 run** (`--skip-captures`) — no W captures, no audio "
            "evidence. H1/H4/H8 confidence is bounded; full RCA requires Phase 2."
        )
    lines.append("")

    # Schema validation.
    lines.append("## Schema Validation")
    lines.append("")
    if validation["ok"]:
        lines.append("✅ Required fields present")
    else:
        lines.append("❌ Schema validation FAILED")
        for f in validation["missing_required"]:
            lines.append(f"  - missing required: `{f}`")
    if validation["missing_recommended"]:
        lines.append(f"⚠️  missing recommended: `{', '.join(validation['missing_recommended'])}`")
    for w in validation["warnings"]:
        lines.append(f"⚠️  {w}")
    lines.append("")

    # Alerts summary.
    lines.append("## Alerts")
    lines.append("")
    by_sev = Counter(a.get("severity", "unknown") for a in alerts)
    lines.append(f"- error: {by_sev.get('error', 0)}")
    lines.append(f"- warn:  {by_sev.get('warn', 0)}")
    lines.append(f"- info:  {by_sev.get('info', 0)}")
    lines.append("")
    if by_sev.get("error", 0) > 0:
        lines.append("### errors")
        for a in alerts:
            if a.get("severity") == "error":
                lines.append(f"- {a.get('message', '?')}")
        lines.append("")

    # Hypothesis ranking.
    lines.append("## Hypothesis Ranking (by confidence)")
    lines.append("")
    hyps_sorted = sorted(hyps, key=lambda h: -h.confidence)
    for h in hyps_sorted:
        if h.confidence < 0.05 and not h.evidence_for and not h.evidence_against:
            continue
        emoji = "🔴" if h.confidence > 0.7 else "🟡" if h.confidence > 0.3 else "⚪"
        lines.append(f"### {emoji} {h.hid}: {h.title} (confidence={h.confidence:.2f})")
        if h.evidence_for:
            lines.append("**Evidence FOR:**")
            for e in h.evidence_for:
                lines.append(f"  - {e}")
        if h.evidence_against:
            lines.append("**Evidence AGAINST:**")
            for e in h.evidence_against:
                lines.append(f"  - {e}")
        lines.append("")

    # Top recommendation.
    lines.append("## Recommendation")
    lines.append("")
    top = hyps_sorted[0] if hyps_sorted else None
    if top and top.confidence > 0.7:
        lines.append(f"**Highest-confidence root cause:** {top.title}")
        lines.append("")
        if top.hid == "H2":
            lines.append(
                "→ Enable `capture_wasapi_exclusive=true` in `system.yaml` "
                "(Windows-specific Voice Clarity APO bypass per anti-pattern #21)."
            )
        elif top.hid == "H3":
            lines.append(
                "→ Disable detected HAL interceptor OR set Sovyx mic device "
                "to bypass aggregate (Audio MIDI Setup > select physical mic)."
            )
        elif top.hid == "H4":
            lines.append(
                "→ Unload destructive PipeWire filter modules; check "
                "`~/.config/wireplumber/` for filter-chain config."
            )
        elif top.hid == "H5":
            lines.append("→ Grant mic permission to Sovyx Python (Settings > Privacy).")
        elif top.hid == "H1":
            lines.append("→ Cross-check H2/H3/H4 (OS-specific) for the root cause of the silence.")
    elif top and top.confidence > 0.3:
        lines.append(
            f"**Most likely (medium confidence):** {top.title}. "
            f"Inconclusive — gather more evidence."
        )
    else:
        lines.append("No high-confidence hypothesis. Manual analysis required.")

    return "\n".join(lines)


# ====================================================================
# Main
# ====================================================================


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "archive", nargs="?", help="path to tarball/zip OR directory if --extract-dir"
    )
    parser.add_argument(
        "--extract-dir", action="store_true", help="treat path as already-extracted directory"
    )
    parser.add_argument("--out", default="-", help="output report path or '-' for stdout")
    args = parser.parse_args()

    if not args.archive:
        parser.print_help()
        return 1

    archive_path = Path(args.archive)

    try:
        if args.extract_dir:
            root = archive_path
            if not root.is_dir():
                print(f"ERROR: not a directory: {root}", file=sys.stderr)
                return 2
        else:
            if not archive_path.exists():
                print(f"ERROR: archive not found: {archive_path}", file=sys.stderr)
                return 2
            root = extract_archive(archive_path)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: extract failed: {e}", file=sys.stderr)
        return 2

    summary_path = find_summary(root)
    if not summary_path:
        print(f"ERROR: SUMMARY.json not found under {root}", file=sys.stderr)
        return 1
    try:
        summary = json.loads(summary_path.read_text())
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: SUMMARY.json malformed: {e}", file=sys.stderr)
        return 1

    validation = validate_schema(summary)
    toolkit = detect_toolkit(summary)
    alerts = load_jsonl(root / "_diagnostics" / "alerts.jsonl")
    if not alerts:
        # Try alternate locations.
        for alt in ("alerts.jsonl",):
            for p in root.rglob(alt):
                alerts.extend(load_jsonl(p))

    hyps = evaluate_hypotheses(root, summary, toolkit, alerts)
    report = render_report(summary, toolkit, validation, alerts, hyps, root)

    if args.out == "-":
        print(report)
    else:
        Path(args.out).write_text(report)
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
