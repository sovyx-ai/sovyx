# Contributing to Sovyx

Sovyx is a persistent AI companion: a Python library, a CLI daemon, and a React
dashboard. It is licensed under **AGPL-3.0-or-later**. By opening a pull
request you agree your contribution is offered under the same license.

This guide covers local setup, the development workflow, commits, the dashboard
submodule flow, the release pipeline, and the twelve anti-patterns we track.

## Quick start

### Prerequisites

- **Python 3.11 or 3.12** (CI runs both).
- **[uv](https://docs.astral.sh/uv/)** for Python — the only supported
  workflow. `pip install` is not maintained.
- **Node.js 20** for the dashboard.
- **Git** with submodule support.

### Bootstrap

```bash
# Clone with the dashboard submodule
git clone --recurse-submodules https://github.com/sovyx-ai/sovyx.git
cd sovyx

# If you forgot --recurse-submodules
git submodule update --init --recursive

# Backend dev dependencies (frozen lockfile)
uv sync --dev --frozen

# Dashboard dev dependencies
cd dashboard && npm ci && cd ..

# Sanity check
uv run python -m sovyx
```

### First run

```bash
uv run sovyx init             # create ~/.local/share/sovyx and the default mind
uv run sovyx start            # daemon + dashboard on 127.0.0.1:7777
uv run sovyx logs --follow    # in another shell
```

Data lives under `~/.local/share/sovyx/` by default. Override with
`SOVYX_DATA_DIR`. Never hardcode paths — the `EngineConfig` model validator
resolves log file paths to `data_dir/logs/sovyx.log`.

---

## Workflow

Sovyx uses **trunk-based development** on `main`. There are no long-lived
feature branches.

1. Pull `main` and confirm CI is green.
2. Implement one logical change.
3. Run **all local quality gates** (see below).
4. Commit with a conventional message.
5. Push and open a PR.
6. Merge after CI is green and review.

Long-running work lives behind a feature flag in `EngineConfig`, not in a side
branch. External contributors fork and open PRs from `fork:main` (or a
short-lived topic branch in their fork) back to `upstream:main`.

---

## Quality gates

These gates must pass locally before you push. They are the exact commands CI
runs — there is no drift.

### Python (from repo root)

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/                          # strict mode
uv run bandit -r src/sovyx/ --configfile pyproject.toml
uv run pytest tests/ --ignore=tests/smoke --timeout=30   # 8 300+ tests, coverage ≥ 95%
```

### Dashboard (from `dashboard/`)

```bash
npx tsc -b tsconfig.app.json             # zero errors
npx vitest run                            # 790+ tests
```

**If any gate fails, fix it before committing. Never skip.** The only
sanctioned escape hatch is marking a test `@pytest.mark.skip(reason="...")`
with a tracked issue, and only for genuinely blocked scenarios.

---

## Code style

Style is enforced by `ruff` and `mypy`. Read `pyproject.toml` for the
authoritative config. Highlights:

- Type hints on every function signature. `disallow_untyped_defs = true`.
- Line length: 99 characters.
- `from __future__ import annotations` at the top of every file. Type-only
  imports go under `if TYPE_CHECKING:`.
- All DB and IO is `async`. Never use `threading` for IO-bound work.
- Custom exceptions live in `engine/errors.py` and always include a
  `context: dict`.
- Config always through `EngineConfig` (pydantic-settings). Env vars use the
  `SOVYX_` prefix and `__` for nesting (`SOVYX_LOG__LEVEL=DEBUG`).
- Logging: `from sovyx.observability.logging import get_logger` then
  `logger = get_logger(__name__)`. Never `print()` or plain
  `logging.getLogger()`.
- Docstrings on every public class and function. First line is an imperative
  summary.

Dashboard conventions: all API responses typed in `dashboard/src/types/api.ts`
(backend is the source of truth); state in Zustand slices; API calls through
`dashboard/src/lib/api.ts`; all user-visible strings via `useTranslation()`.

---

## Conventional commits

Messages follow [Conventional Commits](https://www.conventionalcommits.org/).

| Type        | When to use                                              |
| ----------- | -------------------------------------------------------- |
| `feat:`     | New user-visible functionality.                          |
| `fix:`      | Bug fix (include the failure mode in the body).          |
| `refactor:` | Internal change with no behavioural delta.               |
| `test:`     | Tests only (no src change).                              |
| `docs:`     | Docs only.                                               |
| `chore:`    | Build, CI, deps, tooling, repository hygiene.            |
| `perf:`     | Performance improvement with a measured delta.           |
| `style:`    | Formatting only. Rare — usually auto.                    |

Format:

```
<type>: <imperative subject, ≤72 chars>

<body — explain WHY, not WHAT. wrap at 99 chars.>

<optional footer: Refs/Fixes/Closes #123, BREAKING CHANGE: ...>
```

Rules: one commit per logical change, all fixes for the same root cause in a
single commit, no "WIP" or "fix ci" churn on `main`, and never amend after
push.

---

## Dashboard submodule

`dashboard/` is a separate git repository tracked as a submodule. Changes
always flow through two commits.

```bash
# 1. Edit inside the submodule
cd dashboard
# ... edit src/pages/Something.tsx ...

# 2. Commit inside the submodule first, then push
git add src/pages/Something.tsx
git commit -m "feat: dashboard — new Something page"
git push origin main

# 3. Back to the parent — record the new submodule SHA
cd ..
git add dashboard
git commit -m "chore: bump dashboard submodule to abc1234"
git push origin main
```

If the submodule pointer in the parent repo references a SHA not on
`origin/main` of the submodule, the CI Dashboard Build job fails. Always push
the submodule commit **before** the parent commit.

---

## Release

Releases are fully automated. The only human step is a version bump plus a
tag.

```bash
# 1. Bump the version in both files (CI checks they match)
#    pyproject.toml        → [project] version = "X.Y.Z"
#    src/sovyx/__init__.py → __version__ = "X.Y.Z"

# 2. Commit + tag + push
git commit -am "chore: bump vX.Y.Z"
git tag vX.Y.Z
git push origin main --tags
```

On tag push `publish.yml` re-runs the full CI pipeline as a gate, builds
`sdist` + `wheel` with the prebuilt dashboard bundle, verifies the tag matches
`pyproject.toml`, publishes to PyPI via OIDC trusted publishing, and creates a
GitHub Release with generated notes. In parallel, `docker.yml` builds
multi-arch images (`linux/amd64`, `linux/arm64`) and pushes them to
`ghcr.io/sovyx-ai/sovyx:X.Y.Z` and `:latest`.

Sovyx follows SemVer 2.0.0. Schema migrations live under
`src/sovyx/persistence/migrations/` and must be forward-only; a schema bump
that requires a migration is at least `MINOR`.

---

## Anti-patterns

Twelve bugs we already paid for. If a PR reintroduces any of them, revert and
link this section.

1. **Circular imports in `observability/__init__.py`.** Use the lazy
   `__getattr__` pattern; never add eager re-exports.
2. **`sys.modules` stubs in tests.** Use `monkeypatch.setattr(module, ...)` on
   an already-imported module, or accept an injection seam in production.
3. **`LoggingConfig.console_format` vs `format`.** The field is
   `console_format`. The file handler always writes JSON and is not
   configurable. Legacy YAML is auto-migrated with a `DeprecationWarning`.
4. **Hardcoded `log_file` paths.** `LoggingConfig.log_file` defaults to `None`
   and is resolved by `EngineConfig` to `data_dir/logs/sovyx.log`.
5. **Dashboard instantiating `EngineConfig()`.** The dashboard must read
   config from `ServiceRegistry`, not create a new instance.
6. **Raw `httpx` logs in the console.** `setup_logging()` suppresses `httpx`
   to WARNING. If you see raw HTTP lines, `setup_logging()` was not called.
7. **Dashboard `LogEntry` field names.** Required fields are `timestamp`,
   `level`, `logger`, `event`. The backend normalizes `ts`, `severity`,
   `message`, and `module`.
8. **`pytest.raises(InternalClass)` under xdist.** Class identity is not
   stable under `pytest-xdist`. Use `pytest.raises(Exception)` and assert on
   `type(exc).__name__`. Production code must not use `isinstance` for
   exception dispatch either — use the class name.
9. **Plain `Enum` with string values.** Always inherit from `StrEnum`.
   Guarantees value-based comparison and survives xdist namespace
   duplication.
10. **Monkeypatching auth in tests.** Use `create_app(token="literal")`.
    Never monkeypatch `_ensure_token` or set the `_server_token` global.
11. **`patch("dotted.path")`.** String paths resolve to different module
    objects under xdist. Use `patch.object(imported_module, "function")`.
12. **Defence-in-depth in tests.** If three layers are patched "to be safe"
    and the test passes, only one of them is actually doing work. Remove the
    workaround once the real fix is in.

The same checklist applies to plain `print()` calls, plain
`logging.getLogger()`, hardcoded filesystem paths, and env vars that skip the
`SOVYX_` prefix.

---

## PR checklist

Before requesting review:

- [ ] Branch is up to date with `main` (rebased, not merged).
- [ ] All quality gates pass locally (ruff, ruff format, mypy strict, bandit,
      pytest, tsc, vitest).
- [ ] Coverage ≥ 95% on every modified file
      (`uv run pytest --cov=sovyx --cov-report=term-missing`).
- [ ] New or changed behaviour has tests, including edge cases.
- [ ] No new `print()`, no new plain `logging.getLogger()`, no `sys.modules`
      stubs.
- [ ] No new `pytest.raises(InternalException)` — use class-name asserts.
- [ ] No new `patch("sovyx.module.func")` string paths — use `patch.object`.
- [ ] New enums with string values inherit from `StrEnum`.
- [ ] Dashboard API types updated to mirror backend changes.
- [ ] `CHANGELOG.md` entry under `## [Unreleased]`.
- [ ] Conventional-commits messages, one commit per logical change.
- [ ] Docstrings on every new public class or function.
- [ ] No hardcoded paths, no hardcoded secrets, no env vars without the
      `SOVYX_` prefix.

---

## Where to ask

- **Bug reports** — GitHub Issues with a minimal repro, expected versus
  actual behaviour, Python version, and the `sovyx doctor` output.
- **Design questions** — GitHub Discussions under the "Design" category.
- **Security issues** — email `security@sovyx.ai`. Do not open a public
  issue.
- **Community chat** — the `#dev` channel in the community Discord linked
  from `https://sovyx.ai`.
