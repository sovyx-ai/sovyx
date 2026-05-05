#!/usr/bin/env python3
"""voice_diag_triage — analyst-side automated triage for sovyx-voice-diag tarballs.

Ingests a tarball/zip from any of the 3 toolkits (Linux v4.3, Windows v2,
macOS v1), validates schema, cross-correlates artifacts, and emits a
ranked RCA report in markdown.

Public API (post-T1.2 of MISSION-voice-self-calibrating-system-2026-05-05):
    * :class:`TriageResult` -- structured triage verdict (frozen dataclass)
    * :class:`HypothesisVerdict` -- one ranked hypothesis with confidence
    * :class:`HypothesisId` -- closed enum of supported hypotheses
    * :class:`SchemaValidation` -- schema validation outcome
    * :class:`AlertsSummary` -- alerts severity breakdown
    * :func:`triage_tarball` -- analyze a diag tarball, return TriageResult
    * :func:`render_markdown` -- render TriageResult as operator markdown

Usage::

    python tools/voice_diag_triage.py <tarball_path> [--out report.md]
    python tools/voice_diag_triage.py --extract-dir <already_extracted_dir>

Output:
    Markdown report to stdout (or --out file) with:
    - Schema validation result
    - Top-N hypotheses ranked by evidence strength
    - Cross-correlation findings
    - Specific actionable recommendations

Exit codes:
    0 -- analysis complete (regardless of voice bug status)
    1 -- schema validation failed (missing required field) or SUMMARY.json absent
    2 -- tarball extract/read failed
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import tarfile
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

# Windows console (cp1252) doesn't handle Unicode glyphs; force UTF-8.
if sys.platform == "win32":
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


# ====================================================================
# Public types — closed-enum verdicts + frozen dataclasses
# ====================================================================


class HypothesisId(StrEnum):
    """Closed enum of supported triage hypotheses.

    Each enum value is the short ID rendered in markdown (``H1``..``H10``).
    The full title, evidence rules, and confidence weighting live in
    :func:`_evaluate_hypotheses`. New hypotheses MUST be appended here
    (never re-numbered) so OTel telemetry cardinality stays bounded.
    """

    H1_MIC_DESTROYED = "H1"
    H2_VOICE_CLARITY_APO = "H2"
    H3_MACOS_HAL_INTERCEPTOR = "H3"
    H4_LINUX_DESTRUCTIVE_FILTER = "H4"
    H5_MIC_PERMISSION_DENIED = "H5"
    H6_SELFTEST_FAILED = "H6"
    H7_NETWORK_BLOCKED_PROVIDER = "H7"
    H8_DAEMON_CRASH = "H8"
    H9_HARDWARE_GAP = "H9"
    H10_LINUX_MIXER_ATTENUATED = "H10"


@dataclass(frozen=True, slots=True)
class HypothesisVerdict:
    """One ranked hypothesis with its evidence + confidence + suggested action."""

    hid: HypothesisId
    title: str
    confidence: float
    evidence_for: tuple[str, ...]
    evidence_against: tuple[str, ...]
    recommended_action: str | None


@dataclass(frozen=True, slots=True)
class SchemaValidation:
    """Outcome of validating SUMMARY.json against required+recommended fields."""

    ok: bool
    missing_required: tuple[str, ...]
    missing_recommended: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlertsSummary:
    """Severity breakdown of alerts.jsonl plus the error-level messages."""

    error_count: int
    warn_count: int
    info_count: int
    error_messages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TriageResult:
    """Structured output of one triage run.

    All fields are populated from the diag tarball at analysis time;
    the dataclass is fully self-contained, immutable, and sufficient
    to render the operator-facing markdown via :func:`render_markdown`
    without re-reading the tarball.
    """

    schema_version: int
    toolkit: str
    tarball_root: Path

    tool_name: str
    tool_version: str
    host: str
    captured_at_utc: str
    os_descriptor: str

    status: str
    exit_code: str
    selftest_status: str
    steps: Mapping[str, Any]
    skip_captures: bool

    schema_validation: SchemaValidation
    alerts: AlertsSummary

    hypotheses: tuple[HypothesisVerdict, ...]

    @property
    def winner(self) -> HypothesisVerdict | None:
        """Highest-confidence hypothesis with confidence >= 0.5; ``None`` otherwise.

        Used by callers (``sovyx doctor voice --full-diag``) to decide
        whether to surface a fix command directly to the operator. The
        0.5 threshold matches the markdown renderer's "high-confidence"
        cutoff (>0.7 for "Highest-confidence root cause", >0.3 for
        "Most likely (medium confidence)").
        """
        if not self.hypotheses:
            return None
        top = self.hypotheses[0]
        return top if top.confidence >= 0.5 else None


# Operator-facing fix commands keyed by hypothesis. Used by callers
# that render verdicts in non-markdown formats (e.g. the new
# ``sovyx doctor voice --full-diag`` CLI). The markdown renderer
# preserves its existing ``→`` recommendation block verbatim for
# byte-equivalence with the v0.30.13 output.
_RECOMMENDED_ACTIONS: dict[HypothesisId, str] = {
    HypothesisId.H1_MIC_DESTROYED: (
        "Cross-check H2/H3/H4 (OS-specific) for the root cause of the silence."
    ),
    HypothesisId.H2_VOICE_CLARITY_APO: (
        "Enable `capture_wasapi_exclusive=true` in `system.yaml` "
        "(Windows-specific Voice Clarity APO bypass per anti-pattern #21)."
    ),
    HypothesisId.H3_MACOS_HAL_INTERCEPTOR: (
        "Disable detected HAL interceptor OR set Sovyx mic device to "
        "bypass aggregate (Audio MIDI Setup > select physical mic)."
    ),
    HypothesisId.H4_LINUX_DESTRUCTIVE_FILTER: (
        "Unload destructive PipeWire filter modules; check "
        "`~/.config/wireplumber/` for filter-chain config."
    ),
    HypothesisId.H5_MIC_PERMISSION_DENIED: (
        "Grant mic permission to Sovyx Python (Settings > Privacy)."
    ),
    HypothesisId.H10_LINUX_MIXER_ATTENUATED: (
        "Run `sovyx doctor voice --fix --yes` to lift the attenuated mixer controls."
    ),
}


# ====================================================================
# Schema constants
# ====================================================================


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


def _has_either(summary: dict[str, Any], key_alts: str) -> bool:
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
                tar.extractall(tmp, members=safe_members, filter="data")
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


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load JSONL file (one JSON object per line)."""
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            out.append(json.loads(line))
    return out


# ====================================================================
# Schema validation
# ====================================================================


def _validate_schema(summary: dict[str, Any]) -> SchemaValidation:
    missing_required: list[str] = []
    missing_recommended: list[str] = []
    warnings: list[str] = []

    for field in REQUIRED_SUMMARY_FIELDS:
        if field not in summary:
            missing_required.append(field)
    for field in RECOMMENDED_SUMMARY_FIELDS:
        if not _has_either(summary, field):
            missing_recommended.append(field)
    if summary.get("schema_version") != 1:
        warnings.append(f"schema_version={summary.get('schema_version')} — expected 1")

    return SchemaValidation(
        ok=not missing_required,
        missing_required=tuple(missing_required),
        missing_recommended=tuple(missing_recommended),
        warnings=tuple(warnings),
    )


def _summary_get(summary: dict[str, Any], *keys: str, default: str = "?") -> str:
    """Get first available key from summary (handles v4.3 vs D1-spec naming)."""
    for k in keys:
        v = summary.get(k)
        if v is not None:
            return str(v)
    return default


def _detect_toolkit(summary: dict[str, Any]) -> str:
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
# Hypothesis builder + evaluator (internal — converted to HypothesisVerdict at boundary)
# ====================================================================


class _HypothesisBuilder:
    """Mutable scratchpad used by :func:`_evaluate_hypotheses` to accumulate
    evidence per hypothesis. Frozen into :class:`HypothesisVerdict` at the
    boundary of :func:`triage_tarball`.
    """

    def __init__(self, hid: str, title: str) -> None:
        self.hid = hid
        self.title = title
        self.evidence_for: list[str] = []
        self.evidence_against: list[str] = []
        self.confidence = 0.0  # 0.0 to 1.0

    def add_for(self, evidence: str, weight: float = 0.2) -> None:
        self.evidence_for.append(evidence)
        self.confidence = min(1.0, self.confidence + weight)

    def add_against(self, evidence: str, weight: float = 0.2) -> None:
        self.evidence_against.append(evidence)
        self.confidence = max(0.0, self.confidence - weight)


def _evaluate_hypotheses(
    root: Path, summary: dict[str, Any], toolkit: str, alerts: list[dict[str, Any]]
) -> list[_HypothesisBuilder]:
    hyps: dict[str, _HypothesisBuilder] = {}

    def get(hid: str, title: str) -> _HypothesisBuilder:
        if hid not in hyps:
            hyps[hid] = _HypothesisBuilder(hid, title)
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
    voice_captures: list[tuple[Path, float | None, str | None]] = []
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
        content: str | None = None
        with contextlib.suppress(Exception):
            content = log_path.read_text(encoding="utf-8", errors="replace")
        if content is None:
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
            dv_content: str | None = None
            with contextlib.suppress(Exception):
                dv_content = dv_file.read_text(encoding="utf-8", errors="replace")
            if dv_content is None:
                continue
            if "linux_mixer_saturated" in dv_content and "attenuated" in dv_content.lower():
                h10.add_for(
                    f"`sovyx doctor voice` reports linux_mixer_saturated (attenuation "
                    f"regime) in {dv_file.name}",
                    weight=0.9,
                )
                break
        # Also scan amixer dumps for the signature pattern.
        amixer_files = list(root.rglob("amixer_card*_scontents.txt"))
        for ax_file in amixer_files[:2]:
            ax_content: str | None = None
            with contextlib.suppress(Exception):
                ax_content = ax_file.read_text(encoding="utf-8", errors="replace")
            if ax_content is None:
                continue
            # Pattern: 'Mic Boost',0  ... Front Left: 0 [0%]
            mic_boost_zero = bool(
                re.search(
                    r"'Mic Boost'.*?Front Left:\s*0\s*\[0%\]",
                    ax_content,
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


def _to_verdict(b: _HypothesisBuilder) -> HypothesisVerdict:
    """Freeze a builder into a public :class:`HypothesisVerdict`."""
    try:
        hid = HypothesisId(b.hid)
    except ValueError as exc:  # pragma: no cover -- guard against unknown IDs
        raise ValueError(f"unknown hypothesis id: {b.hid!r} (not in HypothesisId enum)") from exc
    return HypothesisVerdict(
        hid=hid,
        title=b.title,
        confidence=b.confidence,
        evidence_for=tuple(b.evidence_for),
        evidence_against=tuple(b.evidence_against),
        recommended_action=_RECOMMENDED_ACTIONS.get(hid),
    )


# ====================================================================
# Public API
# ====================================================================


def triage_tarball(archive_or_dir: Path, *, is_extracted_dir: bool = False) -> TriageResult:
    """Analyze a diag tarball (or already-extracted directory) and return a TriageResult.

    Args:
        archive_or_dir: Path to the diag ``.tar.gz`` / ``.zip`` archive,
            or to an already-extracted root directory if
            ``is_extracted_dir=True``.
        is_extracted_dir: If ``True``, ``archive_or_dir`` is treated as
            the already-extracted root directory and no extraction is
            performed. Useful when the caller has already untarred the
            archive (e.g. for repeated analysis without re-extracting).

    Returns:
        A frozen :class:`TriageResult` containing schema validation,
        alerts summary, ranked hypotheses (highest-confidence first),
        and all metadata needed by :func:`render_markdown`.

    Raises:
        FileNotFoundError: if ``archive_or_dir`` does not exist.
        ValueError: if the archive type is unsupported, contains unsafe
            paths (CWE-22), or its ``SUMMARY.json`` is missing/malformed.
    """
    if is_extracted_dir:
        if not archive_or_dir.is_dir():
            raise FileNotFoundError(f"not a directory: {archive_or_dir}")
        root = archive_or_dir
    else:
        if not archive_or_dir.exists():
            raise FileNotFoundError(f"archive not found: {archive_or_dir}")
        root = extract_archive(archive_or_dir)

    summary_path = find_summary(root)
    if summary_path is None:
        raise ValueError(f"SUMMARY.json not found under {root}")
    try:
        summary = json.loads(summary_path.read_text())
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"SUMMARY.json malformed: {exc}") from exc

    validation = _validate_schema(summary)
    toolkit = _detect_toolkit(summary)

    alerts = load_jsonl(root / "_diagnostics" / "alerts.jsonl")
    if not alerts:
        for alt in ("alerts.jsonl",):
            for p in root.rglob(alt):
                alerts.extend(load_jsonl(p))

    builders = _evaluate_hypotheses(root, summary, toolkit, alerts)
    verdicts = tuple(sorted((_to_verdict(b) for b in builders), key=lambda v: -v.confidence))

    by_severity = Counter(a.get("severity", "unknown") for a in alerts)
    error_messages = tuple(a.get("message", "?") for a in alerts if a.get("severity") == "error")
    alerts_summary = AlertsSummary(
        error_count=by_severity.get("error", 0),
        warn_count=by_severity.get("warn", 0),
        info_count=by_severity.get("info", 0),
        error_messages=error_messages,
    )

    # Header fields (preserve byte-equivalence with v0.30.13 render_report).
    tool_name = _summary_get(summary, "tool", "script_name", default="sovyx-voice-diag")
    tool_version = _summary_get(summary, "tool_version", "script_version")
    kline = (summary.get("host_capability_summary", {}) or {}).get("kernel_line", "")
    os_str = _summary_get(summary, "os", "os_version", default=toolkit)
    if kline and os_str == toolkit:
        # Preserve the v0.30.13 quirk: list slice is rendered via repr().
        os_str = f"{toolkit} ({kline.split()[0:6] if kline else ''})"

    selftest = (summary.get("calibration", {}) or {}).get(
        "analyzer_selftest_status", summary.get("analyzer_selftest_status", "?")
    )

    return TriageResult(
        schema_version=int(summary.get("schema_version", 1) or 1),
        toolkit=toolkit,
        tarball_root=root,
        tool_name=tool_name,
        tool_version=tool_version,
        host=_summary_get(summary, "host", "hostname"),
        captured_at_utc=_summary_get(summary, "captured_at_utc", "started_utc_ns"),
        os_descriptor=os_str,
        status=str(summary.get("status", "?")),
        exit_code=str(summary.get("final_exit_code", "?")),
        selftest_status=str(selftest),
        steps=summary.get("steps", {}),
        skip_captures=bool((summary.get("flags", {}) or {}).get("skip_captures", False)),
        schema_validation=validation,
        alerts=alerts_summary,
        hypotheses=verdicts,
    )


def render_markdown(result: TriageResult) -> str:
    """Render a :class:`TriageResult` as operator-facing markdown.

    The output is byte-equivalent to the v0.30.13 ``render_report``
    function for the same diag tarball; existing analyst workflows that
    diff or grep the markdown continue to work without modification.
    """
    lines: list[str] = []
    lines.append("# Voice Diagnostic Triage Report")
    lines.append("")
    lines.append(f"**Source:** `{result.tarball_root}`")
    lines.append(f"**Toolkit:** {result.tool_name} v{result.tool_version}")
    lines.append(f"**OS:** {result.os_descriptor}")
    lines.append(f"**Host:** {result.host}")
    lines.append(f"**Captured:** {result.captured_at_utc}")
    lines.append(
        f"**Status:** {result.status} (exit={result.exit_code}) | "
        f"selftest={result.selftest_status} | steps={result.steps}"
    )
    if result.skip_captures:
        lines.append("")
        lines.append(
            "⚠️  **Phase 1 run** (`--skip-captures`) — no W captures, no audio "
            "evidence. H1/H4/H8 confidence is bounded; full RCA requires Phase 2."
        )
    lines.append("")

    # Schema validation.
    lines.append("## Schema Validation")
    lines.append("")
    if result.schema_validation.ok:
        lines.append("✅ Required fields present")
    else:
        lines.append("❌ Schema validation FAILED")
        for f in result.schema_validation.missing_required:
            lines.append(f"  - missing required: `{f}`")
    if result.schema_validation.missing_recommended:
        lines.append(
            f"⚠️  missing recommended: `{', '.join(result.schema_validation.missing_recommended)}`"
        )
    for w in result.schema_validation.warnings:
        lines.append(f"⚠️  {w}")
    lines.append("")

    # Alerts summary.
    lines.append("## Alerts")
    lines.append("")
    lines.append(f"- error: {result.alerts.error_count}")
    lines.append(f"- warn:  {result.alerts.warn_count}")
    lines.append(f"- info:  {result.alerts.info_count}")
    lines.append("")
    if result.alerts.error_count > 0:
        lines.append("### errors")
        for msg in result.alerts.error_messages:
            lines.append(f"- {msg}")
        lines.append("")

    # Hypothesis ranking.
    lines.append("## Hypothesis Ranking (by confidence)")
    lines.append("")
    for h in result.hypotheses:
        if h.confidence < 0.05 and not h.evidence_for and not h.evidence_against:
            continue
        emoji = "🔴" if h.confidence > 0.7 else "🟡" if h.confidence > 0.3 else "⚪"
        lines.append(f"### {emoji} {h.hid.value}: {h.title} (confidence={h.confidence:.2f})")
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
    top = result.hypotheses[0] if result.hypotheses else None
    if top and top.confidence > 0.7:
        lines.append(f"**Highest-confidence root cause:** {top.title}")
        lines.append("")
        if top.hid == HypothesisId.H2_VOICE_CLARITY_APO:
            lines.append(
                "→ Enable `capture_wasapi_exclusive=true` in `system.yaml` "
                "(Windows-specific Voice Clarity APO bypass per anti-pattern #21)."
            )
        elif top.hid == HypothesisId.H3_MACOS_HAL_INTERCEPTOR:
            lines.append(
                "→ Disable detected HAL interceptor OR set Sovyx mic device "
                "to bypass aggregate (Audio MIDI Setup > select physical mic)."
            )
        elif top.hid == HypothesisId.H4_LINUX_DESTRUCTIVE_FILTER:
            lines.append(
                "→ Unload destructive PipeWire filter modules; check "
                "`~/.config/wireplumber/` for filter-chain config."
            )
        elif top.hid == HypothesisId.H5_MIC_PERMISSION_DENIED:
            lines.append("→ Grant mic permission to Sovyx Python (Settings > Privacy).")
        elif top.hid == HypothesisId.H1_MIC_DESTROYED:
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
# CLI
# ====================================================================


def _cli_main() -> int:
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
        result = triage_tarball(archive_path, is_extracted_dir=args.extract_dir)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        # Distinguish "SUMMARY.json missing/malformed" (exit 1) from
        # "extract failed" (exit 2) to preserve the v0.30.13 contract.
        msg = str(e)
        if "SUMMARY.json" in msg:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(f"ERROR: extract failed: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: extract failed: {e}", file=sys.stderr)
        return 2

    report = render_markdown(result)

    if args.out == "-":
        print(report)
    else:
        Path(args.out).write_text(report)
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_cli_main())
