#!/usr/bin/env node
/**
 * test-lint-rule.mjs — assert the BT.B.1 ESLint rule fires.
 *
 * Writes a deliberate-violation fixture to ``os.tmpdir()`` at test
 * time, runs ESLint against it, and asserts that:
 *   * exit code is non-zero (rule fired);
 *   * the output contains all 3 violation messages (default-param +
 *     2 JSX literal sites).
 *
 * Why a temp file instead of a committed fixture:
 *   A persistent fixture in ``scripts/test-lint-rule-fixture-*.tsx``
 *   would force the day-to-day ``npx eslint .`` to either fail (3
 *   intentional errors in tree) OR carry a globalIgnore for the
 *   path — which then prevents the runner itself from linting the
 *   ﬁle. The temp-ﬁle approach sidesteps both problems: the ﬁle
 *   only exists during the test run, never gets caught by the
 *   default lint sweep, and the runner explicitly passes the path
 *   so no ignore rule masks it.
 *
 * Run via: ``node scripts/test-lint-rule.mjs`` (from dashboard/).
 *
 * The vitest wrapper at ``scripts/test-lint-rule.test.ts`` calls
 * the same flow so the lint contract is part of the standard test
 * suite.
 */

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const dashboardRoot = join(__dirname, "..");

const FIXTURE_CONTENTS = `
/**
 * Generated at test time by scripts/test-lint-rule.mjs. NOT checked
 * in. Contains deliberate violations of the BT.B.1 ESLint rule.
 */
import * as React from "react";

interface FooProps {
  mindId?: string;
}

// Violation: default-param mindId = "default"
export function Foo({ mindId = "default" }: FooProps): React.ReactElement {
  return <span>{mindId}</span>;
}

export function Bar(): React.ReactElement {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const FooAny = Foo as any;
  return (
    <div>
      {/* Violation: JSX literal mindId="default" */}
      <Foo mindId="default" />
      {/* Violation: JSX literal mind_id="default" */}
      <FooAny mind_id="default" />
    </div>
  );
}
`;

const EXPECTED_VIOLATIONS = [
  'Default-param mindId = "default"',
  'Hardcoded mindId="default"',
  'Hardcoded mind_id="default"',
];

/**
 * Write the fixture to a temp file inside dashboard/ so the eslint
 * config (flat config in dashboard/) resolves correctly. We write
 * to a subdirectory of dashboard/ — NOT os.tmpdir() — because eslint
 * v9 only applies the flat config to ﬁles inside the config's root.
 * The directory is removed in a finally block.
 */
function writeFixture() {
  const dir = mkdtempSync(join(dashboardRoot, ".lint-rule-fixture-"));
  const file = join(dir, "fixture.tsx");
  writeFileSync(file, FIXTURE_CONTENTS, "utf8");
  return { dir, file };
}

function runEslint(file) {
  return new Promise((resolve, reject) => {
    // ``shell: true`` so npx resolves to npx.cmd on Windows.
    const child = spawn(
      "npx",
      ["eslint", "--no-warn-ignored", JSON.stringify(file)],
      {
        cwd: dashboardRoot,
        env: process.env,
        shell: true,
      },
    );
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("error", reject);
    child.on("close", (code) => {
      resolve({ code, stdout, stderr });
    });
  });
}

const { dir, file } = writeFixture();
let result;
try {
  result = await runEslint(file);
} finally {
  rmSync(dir, { recursive: true, force: true });
}

const { code, stdout, stderr } = result;
const combined = stdout + "\n" + stderr;

if (code === 0) {
  console.error(
    "[test-lint-rule] FAIL: ESLint exited 0 — rule did NOT fire. " +
      "Expected violations against the temp fixture:\n" +
      combined,
  );
  process.exit(1);
}

const missing = EXPECTED_VIOLATIONS.filter(
  (msg) => !combined.includes(msg),
);
if (missing.length > 0) {
  console.error(
    "[test-lint-rule] FAIL: missing expected violations: " +
      missing.join(", ") +
      "\nFull output:\n" +
      combined,
  );
  process.exit(1);
}

console.log(
  "[test-lint-rule] OK — rule fired with all " +
    EXPECTED_VIOLATIONS.length +
    " expected violations.",
);
process.exit(0);
