# CI Pipeline

**Audience**: contributors and maintainers.
**Scope**: GitHub Actions workflows, job graph, triggers, runners, release automation, historical fixes.
**Status**: canonical — reflects the workflows in `.github/workflows/` and the quality-gate contract in `CLAUDE.md`.

---

## Provider

Sovyx runs on **GitHub Actions**. Three workflows cover the full lifecycle:

| Workflow         | File                             | Trigger                   | Purpose                                           |
| ---------------- | -------------------------------- | ------------------------- | ------------------------------------------------- |
| CI               | `.github/workflows/ci.yml`       | push `main`, PR, `workflow_call` | lint → typecheck → security → tests → dashboard   |
| Publish to PyPI  | `.github/workflows/publish.yml`  | tag `v*`                  | CI gate → sdist+wheel → PyPI (OIDC) → GH Release  |
| Docker Build     | `.github/workflows/docker.yml`   | tag `v*`                  | multi-arch image → `ghcr.io/sovyx-ai/sovyx`       |

There is no separate `lint-only` or `nightly` workflow. Everything you need to pass on `main` is in `ci.yml`.

---

## Triggers

### Push to `main`
Runs the full CI pipeline. Must stay green — a red `main` blocks everyone.

### Pull request to `main`
Runs the full CI pipeline. Merge is gated on success.

### Tag `vX.Y.Z`
Runs `publish.yml` **and** `docker.yml` in parallel. `publish.yml` invokes `ci.yml` via `workflow_call` as a gate before building distribution artifacts, so a broken `main` cannot be released even if you tag it.

### `workflow_call`
`ci.yml` declares `workflow_call` under `on:` so `publish.yml` can re-run it as a dependency. This guarantees PyPI publishes never bypass the quality gates.

### Concurrency
`ci.yml` uses `concurrency: group: ${{ github.workflow }}-${{ github.ref }}, cancel-in-progress: true` — if you push twice to the same PR, the older run is cancelled.

`publish.yml` uses `cancel-in-progress: false` because cancelling a half-published release would leave PyPI in a worse state than finishing it.

---

## CI job graph

All jobs in `ci.yml` run **in parallel** on the self-hosted runner pool. There are no explicit `needs:` between them — each is independent, and the workflow is green only if every job is green.

```
push / PR / tag (via workflow_call)
           │
           ├──► lint        (ruff check + format)
           ├──► typecheck   (mypy strict)
           ├──► security    (bandit)
           ├──► dashboard   (tsc -b + vitest + vite build + static verify)
           └──► test        (pytest 3.11 + 3.12, matrix)
                   │
                   └──► upload coverage.xml (3.12 only)
```

Because jobs are independent, a ruff failure and a mypy failure surface simultaneously — one push, full picture, no drip-feed.

---

## Jobs in detail

### `lint` — ruff

```yaml
runs-on: sovyx-4core
steps:
  - uses: actions/checkout@v4
  - uses: astral-sh/setup-uv@v5
  - run: uv lock --check              # fail fast if uv.lock is stale
  - run: uv sync --dev --frozen
  - run: uv run ruff check src/ tests/
  - run: uv run ruff format --check src/ tests/
```

Both `ruff check` and `ruff format --check` must pass. `--frozen` forbids lockfile drift — if `pyproject.toml` changed without a matching `uv.lock` bump, this job fails at `uv lock --check`.

Rule selection (`pyproject.toml` → `[tool.ruff.lint] select`): `E, F, W, I, N, UP, ANN, B, A, SIM, TCH`. Tests get per-file ignores for a small set of annotation rules — see the `[tool.ruff.lint.per-file-ignores]` block.

### `typecheck` — mypy strict

```yaml
- run: uv run mypy src/
```

`pyproject.toml` sets `strict = true` and `disallow_untyped_defs = true`. There are narrow per-module overrides (`tool.mypy.overrides`) for third-party libraries that don't ship type stubs (`onnxruntime`, `zeroconf`, `boto3`, `litellm`, etc.). Never add `# type: ignore` without a narrow code (`# type: ignore[arg-type]`) and a comment explaining why.

### `security` — bandit

```yaml
- run: uv run bandit -r src/sovyx/ --configfile pyproject.toml
```

Configuration from `pyproject.toml`:

```toml
[tool.bandit]
exclude_dirs = ["tests"]
skips = ["B101", "B104", "B105", "B106", "B110", "B311", "B404", "B603", "B607", "B608"]
```

Skips are deliberate — e.g. `B101` (assert) is kept out because mypy strict already guarantees assertion semantics. Do not extend the skip list without an ADR.

### `dashboard` — build + test

```yaml
runs-on: sovyx-4core
steps:
  - uses: actions/checkout@v4
    with: { submodules: true }        # required — dashboard is a submodule
  - uses: actions/setup-node@v4
    with:
      node-version: "20"
      cache: npm
      cache-dependency-path: dashboard/package-lock.json
  - run: npm ci                        # working-directory: dashboard
  - run: npx tsc -b tsconfig.app.json
  - run: npx vitest run
  - run: npm run build
  - run: test -f src/sovyx/dashboard/static/index.html
```

The final `test -f` step is load-bearing: the Python wheel bundles the prebuilt dashboard assets from `src/sovyx/dashboard/static/`. If this file is missing, the packaged distribution is broken.

### `test` — pytest matrix

```yaml
runs-on: sovyx-4core
timeout-minutes: 25
strategy:
  fail-fast: true
  matrix:
    python-version: ["3.11", "3.12"]
steps:
  - uses: actions/checkout@v4
  - uses: astral-sh/setup-uv@v5
  - run: uv python install ${{ matrix.python-version }}
  - run: |
      uv sync --dev --frozen --python ${{ matrix.python-version }}
      uv pip install --python ${{ matrix.python-version }} \
        "ddgs>=9.0" "trafilatura>=2.0"
  - run: |
      timeout --kill-after=30s 600s \
        uv run python -m pytest tests/ --ignore=tests/smoke \
          --timeout=30 -v 2>&1 | tee "$LOG"
      # then grep the log to decide exit code
```

Notes:

1. **Two Python versions.** Every test must pass on both 3.11 and 3.12. `fail-fast: true` means the first failure cancels the other leg — saves runner time.
2. **`--ignore=tests/smoke`.** Smoke tests run separately (they hit external services or need a live daemon) and are not part of the CI gate.
3. **`--timeout=30`.** CI uses 30 s to tolerate slow cold starts; locally `--timeout=20` is the recommended default (see `testing.md`).
4. **The `timeout | tee | grep` shell dance** exists because pytest occasionally hangs after printing the summary (non-daemon threads, unreleased event-loop resources). The wrapper trusts the summary: if it reads `N passed ... in X.Ys`, exit 0; if it reads `N failed`, exit 1; if no summary at all, exit 1 with a clear "hung before completion" message. This pattern was introduced as part of the 2026-04-13 deadlock fix (see below).
5. **`ddgs` and `trafilatura` are installed separately** because they are `[project.optional-dependencies].search` and CI does not pull optional groups by default.
6. **Coverage upload.** Only the 3.12 matrix leg uploads `coverage.xml` as an artifact — one leg is enough for the `fail_under = 95` gate, and duplicating the upload would just race.

---

## Quality gates (the CI contract)

The commands below are **the exact ones CI runs**. Running them locally before pushing is mandatory (`CLAUDE.md` → Quality Gates).

```bash
# ── Python ──
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
uv run bandit -r src/sovyx/ --configfile pyproject.toml
uv run pytest tests/ --timeout=20

# ── Dashboard ──
cd dashboard
npx tsc -b tsconfig.app.json
npx vitest run
```

If you can run all eight commands cleanly, CI will pass. If any one fails, CI will fail the same way.

---

## Runners

### `sovyx-4core` — self-hosted

All `ci.yml` jobs run on the **`sovyx-4core`** runner group: self-hosted, 4-core, GitHub-Team plan. Provisioned **2026-04-13** as part of the CI deadlock fix (see below). The self-hosted pool gives us:

- Deterministic CPU/RAM — `ubuntu-latest` shared runners vary wildly under load, and async cleanup in Sovyx is sensitive to worker timing.
- Cached `uv` and `npm` layers across jobs (via the native `setup-uv` / `setup-node` cache backends).
- Network egress to GHCR for Docker push without rate-limit surprises.

### `ubuntu-latest` — hosted

`publish.yml` and `docker.yml` run on `ubuntu-latest` because they're I/O bound (uploading to PyPI, building multi-arch images with QEMU) and don't need the deterministic CPU of the self-hosted pool.

---

## Deadlock CI history (2026-04-13)

**Background.** Between early April 2026 and 2026-04-12, CI `test` jobs intermittently hung for ~20 minutes after printing the pytest summary. No tests failed; the process simply never exited. This caused flaky reruns, blocked merges, and wasted roughly 4× the expected runner budget.

**Root cause (three-way interaction).**

1. `aiosqlite 0.20.x` had a known deadlock when the connection's worker thread received a cancellation during the async context exit and a writer coroutine was still pending. The thread parked on a condition that nothing would signal. This manifested only under pytest concurrency.
2. `pytest-asyncio 0.26` introduced stricter loop scoping that, combined with `asyncio_mode=auto`, left the default loop in a state where non-daemon threads outlived the test session without a shutdown hook.
3. `pytest-xdist` workers amplified the problem: each subprocess reimported `aiosqlite` and produced independent hung threads.

**Fix.**

- Bumped `aiosqlite`: `0.20 → 0.22.1` (includes the upstream deadlock fix and the conditional-wait timeout guard).
- Bumped `pytest-asyncio`: `0.26 → 1.3` (proper loop teardown, cleaner interop with `asyncio_mode=auto`).
- Kept `pytest-xdist` but paired with the shell-level `timeout` wrapper (see the `test` job above) so that any residual hang is killed at 600 s instead of the 20 min the runner would otherwise spend on async cleanup.
- Migrated CI to the dedicated `sovyx-4core` self-hosted runner pool so timing is reproducible.

**Outcome.** Full suite (4,900+ tests, 3.11 + 3.12 matrix, dashboard build, lint, typecheck, bandit) now completes in **~10 minutes** end to end. No hangs observed since the fix was merged.

**Further reading.** `docs/engineering/aiosqlite-deadlock-protocol.md` (if present in your checkout) documents the investigation and the exact reproduction. The CI wrapper script comment in `ci.yml` is the short version.

---

## Release automation

### `publish.yml` — PyPI

Trigger: `push` on tags `v*`. Concurrency group `publish-${{ github.ref }}`, `cancel-in-progress: false`.

Job graph:

```
ci (workflow_call → ci.yml)
      │
      └──► build        (uv build, verify version matches tag)
              │
              ├──► publish  (PyPI via OIDC trusted publishing)
              └──► release  (GitHub Release w/ auto notes + artifacts)
```

Key guarantees:

1. **No PyPI publish without a green CI run.** The `build` job `needs: ci`.
2. **Tag ↔ `pyproject.toml` version match is enforced.** The build step fails if `v0.10.2` is pushed while `pyproject.toml` still says `0.10.1`.
3. **sdist + wheel are verified on disk** before upload.
4. **OIDC trusted publishing** — no PyPI API token, nothing to rotate or leak. Configured at `pypi.org/manage/project/sovyx/settings/publishing/` with: repo `sovyx-ai/sovyx`, workflow `publish.yml`, environment `pypi`.
5. **GitHub Release** is created after a successful PyPI publish, with auto-generated release notes from commits since the previous tag, plus the `dist/` artifacts attached.

### `docker.yml` — GHCR multi-arch

Trigger: `push` on tags `v*`. Runs independently of `publish.yml` (no shared `needs:`).

```yaml
platforms: linux/amd64,linux/arm64
tags:
  - ghcr.io/sovyx-ai/sovyx:${VERSION}
  - ghcr.io/sovyx-ai/sovyx:latest
cache-from: type=gha
cache-to:   type=gha,mode=max
```

Uses `GITHUB_TOKEN` with `packages: write` permission — no external secret. Build cache is stored in GitHub Actions cache (`type=gha`), shared across tag builds to keep the multi-arch build under ~5 minutes.

---

## Local mirror of CI

If CI is green and you want to match it exactly before pushing, run:

```bash
# Python
uv lock --check
uv sync --dev --frozen
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
uv run bandit -r src/sovyx/ --configfile pyproject.toml
uv run pytest tests/ --ignore=tests/smoke --timeout=30 -v

# Dashboard
cd dashboard
npm ci
npx tsc -b tsconfig.app.json
npx vitest run
npm run build
test -f ../src/sovyx/dashboard/static/index.html
```

If any step fails, fix it before pushing. Each CI round-trip wastes minutes and fragments reasoning (`CLAUDE.md` → Debugging Rules #6).

---

## Pre-commit hooks

`.pre-commit-config.yaml` wires the fastest subset of CI checks into `git commit`:

```yaml
- repo: https://github.com/astral-sh/ruff-pre-commit
  rev: v0.3.0
  hooks:
    - id: ruff
      args: [--fix]
    - id: ruff-format

- repo: https://github.com/pre-commit/mirrors-mypy
  rev: v1.9.0
  hooks:
    - id: mypy
      additional_dependencies: [pydantic>=2.6, pydantic-settings>=2.2, typer>=0.12, structlog>=24.1]
      args: [--strict]
      pass_filenames: false
      entry: mypy src/
```

Install once per checkout:

```bash
uv run pre-commit install
```

Pre-commit is advisory, not authoritative — CI remains the single source of truth. The hook versions may lag the project's declared `ruff`/`mypy` versions by a patch release. If the pre-commit hook passes but CI fails, trust CI.

---

## Historical timeline (before the current pipeline)

- **v0.5.x** — single monolithic workflow, no matrix, no dashboard job, tests occasionally timed out at 10 min.
- **v0.6.0** — split into five independent jobs; added matrix for 3.11/3.12; added dashboard job.
- **v0.7.0** — introduced `workflow_call` and moved PyPI publish behind a CI gate.
- **2026-04-13** — deadlock fix (aiosqlite + pytest-asyncio + xdist); migrated to `sovyx-4core` self-hosted runners; added the `timeout | tee | grep` wrapper around pytest.
- **v0.10.x (current)** — stable. Full suite in ~10 min. Zero flakes over the last 30 days.

---

## References

- **`CLAUDE.md`** → Quality Gates (the mandatory command list).
- **`docs/development/contributing.md`** — local workflow, commit conventions, release procedure.
- **`docs/development/testing.md`** — test layout, patterns, xdist pitfalls.
- **`docs/development/anti-patterns.md`** — the twelve tracked anti-patterns.
- **`.github/workflows/ci.yml`** — main pipeline.
- **`.github/workflows/publish.yml`** — PyPI release pipeline.
- **`.github/workflows/docker.yml`** — container release pipeline.
- **`.pre-commit-config.yaml`** — local pre-commit hooks.
- **`pyproject.toml`** — ruff, mypy, bandit, pytest, coverage config.
- **`docs/engineering/aiosqlite-deadlock-protocol.md`** — full write-up of the 2026-04-13 investigation (if present).
