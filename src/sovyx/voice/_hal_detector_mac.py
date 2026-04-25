"""macOS CoreAudio HAL plugin detector (MA1).

The macOS equivalent of the Windows APO chain (``sovyx.voice._apo_detector``):
audio frames captured by CoreAudio CAN be intercepted by HAL
(Hardware Abstraction Layer) plugins installed at:

* ``/Library/Audio/Plug-Ins/HAL/`` — system-wide.
* ``~/Library/Audio/Plug-Ins/HAL/`` — per-user.

When a HAL plugin captures or reroutes audio, it can SILENTLY destroy
the signal in ways indistinguishable from user-space PortAudio
errors (mic captures BlackHole's null sink instead of the physical
mic; Loopback's virtual aggregate device intercepts the host API).
Pre-MA1 the cascade had no signal to attribute "audio is wrong" to
HAL interference; MA1 enumerates installed plugins so the dashboard
can name the suspects.

Classification taxonomy (matches MS-flavour ``_apo_detector`` for
operator mental-model parity):

* **Virtual audio drivers** — BlackHole, Loopback, Soundflower,
  VB-Cable. These are the 99% case of "user installed a virtual
  audio bus and forgot it routes the mic".
* **Audio enhancement** — Soundsource, Audio Hijack, Boom 3D.
  Process audio between CoreAudio and userspace; can apply EQ,
  noise gate, AGC.
* **OEM/vendor** — Realtek, Apple Boot Camp, Dell SmartByte. Less
  common on Mac than Windows but they do exist for boot-camped
  rigs.
* **Unknown** — every other ``.driver`` bundle in the HAL plug-in
  paths. Surfaces the bundle name + path so operators can grep.

Discovery method: filesystem listing of the two HAL directories +
substring matching against the known-plugin catalogue. NO subprocess
needed (unlike system_profiler which can take 5-10 s on cold start).
NO pyobjc binding required.

Reference: F1 inventory mission task MA1; CoreAudio HAL Plug-In
Programming Guide.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── HAL plug-in directories ───────────────────────────────────────


_SYSTEM_HAL_DIR = Path("/Library/Audio/Plug-Ins/HAL")
"""System-wide HAL plug-ins. Most virtual-audio drivers install
here (requires admin / Installer.app). Path is canonical in macOS
since 10.6; never moves between releases."""


def _user_hal_dir() -> Path:
    return Path.home() / "Library" / "Audio" / "Plug-Ins" / "HAL"


# ── Plug-in classification catalogue ──────────────────────────────


class HalPluginCategory(StrEnum):
    """Closed-set categorisation of detected HAL plug-ins."""

    VIRTUAL_AUDIO = "virtual_audio"
    """Virtual audio bus / loopback driver (BlackHole, Loopback,
    Soundflower, VB-Cable). Routes audio between apps; if installed
    AND set as the default input, Sovyx captures the bus's null
    sink instead of the real mic."""

    AUDIO_ENHANCEMENT = "audio_enhancement"
    """Real-time audio processor (Soundsource, Audio Hijack, Boom
    3D, Loopback's helper). Applies EQ / noise gate / AGC between
    CoreAudio and userspace; can subtly destroy speech features
    that VAD / STT depend on."""

    VENDOR = "vendor"
    """OEM / vendor HAL driver (Realtek, Apple Boot Camp, Dell
    SmartByte). Less common on macOS than Windows but do exist."""

    UNKNOWN = "unknown"
    """Plug-in present in the HAL directory but not matched by the
    classification catalogue. Surface the raw bundle name so
    operators can grep."""


# Curated catalogue. Substring matching against the bundle FILENAME
# (case-insensitive). Known vendor patterns; this is intentionally
# small — unknown plugins still surface via the UNKNOWN category
# with their raw bundle name.
_PLUGIN_CATALOGUE: tuple[tuple[str, str, HalPluginCategory], ...] = (
    ("blackhole", "BlackHole (virtual audio bus)", HalPluginCategory.VIRTUAL_AUDIO),
    ("loopback", "Loopback (Rogue Amoeba)", HalPluginCategory.VIRTUAL_AUDIO),
    ("soundflower", "Soundflower (legacy virtual audio)", HalPluginCategory.VIRTUAL_AUDIO),
    ("vb-cable", "VB-CABLE (VB-Audio)", HalPluginCategory.VIRTUAL_AUDIO),
    ("vbcable", "VB-CABLE (VB-Audio)", HalPluginCategory.VIRTUAL_AUDIO),
    ("audiohijack", "Audio Hijack (Rogue Amoeba)", HalPluginCategory.AUDIO_ENHANCEMENT),
    ("soundsource", "SoundSource (Rogue Amoeba)", HalPluginCategory.AUDIO_ENHANCEMENT),
    ("boom", "Boom 3D / Boom 2", HalPluginCategory.AUDIO_ENHANCEMENT),
    ("krisp", "Krisp (noise suppression)", HalPluginCategory.AUDIO_ENHANCEMENT),
    ("realtek", "Realtek HAL driver", HalPluginCategory.VENDOR),
    ("appleHDA", "Apple HDA boot camp", HalPluginCategory.VENDOR),
    ("samsung", "Samsung audio driver", HalPluginCategory.VENDOR),
)


# ── Public types ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HalPluginEntry:
    """One detected HAL plug-in."""

    bundle_name: str
    """The bundle filename (without ``.driver`` suffix). Stable
    across enumerations."""

    path: str
    """Absolute path to the plug-in bundle. Useful for dashboards
    that link to "Reveal in Finder"."""

    category: HalPluginCategory
    """Classification verdict."""

    friendly_label: str = ""
    """Human-readable label from the catalogue. Empty for UNKNOWN
    (caller renders the raw ``bundle_name`` in that case)."""


@dataclass(frozen=True, slots=True)
class HalReport:
    """Structured HAL plug-in detection outcome."""

    plugins: tuple[HalPluginEntry, ...] = field(default_factory=tuple)
    """All detected plug-ins, in stable filesystem-order then sorted
    by bundle_name to keep the report deterministic across runs."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Per-step diagnostic notes (directory missing, permission
    denied, etc.)."""

    @property
    def virtual_audio_active(self) -> bool:
        """``True`` iff at least one VIRTUAL_AUDIO plug-in is
        installed. Load-bearing predicate: when True AND the user
        is reporting "Sovyx hears nothing", the dashboard should
        suggest checking the OS default-input device first."""
        return any(p.category is HalPluginCategory.VIRTUAL_AUDIO for p in self.plugins)

    @property
    def audio_enhancement_active(self) -> bool:
        """``True`` iff at least one AUDIO_ENHANCEMENT plug-in is
        installed. Useful for attribution when speech-feature
        destruction is suspected."""
        return any(p.category is HalPluginCategory.AUDIO_ENHANCEMENT for p in self.plugins)

    @property
    def by_category(self) -> dict[str, tuple[HalPluginEntry, ...]]:
        """Plug-ins grouped by category — one tuple per category
        present. Empty categories are omitted from the dict so
        downstream dashboards don't render empty rows."""
        groups: dict[str, list[HalPluginEntry]] = {}
        for plugin in self.plugins:
            groups.setdefault(plugin.category.value, []).append(plugin)
        return {k: tuple(v) for k, v in groups.items()}


# ── Detection ─────────────────────────────────────────────────────


def detect_hal_plugins() -> HalReport:
    """Synchronous HAL plug-in detection.

    Returns:
        :class:`HalReport` with all detected plug-ins + per-step
        diagnostic notes. Never raises — non-darwin returns an
        empty report; FS / permission failures collapse into notes.
    """
    if sys.platform != "darwin":
        return HalReport(notes=(f"non-darwin platform: {sys.platform}",))

    notes: list[str] = []
    plugins: list[HalPluginEntry] = []
    for directory in (_SYSTEM_HAL_DIR, _user_hal_dir()):
        plugins.extend(_scan_dir(directory, notes))
    # Stable order by bundle_name so the report is deterministic
    # across runs (e.g. for diff-based regression).
    plugins.sort(key=lambda p: p.bundle_name.lower())
    return HalReport(plugins=tuple(plugins), notes=tuple(notes))


def _scan_dir(directory: Path, notes: list[str]) -> list[HalPluginEntry]:
    """Enumerate ``directory`` for ``.driver`` bundles and classify
    each. Returns empty list on missing directory / permission error
    — appends a note in those cases for trace observability."""
    if not directory.exists():
        notes.append(f"{directory}: directory does not exist (no plug-ins of this scope)")
        return []
    try:
        entries = list(directory.iterdir())
    except PermissionError as exc:
        notes.append(f"{directory}: permission denied ({exc!r})")
        return []
    except OSError as exc:
        notes.append(f"{directory}: read failed ({exc!r})")
        return []
    out: list[HalPluginEntry] = []
    for entry in entries:
        if entry.suffix != ".driver":
            continue
        # Bundle name is the stem (without ``.driver``).
        bundle_name = entry.stem
        category, friendly = _classify(bundle_name)
        out.append(
            HalPluginEntry(
                bundle_name=bundle_name,
                path=str(entry),
                category=category,
                friendly_label=friendly,
            ),
        )
    return out


def _classify(bundle_name: str) -> tuple[HalPluginCategory, str]:
    """Match the bundle name against the catalogue.

    Substring-matching (case-insensitive) so vendor variants like
    ``"BlackHole2ch"`` and ``"BlackHole 16ch"`` both classify as
    BlackHole virtual_audio. The first match wins — catalogue order
    matters when patterns overlap (none currently do)."""
    lowered = bundle_name.lower()
    for needle, label, category in _PLUGIN_CATALOGUE:
        if needle in lowered:
            return category, label
    return HalPluginCategory.UNKNOWN, ""


__all__ = [
    "HalPluginCategory",
    "HalPluginEntry",
    "HalReport",
    "detect_hal_plugins",
]
