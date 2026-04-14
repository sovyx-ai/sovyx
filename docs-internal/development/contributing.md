# Contributing to Sovyx

**Audience**: contributors, maintainers, external developers.
**Scope**: local setup, workflow, commits, submodule flow, release, PR checklist.
**Status**: canonical — replaces the root-level `CONTRIBUTING.md` sketch and aligns with `CLAUDE.md` conventions.

---

## Welcome

Sovyx (Sovereign Minds Engine) is a persistent AI companion with real memory, cognitive loop, and brain graph. It ships as a Python library, a CLI daemon, and a React dashboard. The project is AGPL-3.0 licensed and accepts contributions via GitHub pull requests against `main`.

By contributing you agree that your contributions are licensed under **AGPL-3.0-or-later** (see `pyproject.toml:license`).

Before you start:

1. Read `CLAUDE.md` at the repo root — it is the ground truth for conventions, anti-patterns, and the exact quality gates CI enforces.
2. Skim the module gap analysis in `docs/_meta/gap-analysis.md` so you know what is feature-complete vs. `[NOT IMPLEMENTED]` before picking a task — that doc cross-references the canonical `SOVYX-BKD-IMPL-*` and `SOVYX-BKD-SPE-*` specs for every gap.
3. Open an issue (or claim an existing one) before large refactors.

---

## Local setup

### Prerequisites

- **Python 3.11 or 3.12** (CI runs both).
- **[uv](https://docs.astral.sh/uv/)** — the only supported Python workflow. `pip install` is not maintained.
- **Node.js 20** (for the dashboard). Use `nvm` or Volta.
- **Git**, with submodule support enabled.

### First-time bootstrap

```bash
# Clone with the dashboard submodule
git clone --recurse-submodules https://github.com/sovyx-ai/sovyx.git
cd sovyx

# If you forgot --recurse-submodules
git submodule update --init --recursive

# Install backend dev dependencies (frozen lockfile)
uv sync --dev --frozen

# Install dashboard dev dependencies
cd dashboard && npm ci && cd ..

# Verify the install
uv run python -m sovyx
# → Sovyx v0.10.1
```

### First run

```bash
# Initialise a local data directory and default mind
uv run sovyx init

# Start the daemon (foreground, with dashboard on :7070)
uv run sovyx start

# In another shell, tail logs
uv run sovyx logs --follow
```

All data lives under `~/.local/share/sovyx/` by default. Override with `SOVYX_DATA_DIR`. Never hardcode paths — the `EngineConfig` model validator resolves log files to `data_dir/logs/sovyx.log` (see anti-pattern #4 in `anti-patterns.md`).

---

## Workflow

### Branching — always `main`

Sovyx uses **trunk-based development**. There are no long-lived feature branches. From `CLAUDE.md`:

> **Branch:** Always `main`. No feature branches (fast iteration, CI validates).

This means:

- Small PRs land directly on `main` (squashed if necessary).
- External contributors fork the repo and open a PR from `fork:main` (or a short-lived topic branch in their fork) back to `upstream:main`.
- Long-running work lives behind feature flags in `EngineConfig`, not in a side branch.
- CI is the safety net: if anything on `main` goes red, fixing it is the highest-priority task.

### Fast-iteration cadence

1. Pull `main`, check CI is green.
2. Implement the change (one logical unit).
3. Run **all local quality gates** (see below).
4. Commit with a conventional message.
5. Push and open a PR.
6. Wait for CI; merge after green + review.

---

## Quality gates (mandatory before every commit)

These gates run in CI (`.github/workflows/ci.yml`) and must pass locally before you push. They are the same commands CI invokes — no drift.

### Python (from repo root)

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/                                         # strict mode
uv run bandit -r src/sovyx/ --configfile pyproject.toml
uv run pytest tests/ --timeout=20                        # 4900+ tests
```

### Dashboard (from `dashboard/`)

```bash
npx tsc -b tsconfig.app.json   # zero errors
npx vitest run                  # 400+ tests
```

See `ci-pipeline.md` for how each gate maps to a CI job and `testing.md` for the full test layout.

**If any gate fails, fix it before committing. Never skip.** The only sanctioned escape hatch is marking a test `@pytest.mark.skip(reason="...")` with a tracked issue number — and even then, only for genuinely blocked scenarios (e.g., platform-specific ONNX failures).

---

## Code style

All style rules are enforced by `ruff` and `mypy`. Read `pyproject.toml` (`[tool.ruff]`, `[tool.mypy]`) for the authoritative config.

Highlights:

- **Python 3.11+**, type hints on every function signature. `disallow_untyped_defs = true`.
- **Line length**: 99 characters (`[tool.ruff] line-length = 99`).
- **Imports**: sorted by ruff (`I` rule). `from __future__ import annotations` at the top of every file. Type-only imports go under `if TYPE_CHECKING:` (`TCH` rule).
- **Async**: all DB/IO is `async`. Never `threading` for IO-bound work.
- **Errors**: custom exceptions live in `engine/errors.py` and always include a `context: dict`.
- **Config**: everything via `EngineConfig` (pydantic-settings). Env vars use `SOVYX_` prefix, `__` for nesting (e.g., `SOVYX_LOG__LEVEL=DEBUG`). No hardcoded values.
- **Logging**: always `from sovyx.observability.logging import get_logger` then `logger = get_logger(__name__)`. Never `print()` or plain `logging.getLogger()`. See anti-pattern #6.
- **Docstrings**: every public class/function. First line is an imperative summary.

Dashboard (TypeScript) conventions are in `CLAUDE.md` → "Dashboard (TypeScript)". Key points:

- All API responses typed in `dashboard/src/types/api.ts` (must mirror backend exactly — backend is the source of truth).
- State in Zustand slices (`dashboard/src/stores/dashboard.ts`).
- API calls through `dashboard/src/lib/api.ts` (centralised auth + error handling).
- User-visible strings always via `useTranslation()` — i18n is not optional.

---

## Conventional commits

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) so CHANGELOG generation and tag-based automation work.

**Allowed types:**

| Type        | When to use                                              |
| ----------- | -------------------------------------------------------- |
| `feat:`     | New user-visible functionality.                          |
| `fix:`      | Bug fix (include the failure mode in the body).          |
| `refactor:` | Internal change with no behavioural delta.               |
| `test:`     | Tests only (no src change). Includes property/security.  |
| `docs:`     | Docs only.                                               |
| `chore:`    | Build, CI, deps, tooling, repository hygiene.            |
| `perf:`     | Performance improvement with a measured delta.           |
| `style:`    | Formatting only (ruff/prettier). Rare — usually auto.    |

**Format:**

```
<type>: <imperative subject, ≤72 chars>

<body — explains WHY, not WHAT. wrap at 99 chars.>

<optional footer: Refs/Fixes/Closes #123, BREAKING CHANGE: ...>
```

**Examples (from `git log`):**

```
feat: TASK-42 — add spreading-activation boost for recent episodes
fix: mypy — cast migration after name-based type check
refactor: eliminate remaining isinstance/issubclass/singleton class-identity
style: ruff format test_rpc after skip markers
```

**Rules:**

- One commit per logical change. No "WIP" or "fix ci" churn on `main`.
- All fixes for the same root cause go in a single commit (`CLAUDE.md` → Debugging Rules #4).
- Never amend after push. Create a new commit. Amend only for truly local, unpushed work.

---

## Dashboard is a submodule

The `dashboard/` directory is a **separate git repository** tracked as a submodule. Changes to the dashboard follow a two-step commit flow.

### When you touch dashboard files

```bash
# 1. Change dashboard code
cd dashboard
# ... edit src/pages/Something.tsx ...

# 2. Commit INSIDE the submodule first
git add src/pages/Something.tsx
git commit -m "feat: dashboard — new Something page"
git push origin main            # push the submodule commit

# 3. Back to the parent repo — record the new submodule SHA
cd ..
git add dashboard               # this stages the pointer update
git commit -m "chore: bump dashboard submodule to abc1234"
git push origin main
```

### Verifying the submodule pointer

CI runs `actions/checkout@v4` with `submodules: true` and then `npm ci && npm run build` inside `dashboard/`. If the submodule pointer is stale or points to a commit not on `origin/main`, the Dashboard Build job fails. Always push the submodule commit **before** the parent commit.

### Common submodule pitfalls

- Forgetting `git submodule update --init --recursive` after a fresh clone or after a parent-repo pull that bumped the submodule SHA.
- Committing parent-repo changes without pushing the corresponding submodule commit — CI will fetch a SHA that doesn't exist on the remote.
- Editing `dashboard/` files with detached HEAD — always run `cd dashboard && git checkout main` before editing.

---

## Tag → release

Releases are fully automated. The only human step is a version bump + tag.

### Release procedure

```bash
# 1. Bump the version in both files (must match — CI enforces this)
# pyproject.toml          → [project] version = "X.Y.Z"
# src/sovyx/__init__.py   → __version__ = "X.Y.Z"

# 2. Commit the bump
git commit -am "chore: bump to vX.Y.Z"

# 3. Tag and push
git tag vX.Y.Z
git push origin main --tags
```

### What happens on the tag push

The `publish.yml` workflow fires. It:

1. **Re-runs the full CI pipeline** as a gate (via `workflow_call`) — lint, typecheck, bandit, python tests on 3.11 + 3.12, dashboard build.
2. **Builds `sdist` + `wheel`** via `uv build`, including the prebuilt dashboard static assets from `src/sovyx/dashboard/static/index.html`.
3. **Verifies** tag version matches `pyproject.toml`.
4. **Publishes to PyPI** via OIDC trusted publishing — no API token, no manual step.
5. **Creates a GitHub Release** with generated release notes and the `dist/` artifacts attached.

In parallel, `docker.yml` builds **multi-arch** images (`linux/amd64`, `linux/arm64`) and pushes to `ghcr.io/sovyx-ai/sovyx:X.Y.Z` and `:latest`.

**Never** push to PyPI manually — OIDC trusted publishing is the only supported path.

### Version policy

Sovyx follows **SemVer 2.0.0**:

- `MAJOR` — breaking public API or data-schema change.
- `MINOR` — new backward-compatible feature.
- `PATCH` — backward-compatible bug fix or doc/docs-only change.

Data schema migrations live under `src/sovyx/persistence/migrations/` and must be forward-only. A schema bump that requires a migration is at least `MINOR`.

---

## PR checklist

Before requesting review, confirm:

- [ ] Branch is up to date with `main` (rebased, not merged).
- [ ] **All quality gates pass locally** (ruff, ruff format, mypy strict, bandit, pytest, tsc, vitest). No skipped gates.
- [ ] **Coverage ≥95% on every modified file** (`uv run pytest --cov=sovyx --cov-report=term-missing`). `fail_under = 95` is enforced in `pyproject.toml`.
- [ ] New/changed behaviour has tests — including edge cases and at least one property-based or adversarial test for non-trivial logic.
- [ ] No new `print()`, no new plain `logging.getLogger()`, no new `sys.modules` stubs in tests.
- [ ] No new `pytest.raises(InternalException)` — use `pytest.raises(Exception)` + assert on `type(exc).__name__` (anti-pattern #8).
- [ ] No new `patch("sovyx.module.func")` string paths — use `patch.object(imported_module, "func")` (anti-pattern #11).
- [ ] Any new enum with string values inherits from `StrEnum` (anti-pattern #9).
- [ ] Dashboard API types in `dashboard/src/types/api.ts` updated to mirror backend changes.
- [ ] `CHANGELOG.md` entry under `## [Unreleased]` — terse, user-visible.
- [ ] Commit messages follow Conventional Commits. One commit per logical change.
- [ ] PR title mirrors the primary commit subject.
- [ ] PR body explains **why** (not what — the diff shows the what).
- [ ] Docstrings on every public class/function.
- [ ] No hardcoded paths, no hardcoded secrets, no env vars without the `SOVYX_` prefix.

---

## Where to ask

- **Architecture & design questions** — open a GitHub Discussion under the "Design" category. Link the relevant ADR in `docs/architecture/decisions/` or note that a new ADR is needed.
- **Bug reports** — GitHub Issues with a minimal repro, expected vs. actual, Python version, and `sovyx doctor` output.
- **Security issues** — email `security@sovyx.ai` (see `SECURITY.md`). Do not open a public issue.
- **Contributor chat** — the `#dev` channel in the community Discord (link on `https://sovyx.ai`).

---

## References

- **`CLAUDE.md`** — ground truth for conventions, stack, anti-patterns, debugging rules, working style.
- **`docs/_meta/gap-analysis.md`** — current module status, gaps, roadmap signals.
- **`docs/development/testing.md`** — full test layout, patterns, xdist pitfalls.
- **`docs/development/ci-pipeline.md`** — CI jobs, triggers, release automation.
- **`docs/development/anti-patterns.md`** — the twelve tracked anti-patterns with code examples.
- **`pyproject.toml`** — ruff, mypy, bandit, pytest, coverage config.
- **`.github/workflows/ci.yml`** — exact commands CI runs.
- **`.github/workflows/publish.yml`** — release pipeline.
- **`.github/workflows/docker.yml`** — container publish pipeline.
- **`.pre-commit-config.yaml`** — pre-commit hooks (ruff + mypy).

### Upstream specs worth knowing

- **SOVYX-BKD-ADR-003-LICENSE-MODEL** (`vps-brain-dump/.../adrs/`) — licensing rationale (AGPL-3.0 + DCO + open-core monetization) that informs contribution terms.
- **SOVYX-BKD-SPE-015-CLI-TOOLS** — CLI contract that new commands must follow (Typer + `--json` + Rich + `DaemonClient`).
- **SOVYX-BKD-ADR-007-EVENT-ARCHITECTURE** — event bus conventions for new services.
- **SOVYX-BKD-SPE-001-ENGINE-CORE** — DI container (`ServiceRegistry`) and lifecycle rules new subsystems must respect.
