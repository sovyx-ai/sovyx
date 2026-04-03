# Contributing to Sovyx

Thank you for your interest in contributing to Sovyx.

## Setup

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Git

### Getting Started

```bash
# Clone the repository
git clone https://github.com/sovyx-ai/sovyx.git
cd sovyx

# Install dependencies (production + dev)
uv sync --dev

# Verify installation
uv run python -m sovyx
# → Sovyx v0.1.0
```

### Running Checks

```bash
# Run all checks (lint + typecheck + security + tests)
make all

# Or individually:
make lint        # ruff check
make typecheck   # mypy --strict
make security    # bandit
make test        # pytest
make test-cov    # pytest with coverage (≥95% required)

# Or use the script:
./scripts/check.sh
```

## Quality Standards

All contributions must pass:

- **ruff** — zero warnings, formatted
- **mypy --strict** — zero errors, no `Any` except JSON boundaries
- **bandit** — zero high/critical findings
- **pytest** — all tests passing, ≥95% coverage per modified file
- **Docstrings** — on every public API

## Code Style

- Python 3.11+, type hints everywhere
- Line length: 99 characters
- Imports: sorted by ruff (isort rules)
- Async: use `asyncio`, never `threading` for IO
- Errors: always inherit from `SovyxError` hierarchy
- Config: no hardcodes, use `SOVYX_` env prefix
- Tests: each test < 2s, async tests with 5s timeout

## Commit Messages

One commit per logical change:

```
feat: TASK-XX — Short description
fix: Brief description of the fix
docs: What was documented
test: What was tested
```

## Pull Requests

1. Branch from `main`
2. Run `make all` before pushing
3. PR title: `feat: TASK-XX — Description`
4. All CI checks must pass

## License

By contributing, you agree that your contributions will be licensed under AGPL-3.0.
