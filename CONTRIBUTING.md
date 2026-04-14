# Contributing to Sovyx

Thanks for wanting to contribute. This file is the short version; the long version — anti-patterns, testing patterns, repo layout, release flow — lives in [`CLAUDE.md`](CLAUDE.md). **Read CLAUDE.md before your first PR.**

## Prerequisites

- Python 3.11 or 3.12 (CI runs both)
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- Node 20+ and `npm` to work on the dashboard
- A POSIX-ish shell (Linux / macOS / WSL / Git Bash on Windows)

## Local setup

```bash
git clone https://github.com/sovyx-ai/sovyx.git
cd sovyx
uv sync --dev                                    # installs runtime + dev extras
uv run sovyx --version                           # should print 0.11.0 or newer
```

Dashboard:

```bash
cd dashboard
npm install
npx vitest run                                   # ~767 tests
npx tsc -b tsconfig.app.json                     # type check
```

## Quality gates (CI-enforced)

The same commands CI runs. If one fails locally, it fails in the pull request.

```bash
# Python
uv lock --check                                  # lockfile matches pyproject.toml
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/                                 # strict
uv run bandit -r src/sovyx/ --configfile pyproject.toml
uv run python -m pytest tests/ --ignore=tests/smoke --timeout=30

# Dashboard (from dashboard/)
npx tsc -b tsconfig.app.json
npx vitest run
```

Or run everything via `make`:

```bash
make all           # lint + typecheck + security + test-cov
make lint          # ruff check
make format        # ruff format (writes)
make typecheck     # mypy strict
make test          # pytest
make test-cov      # pytest + coverage ≥95% gate
make security      # bandit
```

There's also `./scripts/check.sh` which runs the full pipeline with clear section headers.

## Coverage

Line coverage ≥95% per modified file. CI runs pytest on Python 3.11 and 3.12 in parallel. Coverage is uploaded as a CI artifact for the 3.12 job — inspect it on the workflow run page if your PR drops the number.

Tests that take longer than 30 seconds are automatically killed. Keep unit tests fast; if you need real I/O, put the test under `tests/integration/` or `tests/stress/` and document why.

## Conventions

Everything structural — Python style, async rules, typing, config, logging, error patterns, Zustand slices, API client usage — is in [`CLAUDE.md`](CLAUDE.md) under **Conventions**. TL;DR:

- Python: `from __future__ import annotations` everywhere, full type hints, no `print()`, structlog via `get_logger(__name__)`, all config through `EngineConfig`, sync CPU-bound work inside `asyncio.to_thread()`.
- Dashboard: API calls only through `src/lib/api.ts`; token in `sessionStorage`; zod validation on responses; `React.memo` on virtualized rows.
- Enums with string values: `StrEnum`, never `Enum`.

The **Anti-Patterns** section in `CLAUDE.md` lists 20 concrete footguns we've actually hit — read it end to end before touching plugins, tests, or anything under `src/sovyx/voice/` or `src/sovyx/brain/`.

## Commit messages

Conventional Commits:

```
<type>(<scope>): <short imperative summary>

<body — why, not what>
```

Types used in this repo:

- `feat:` — new feature
- `fix:` — bug fix
- `refactor:` — change that neither adds a feature nor fixes a bug
- `perf:` — performance improvement
- `test:` — test-only change
- `docs:` — documentation
- `chore:` — build, tooling, version bumps
- `ci:` — CI / workflow changes

One commit per logical change. Group related edits; split unrelated ones. The body explains **why** — the diff already shows the what.

## Pull request flow

1. Branch from `main` (or submit from a fork — we accept both).
2. Run the full quality gate locally. CI failures that you could have caught locally slow everyone down.
3. Open the PR against `main`. The template walks you through a short checklist.
4. Keep the PR focused — one concern per PR. If the commit sequence tells a clean story, reviewers appreciate a `Rebase and merge`.
5. Address review comments in new commits (don't force-push until the review is done — it hides what changed).
6. A maintainer merges once CI is green and review is approved.

## Reporting bugs / requesting features

Open a GitHub issue using the template under **Issues → New issue**. Security-sensitive reports go to the address in [`SECURITY.md`](SECURITY.md) — **do not** open a public issue for a vulnerability.

## Code of Conduct

All contributors agree to the terms in [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) (Contributor Covenant 2.1).

## License

By contributing, you agree that your contributions will be licensed under [AGPL-3.0-or-later](LICENSE), the same license that covers the rest of the project.
