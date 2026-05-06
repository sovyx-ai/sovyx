"""R50: hardware gap -- no usable capture device on the host.

Translates triage hypothesis H9 (`HypothesisId.H9_HARDWARE_GAP`) into
a deterministic calibration rule. When the bash diag's ``A_hardware``
+ ``E_portaudio`` layers report zero capture-capable devices (no
internal mic, no headset, no USB device, no Bluetooth A2DP), the
operator's setup is missing the audio input the entire voice
pipeline requires. The rule fires regardless of OS because hardware
gap is platform-agnostic.

Why ADVISE, not SET (in v0.30.20 + permanently):

A hardware gap is by definition outside Sovyx's reach -- no software
config flip can manifest a microphone the kernel doesn't see. R50's
permanent shape is operator guidance: check for unplugged USB
devices, unpaired Bluetooth headsets, BIOS-disabled internal mics,
or distro-side audio-stack restart needed.

Priority 65 (high): hardware gap blocks the entire pipeline; fixing
it must precede signal-quality rules (R60+) that assume an existing
capture stream. Sits below R20 (APO=80), R30 (destructive filter=75),
and R40 (TCC=70) because hardware gap is the most operator-obvious
failure (operator KNOWS they don't have a mic) so resolution is
typically faster than the APO/filter/TCC scenarios.

History: introduced in v0.30.20 as T2.5.R50 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 2.
"""

from __future__ import annotations

from sovyx.voice.calibration.rules._base import (
    CalibrationRule,
    RuleContext,
    RuleEvaluation,
)
from sovyx.voice.calibration.schema import (
    CalibrationConfidence,
    CalibrationDecision,
)


class _Rule:
    rule_id = "R50_hardware_gap"
    rule_version = 1
    priority = 65
    description = (
        "No capture-capable device on the host -- advises operator to "
        "check USB / Bluetooth / BIOS / kernel module state."
    )

    def applies(self, ctx: RuleContext) -> bool:
        # Two corroborating signals: capture_card_count tells us whether
        # the kernel sees any input device at all, and the triage
        # winner cross-checks that the bash diag agreed it's a
        # hardware gap (vs e.g. permission denied, destructive filter,
        # or APO).
        if ctx.fingerprint.capture_card_count > 0:
            return False
        if ctx.triage_result is None:
            return False
        winner = ctx.triage_result.winner
        if winner is None:
            return False
        if winner.hid != "H9":
            return False
        return winner.confidence >= 0.7

    def evaluate(self, ctx: RuleContext) -> RuleEvaluation:
        winner_confidence = (
            ctx.triage_result.winner.confidence
            if ctx.triage_result is not None and ctx.triage_result.winner is not None
            else 0.0
        )
        # Per-OS guidance differs because the diagnosis paths differ.
        distro = ctx.fingerprint.distro_id
        if distro == "macos":
            checks = (
                "Plug in a USB headset OR pair a Bluetooth A2DP/HSP-capable "
                "headset via System Settings -> Bluetooth. Verify "
                "`system_profiler SPAudioDataType` lists at least one "
                "capture-capable device."
            )
        elif distro == "windows":
            checks = (
                "Open Settings -> Sound -> Input + ensure a microphone is "
                "listed. Plug in a USB headset OR pair a Bluetooth headset "
                "via Settings -> Bluetooth & devices. Verify Device Manager "
                "shows the input device under 'Audio inputs and outputs'."
            )
        else:  # linuxmint, debian, fedora, etc.
            checks = (
                "Plug in a USB headset OR ensure the laptop's internal mic "
                "is enabled in BIOS. Verify `arecord -l` lists at least one "
                "capture device. Check `lsusb` + `bluetoothctl devices` for "
                "external inputs. If a device IS attached but not listed, "
                "try `pactl list short sources` + `systemctl --user restart "
                "pipewire pipewire-pulse wireplumber` to re-probe the stack."
            )

        decisions = (
            CalibrationDecision(
                target="advice.action",
                target_class="TuningAdvice",
                operation="advise",
                value=(
                    f"No capture-capable audio device detected on this host. {checks} "
                    f"After connecting a device, re-run `sovyx doctor voice --calibrate` "
                    f"to confirm the gap is closed."
                ),
                rationale=(
                    f"fingerprint.capture_card_count == 0 (no input device "
                    f"visible to the kernel + audio stack). H9 winner "
                    f"confidence={winner_confidence:.2f}. Voice pipeline cannot "
                    f"start without at least one capture device."
                ),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                confidence=CalibrationConfidence.HIGH,
            ),
        )
        matched_conditions = (
            f"fingerprint.capture_card_count == {ctx.fingerprint.capture_card_count}",
            f"triage_result.winner.hid == 'H9' (confidence={winner_confidence:.2f} >= 0.70)",
        )
        return RuleEvaluation(decisions=decisions, matched_conditions=matched_conditions)


# Module-level singleton picked up by ``iter_rules()``.
rule: CalibrationRule = _Rule()
