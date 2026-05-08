import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

// ── Sovyx v0.32.0 BT.B.1 — block the ``mindId="default"`` sentinel ──
//
// CLAUDE.md anti-pattern #35 (cross-layer config defaults are sentinels,
// not values) reincurred 5 times across v0.31.0..v0.31.7 because every
// new component that took a ``mindId`` prop was at risk of hardcoding the
// literal sentinel. The structural mitigation is the shared
// ``useResolvedMindId()`` hook (single source of truth) plus this lint
// rule that blocks the literal sentinel from appearing in production code.
//
// Patterns blocked (production code only):
//   1. JSX literal:           <Foo mindId="default" />
//   2. JSX literal:           <Foo mind_id="default" />
//   3. Default param/destruct: function Foo({ mindId = "default" })
//   4. Default param/destruct: function Foo({ mind_id = "default" })
//
// Test fixtures legitimately need to drive components with the sentinel
// to validate fallback behaviour (e.g. ``recalibrate-button.test.tsx``
// asserts the warn-once breadcrumb fires when mind id is "default") —
// the override block at the bottom of this file allowlists ``tests/`` +
// ``**/*.test.{ts,tsx}`` so those calls don't trip the rule.
const MIND_ID_DEFAULT_RULES = {
  'no-restricted-syntax': [
    'error',
    {
      selector:
        'JSXAttribute[name.name="mindId"][value.type="Literal"][value.value="default"]',
      message:
        'Hardcoded mindId="default" is forbidden — CLAUDE.md anti-pattern #35. ' +
        'Use the shared useResolvedMindId() hook from @/hooks/use-resolved-mind-id ' +
        'to resolve the active mind id; the hook owns the fallback semantics.',
    },
    {
      selector:
        'JSXAttribute[name.name="mind_id"][value.type="Literal"][value.value="default"]',
      message:
        'Hardcoded mind_id="default" is forbidden — CLAUDE.md anti-pattern #35. ' +
        'Use the shared useResolvedMindId() hook from @/hooks/use-resolved-mind-id ' +
        'to resolve the active mind id; the hook owns the fallback semantics.',
    },
    {
      // Matches: function Foo({ mindId = "default" }) {}
      // and:    const Foo = ({ mindId = "default" }) => {}
      selector:
        'AssignmentPattern[left.type="Identifier"][left.name="mindId"][right.type="Literal"][right.value="default"]',
      message:
        'Default-param mindId = "default" is forbidden — CLAUDE.md anti-pattern #35. ' +
        'Make the prop required (no default) or accept ``string | null`` and ' +
        'resolve via useResolvedMindId(). The sentinel must never originate ' +
        'in a destructure default.',
    },
    {
      selector:
        'AssignmentPattern[left.type="Identifier"][left.name="mind_id"][right.type="Literal"][right.value="default"]',
      message:
        'Default-param mind_id = "default" is forbidden — CLAUDE.md anti-pattern #35. ' +
        'Make the prop required (no default) or accept ``string | null`` and ' +
        'resolve via useResolvedMindId(). The sentinel must never originate ' +
        'in a destructure default.',
    },
  ],
}

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      ...MIND_ID_DEFAULT_RULES,
    },
  },
  // Allowlist: test fixtures legitimately need the literal sentinel to
  // exercise fallback paths. Keep the rule OFF for tests so the warn-
  // once breadcrumb assertions in recalibrate-button.test.tsx and
  // VoiceCalibrationStep.test.tsx (which deliberately pass
  // ``mindId="default"``) keep passing.
  //
  // NOTE: ``scripts/test-lint-rule-fixture-*.tsx`` is INTENTIONALLY NOT
  // in the allowlist — those files are deliberate violation fixtures
  // consumed by ``scripts/test-lint-rule.mjs`` to assert the rule fires
  // end-to-end. Adding them to the allowlist would defeat the test.
  {
    files: [
      'tests/**',
      '**/*.test.{ts,tsx}',
      '**/__tests__/**/*.{ts,tsx}',
    ],
    rules: {
      'no-restricted-syntax': 'off',
    },
  },
])
