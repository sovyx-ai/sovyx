<!--
Thanks for the PR. A few things before you click "Create":

- One logical change per PR. Split unrelated work into separate PRs.
- The body explains WHY. The diff already shows the what.
- If this is your first PR, read CLAUDE.md end-to-end — it catches most
  review comments before they happen.
-->

## Summary

<!-- 1–3 sentences. What changes and why. -->

## Linked issues

<!-- Closes #123, refs #456 — or "none" if this is a new piece of work. -->

## Type of change

<!-- Check all that apply. -->

- [ ] feat — new feature
- [ ] fix — bug fix
- [ ] refactor — no behaviour change
- [ ] perf — performance improvement
- [ ] test — test-only change
- [ ] docs — documentation
- [ ] chore — tooling, build, version bumps
- [ ] ci — CI / workflow changes

## Checklist

- [ ] Conventional-commit title (`<type>(<scope>): <summary>`).
- [ ] `uv lock --check` clean (no lockfile drift).
- [ ] `uv run ruff check src/ tests/` and `ruff format --check` clean.
- [ ] `uv run mypy src/` clean (strict).
- [ ] `uv run bandit -r src/sovyx/ --configfile pyproject.toml` clean.
- [ ] `uv run pytest tests/ --ignore=tests/smoke --timeout=30` passes.
- [ ] Dashboard touched? `npx tsc -b tsconfig.app.json` and `npx vitest run` clean.
- [ ] New code covered by tests; modified files keep ≥ 95 % line coverage.
- [ ] [`CHANGELOG.md`](../CHANGELOG.md) updated under `## [Unreleased]` for any user-visible change.
- [ ] [`CLAUDE.md`](../CLAUDE.md) read — this PR doesn't reintroduce any catalogued anti-pattern.

## Testing

<!--
How to exercise the change locally. Example:

```bash
uv run pytest tests/unit/brain/test_consolidation.py -v
uv run sovyx init test-mind
uv run sovyx start
# ...
```

If the change is user-visible, include a short before/after (CLI output,
dashboard screenshot, API response diff).
-->

## Screenshots / output

<!-- Optional — helpful for UI and CLI-visible changes. -->

## Notes for reviewers

<!-- Anything surprising, any trade-offs you want the reviewer to push back on. -->
