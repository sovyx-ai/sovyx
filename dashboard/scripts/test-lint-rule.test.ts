/**
 * Vitest wrapper for ``test-lint-rule.mjs``. Asserts the BT.B.1
 * ESLint rule fires against a deliberate-violation fixture written
 * to a temp directory at test time.
 *
 * Why a vitest test + a standalone script:
 *   The standalone ``.mjs`` is for CI / pre-push hooks (it runs
 *   ESLint via Node + spawns one process). The vitest wrapper
 *   makes the same assertion part of the standard ``vitest run``
 *   gate so a regression that defeats the rule (e.g. someone
 *   accidentally adds the fixture path to the allowlist, or the
 *   selector regression-tests the wrong AST node) fails the test
 *   suite immediately.
 *
 * Slow-path note: this test spawns ESLint and waits for it to
 * complete (~3-5 s on a warm cache). That's well outside the
 * sub-second hot-path budget for unit tests, but is still under
 * the explicit 30 s timeout. The mission accepts the test latency
 * because the contract is load-bearing.
 */
import { describe, it, expect } from "vitest";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const dashboardRoot = join(__dirname, "..");

const FIXTURE_CONTENTS = `
import * as React from "react";

interface FooProps {
  mindId?: string;
}

export function Foo({ mindId = "default" }: FooProps): React.ReactElement {
  return <span>{mindId}</span>;
}

export function Bar(): React.ReactElement {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const FooAny = Foo as any;
  return (
    <div>
      <Foo mindId="default" />
      <FooAny mind_id="default" />
    </div>
  );
}
`;

interface EslintRun {
  code: number | null;
  stdout: string;
  stderr: string;
}

function runEslint(file: string): Promise<EslintRun> {
  return new Promise((resolve, reject) => {
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
    child.stdout.on("data", (chunk: Buffer) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk: Buffer) => {
      stderr += chunk.toString();
    });
    child.on("error", reject);
    child.on("close", (code) => {
      resolve({ code, stdout, stderr });
    });
  });
}

describe("BT.B.1 ESLint rule — block mindId='default'", () => {
  it(
    "fires against the deliberate-violation fixture with all 3 expected messages",
    { timeout: 30_000 },
    async () => {
      const dir = mkdtempSync(join(dashboardRoot, ".lint-rule-fixture-"));
      const file = join(dir, "fixture.tsx");
      writeFileSync(file, FIXTURE_CONTENTS, "utf8");
      try {
        const { code, stdout, stderr } = await runEslint(file);
        const combined = stdout + "\n" + stderr;

        // Rule must fire (non-zero exit).
        expect(code).not.toBe(0);

        // All 3 violation messages must surface.
        expect(combined).toContain('Default-param mindId = "default"');
        expect(combined).toContain('Hardcoded mindId="default"');
        expect(combined).toContain('Hardcoded mind_id="default"');
      } finally {
        rmSync(dir, { recursive: true, force: true });
      }
    },
  );
});
