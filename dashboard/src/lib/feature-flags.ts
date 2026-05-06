/**
 * Feature flags for the Sovyx dashboard.
 *
 * v0.30.22 status: the only flag that LIVED in this file
 * (CALIBRATION_WIZARD_ENABLED) has been promoted to a runtime config
 * value sourced from the backend's
 * `GET /api/voice/calibration/feature-flag` endpoint, which mirrors
 * `EngineConfig.voice.calibration_wizard_enabled`. The Zustand
 * calibration slice (`stores/slices/calibration.ts`) caches the
 * fetched value as `calibrationFeatureFlag`; consumers (VoiceStep,
 * Settings -> Voice -> Advanced) read from there instead of from
 * this module.
 *
 * Pre-v0.30.22 callers of `CALIBRATION_WIZARD_ENABLED` should switch
 * to `useDashboardStore((s) => s.calibrationFeatureFlag?.enabled ?? false)`.
 *
 * The const is RETAINED here at the value it had pre-promotion
 * (`false`) so any forgotten import keeps the same conservative
 * default; the const is now superseded by the runtime gate.
 *
 * Future feature flags can land here as hardcoded constants if they
 * meet the bar: ship-disabled-by-default, no operator-runtime toggle,
 * unconditionally controllable via redeploy. Operator-toggleable flags
 * belong in `EngineConfig` + a backend endpoint, NOT here.
 *
 * History: introduced in v0.30.17; superseded for the calibration
 * wizard in v0.30.22 (T3.10 wire-up of mission
 * MISSION-voice-self-calibrating-system-2026-05-05.md).
 */

/**
 * @deprecated since v0.30.22. Read the runtime value from
 * `useDashboardStore((s) => s.calibrationFeatureFlag?.enabled ?? false)`.
 * The backend endpoint reflects EngineConfig.voice.calibration_wizard_enabled
 * which the operator can flip via env, system.yaml, or the
 * Settings -> Voice -> Advanced toggle.
 */
export const CALIBRATION_WIZARD_ENABLED = false;
