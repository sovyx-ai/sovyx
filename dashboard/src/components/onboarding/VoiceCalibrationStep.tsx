/**
 * Back-compat re-export shim.
 *
 * The implementation moved to the
 * `dashboard/src/components/onboarding/voice-calibration/` subpackage
 * in v0.30.25 (T3.4 split per mission spec §6.3). This module
 * preserves the legacy import path so existing callers
 * (`VoiceStep.tsx` + tests) keep working unchanged.
 */

export { VoiceCalibrationStep } from "./voice-calibration/VoiceCalibrationStep";
