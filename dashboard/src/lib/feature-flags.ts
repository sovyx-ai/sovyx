/**
 * Feature flags for the Sovyx dashboard.
 *
 * Hardcoded constants (no env vars, no settings store dependency) for
 * "ship code that's off by default" gating. Flip a flag to true here
 * + redeploy to enable the feature; no .env coordination needed.
 *
 * History: introduced in v0.30.17 as part of L3 patch 2 of mission
 * MISSION-voice-self-calibrating-system-2026-05-05.md.
 */

/**
 * The L3 voice calibration wizard step in onboarding.
 *
 * v0.30.17: ships disabled by default. The frontend code is wired but
 * the conditional render in VoiceStep keeps it invisible to operators.
 *
 * v0.31.0: flipped to true alongside multi-mind FINAL GA, after
 * v0.30.17 has soaked + v0.30.18 lands UX polish + telemetry.
 *
 * The backend endpoints (POST /api/voice/calibration/start, GET
 * /jobs/{id}, POST cancel, GET preview-fingerprint, WS stream) ship
 * enabled in v0.30.16; this flag only gates the operator-visible UI.
 */
export const CALIBRATION_WIZARD_ENABLED = false;
