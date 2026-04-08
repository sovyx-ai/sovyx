# Coverage Policy

## pragma: no cover Audit

42 `pragma: no cover` annotations across the codebase. All audited.

### Categories

| Module | Count | Reason |
|--------|-------|--------|
| `cli/main.py` | ~15 | CLI commands with Rich console — require subprocess integration testing |
| `engine/health.py` | ~10 | OS-level health checks (/proc, psutil, subprocess) — platform-dependent |
| `engine/lifecycle.py` | ~5 | PID file permissions, socket cleanup — OS-dependent error paths |
| `cli/rpc_client.py` | ~3 | Socket timeout/OS errors — not reproducible in unit tests |
| `voice/*` | ~5 | Audio device initialization — hardware-dependent |
| `dashboard/server.py` | ~4 | Daemon startup, static file serving edge cases |

### Policy

- No business logic is excluded from coverage. All 42 are OS-interaction paths, hardware-dependent code, or CLI presentation layers.
- New `pragma: no cover` requires justification in the commit message.
- Target: reduce count via integration tests in v1.0 (subprocess-based CLI testing, Docker-based health check testing).

### Related

- `type: ignore` audit: see [docs/V10-SECURITY-ARCHITECTURE.md](docs/V10-SECURITY-ARCHITECTURE.md) section 0.4
- `noqa` audit: 153 total, dominated by `BLE001` (broad exception — documented in V10 doc section 3) and `PLC0415` (lazy imports — intentional for optional deps)
