# Coverage Policy — Sovyx v0.1.x

## pragma: no cover Audit (28 annotations)

All 28 `pragma: no cover` annotations have been audited and classified as **legitimate**.

### Categories

| Module | Count | Reason |
|--------|-------|--------|
| `cli/main.py` | 12 | Click CLI commands with Rich console — require click.testing.CliRunner integration |
| `engine/health.py` | 10 | OS-level health checks (/proc, psutil, subprocess) — OS-dependent |
| `engine/lifecycle.py` | 4 | PidFile PermissionError, default paths, socket cleanup — OS-dependent |
| `cli/rpc_client.py` | 2 | Socket timeout/OS errors — not reproducible in unit tests |

### Policy
- **No code logic is being masked.** All 28 are error handlers or OS-interaction paths.
- Removal target: v1.0 with proper integration test infrastructure.
- New `pragma: no cover` requires PR review justification.
