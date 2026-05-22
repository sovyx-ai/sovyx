"""LLM provider health + test-connection endpoints (Mission C6 §T2.7).

* ``GET /api/llm/health`` — returns the cached
  :class:`~sovyx.llm._provider_health.LLMRouterDiscoveryReport` snapshot
  with per-provider matrix, verdict, configured/available counts. Served
  from the cached router state (refreshed by
  :class:`~sovyx.engine._llm_liveness_probe.LLMLivenessProbe` at the
  configured cadence) so the endpoint is cheap and idempotent.

* ``POST /api/llm/test-connection`` — accepts ``{provider, api_key?,
  model?}``; instantiates a TRANSIENT provider; runs the existing
  ``onboarding._test_provider`` helper; returns ``{ok, message,
  latency_ms}``. NEVER persists. NEVER hot-registers.

Both endpoints depend on :func:`verify_token` for bearer-auth parity with
``/api/engine/degraded`` and ``/api/providers``.

Response models use ``ConfigDict(extra="allow")`` per anti-pattern #40 +
Quality Gate 8 (boundary round-trip discipline) — a future Phase 1.D
extension can add fields without a breaking-change migration.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from sovyx.dashboard.routes._deps import verify_token
from sovyx.llm._provider_registry import LLMProviderKey
from sovyx.observability.logging import get_logger

# Numeric HTTP-status constant — the public contract is 422 and never
# changes regardless of which constant name Starlette is exposing on the
# current pinned version (HTTP_422_UNPROCESSABLE_ENTITY pre-0.40,
# HTTP_422_UNPROCESSABLE_CONTENT post-0.40).
_HTTP_422_UNPROCESSABLE: int = 422

if TYPE_CHECKING:
    from sovyx.llm._provider_health import LLMRouterDiscoveryReport

logger = get_logger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


class ProviderHealthEntryModel(BaseModel):
    """Pydantic shadow of :class:`~sovyx.llm._provider_health.ProviderHealthEntry`.

    Forward-additive via ``extra="allow"`` per anti-pattern #40 so future
    fields (e.g. ``last_call_at``, ``circuit_breaker_state``) can be added
    without a migration.
    """

    model_config = ConfigDict(extra="allow")
    name: str
    env_var: str
    is_cloud: bool
    configured: bool
    reachable: bool | None
    key_valid: bool | None
    failure_reason: str | None


class LLMHealthResponse(BaseModel):
    """Pydantic shadow of :class:`~sovyx.llm._provider_health.LLMRouterDiscoveryReport`."""

    model_config = ConfigDict(extra="allow")
    verdict: str
    configured_count: int
    available_count: int
    default_provider: str
    default_model: str
    per_provider: list[ProviderHealthEntryModel]
    scan_duration_ms: float


class LLMTestConnectionBody(BaseModel):
    """Body of POST /api/llm/test-connection."""

    model_config = ConfigDict(extra="forbid")
    provider: str
    api_key: str | None = None
    model: str | None = Field(default=None, description="Reserved for future per-model probes.")


class LLMTestConnectionResponse(BaseModel):
    """Response of POST /api/llm/test-connection (Mission C C.4).

    Returned on both success + failure paths; `ok` distinguishes them.
    Forward-additive via ``extra="allow"`` (anti-pattern #40)."""

    model_config = ConfigDict(extra="allow")
    ok: bool
    message: str
    latency_ms: float | None = None


def _report_to_response(report: LLMRouterDiscoveryReport) -> dict[str, Any]:
    return {
        "verdict": report.verdict.value,
        "configured_count": report.configured_count,
        "available_count": report.available_count,
        "default_provider": report.default_provider,
        "default_model": report.default_model,
        "scan_duration_ms": round(report.scan_duration_ms, 3),
        "scanned_at_monotonic": report.scanned_at_monotonic,
        "per_provider": [
            {
                "name": entry.name,
                "env_var": entry.env_var,
                "is_cloud": entry.is_cloud,
                "configured": entry.configured,
                "reachable": entry.reachable,
                "key_valid": entry.key_valid,
                "failure_reason": entry.failure_reason,
            }
            for entry in report.per_provider
        ],
    }


@router.get("/llm/health", response_model=LLMHealthResponse)
async def get_llm_health(request: Request) -> JSONResponse:
    """Return the cached LLM router discovery report.

    Served from ``LLMRouter.discovery_report`` (primed at boot by
    ``bootstrap.py`` and refreshed on every liveness-probe tick). Returns
    503 when the engine isn't running OR the router has never been primed
    (pre-first-boot edge case).

    Anti-pattern #40 compliance: ``extra="allow"`` on the response model
    keeps the contract forward-additive without a breaking migration.
    """
    engine_registry = getattr(request.app.state, "registry", None)
    if engine_registry is None:
        return JSONResponse(
            {"error": "Engine not running"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )
    try:
        from sovyx.llm.router import LLMRouter

        if not engine_registry.is_registered(LLMRouter):
            return JSONResponse(
                {"error": "LLM router not available"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )
        router_svc = await engine_registry.resolve(LLMRouter)
        report = router_svc.discovery_report
        if report is None:
            return JSONResponse(
                {"error": "Discovery report not yet primed"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )
        return JSONResponse(_report_to_response(report))
    except Exception as exc:  # noqa: BLE001 — observability-only surface
        logger.warning("llm.health.endpoint_failed", error=str(exc))
        return JSONResponse(
            {"error": "Health endpoint failed", "detail": str(exc)},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )


@router.post("/llm/test-connection", response_model=LLMTestConnectionResponse)
async def test_llm_connection(
    request: Request,
    body: LLMTestConnectionBody,
) -> JSONResponse:
    """Probe a candidate provider WITHOUT persisting or hot-registering.

    Used by the dashboard ``provider-config`` Test Connection button to
    verify operator-pasted keys before commit. Never modifies the
    process env, never updates the credential file, never adds the
    provider to the live router.

    422 on invalid provider name; 422 on missing key for cloud provider.
    503 when the LLM provider classes can't be imported (engine
    half-booted).
    """
    started = time.perf_counter()
    try:
        provider_key = LLMProviderKey(body.provider)
    except ValueError:
        valid = ", ".join(key.value for key in LLMProviderKey)
        return JSONResponse(
            {
                "ok": False,
                "message": f"Unknown provider '{body.provider}'. Valid: {valid}",
            },
            status_code=_HTTP_422_UNPROCESSABLE,
        )

    if provider_key.is_cloud:
        if not body.api_key:
            return JSONResponse(
                {
                    "ok": False,
                    "message": "api_key is required for cloud providers.",
                },
                status_code=_HTTP_422_UNPROCESSABLE,
            )
        try:
            from sovyx.dashboard.routes.onboarding import (
                _create_provider,
                _test_provider,
            )
        except ImportError as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "message": f"Provider validator unavailable: {exc}",
                },
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )
        provider_instance = _create_provider(provider_key.value, body.api_key)
        if provider_instance is None:
            return JSONResponse(
                {
                    "ok": False,
                    "message": f"Failed to instantiate provider '{provider_key.value}'.",
                },
                status_code=HTTP_400_BAD_REQUEST,
            )
        ok, message = await _test_provider(provider_instance)
        latency_ms = (time.perf_counter() - started) * 1000.0
        return JSONResponse(
            {"ok": ok, "message": message, "latency_ms": round(latency_ms, 2)},
        )

    # Ollama path — no key, just ping + list_models.
    try:
        from sovyx.llm.providers.ollama import OllamaProvider
    except ImportError as exc:
        return JSONResponse(
            {
                "ok": False,
                "message": f"Ollama provider unavailable: {exc}",
            },
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )
    transient = OllamaProvider()
    reachable = await transient.ping()
    if not reachable:
        latency_ms = (time.perf_counter() - started) * 1000.0
        return JSONResponse(
            {
                "ok": False,
                "message": "Ollama is not reachable. Start the daemon: 'ollama serve'.",
                "latency_ms": round(latency_ms, 2),
            },
        )
    try:
        models = await transient.list_models()
    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.perf_counter() - started) * 1000.0
        return JSONResponse(
            {
                "ok": False,
                "message": f"Ollama reachable but list_models failed: {exc}",
                "latency_ms": round(latency_ms, 2),
            },
        )
    latency_ms = (time.perf_counter() - started) * 1000.0
    if not models:
        return JSONResponse(
            {
                "ok": False,
                "message": "Ollama running but no models installed. Run 'ollama pull llama3.1'.",
                "latency_ms": round(latency_ms, 2),
                "model_count": 0,
            },
        )
    return JSONResponse(
        {
            "ok": True,
            "message": f"Ollama reachable with {len(models)} model(s).",
            "latency_ms": round(latency_ms, 2),
            "model_count": len(models),
        },
    )
