# Security Policy

## Supported versions

Security fixes are applied to the latest minor release on `main`. Older lines do not receive backports; pin to the current PyPI version to stay covered.

| Version | Supported |
|---|---|
| Latest `0.x` release on `main` | Supported |
| Any earlier release | Not supported -- upgrade to the latest |

## Reporting a vulnerability

**Do not open a public GitHub issue for a security report.**

Email `security@sovyx.ai` with:

1. A description of the issue and the impact you believe it has.
2. A minimal reproduction (CLI invocation, HTTP request, plugin manifest — whatever makes the bug trigger).
3. The Sovyx version (`sovyx --version`) and the environment (OS, Python version, Docker tag if relevant).
4. Any suggested mitigation or patch.

If you prefer PGP, ask for the public key at the same address and we'll reply with it before you send the details.

We aim to acknowledge reports within **72 hours** and to ship a fix within **30 days** for high-severity issues. Lower-severity reports are rolled into the next patch release on the normal cadence.

## Disclosure

We follow **coordinated disclosure**:

- We work with the reporter to understand, reproduce, and fix the issue.
- A fix lands on `main` and a patched version is published to PyPI + Docker Hub.
- A CVE is requested where applicable.
- After the patched version is public and users have had at least 14 days to upgrade, a security advisory is published on the GitHub Security tab with credit to the reporter (unless anonymity is requested).

## Scope

In scope:

- The Sovyx Python package published to PyPI (`sovyx`).
- The container image published to GHCR (`ghcr.io/sovyx-ai/sovyx`).
- The dashboard bundle served from the daemon on port 7777.
- The plugin SDK and sandbox (`sovyx.plugins.*`).
- CLI RPC surface (Unix socket, JSON-RPC 2.0).

Out of scope for bounty-style reports:

- Denial of service that requires full local access to the machine running the daemon (the user already owns the box).
- Social-engineering attacks on maintainers.
- Known limitations documented in the roadmap (e.g., sandbox v2 not yet shipped).
- Issues in third-party LLM providers Sovyx happens to call (report those upstream).

## Handling API keys and secrets

- Sovyx never logs API keys, tokens, or user messages at `INFO` or below. `DEBUG` may include truncated identifiers but never full secrets.
- Dashboard tokens live in `sessionStorage` plus an in-memory fallback; `localStorage` is never used. A legacy-token migrator reads any stale `localStorage` entry into `sessionStorage` once on boot and removes the source.
- JSON rendered in the dashboard (plugin manifests, tool parameters, log extra fields) passes through `safeStringify`, which clamps oversize payloads and redacts values under secret-looking keys (`token`, `api_key`, `password`, `secret`, `authorization`, `cookie`, `private_key`, `session`, `credential`, `auth`).
- Cloud backups are encrypted client-side with Argon2id (password → key) + AES-256-GCM (per-chunk) before upload. The server storing the backup (Cloudflare R2) sees ciphertext only.

## Plugin sandbox

Plugins run in-process but under a five-layer sandbox:

1. **AST scanner** — rejects `eval`, `exec`, `subprocess`, `__import__`, plus known escape patterns (`().__class__.__base__.__subclasses__()`, `f_back`, `gi_frame`, etc.).
2. **Runtime import guard** — blocks imports of restricted modules via `sys.meta_path` (`find_spec`, PEP 451 — the pre-0.7.1 `find_module` bypass is closed).
3. **Sandboxed HTTP** — `SandboxedHttpClient` enforces per-plugin allowed-domains, rate limits, and response-size caps. Raw `httpx` from plugin code is rejected at manifest validation time.
4. **Sandboxed filesystem** — `sandbox_fs` scopes reads and writes to the plugin's own directory plus an optional shared data dir.
5. **Permission manifest** — plugins declare `network:internet`, `brain:read`, `fs:write`, etc. Each permission is shown to the user in the dashboard's approval dialog and enforced at call time by the `PermissionEnforcer`.

Plugin sandbox v2 (seccomp-BPF, namespace isolation, macOS Seatbelt) is on the roadmap for the public plugin marketplace and is not yet in production.

## Hardening posture

The following properties are enforced by CI on every commit:

- `bandit` scan of `src/sovyx/` with zero HIGH findings.
- `mypy --strict` clean on every file under `src/` (222 files).
- Token endpoints use timing-safe comparison.
- Dashboard responses carry `Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, and `Permissions-Policy` headers.
- WebSocket URLs derive their scheme from the page (`https:` → `wss:`), not a hardcoded `ws://`.

## Questions

For non-sensitive security questions that don't involve a vulnerability — general policy, sandbox design, threat model — open a GitHub Discussion. For anything that could put a running Sovyx instance at risk, email the address at the top of this file.
