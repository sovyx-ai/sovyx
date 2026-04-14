"""LLM provider status + runtime provider switch endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


@router.get("/providers")
async def get_providers(request: Request) -> JSONResponse:
    """LLM provider status, availability, and available models.

    Returns all registered providers with their configuration state, plus
    the currently active provider/model from MindConfig. Cloud providers
    report ``configured`` based on API key presence. Ollama reports
    ``reachable`` via a live ping and lists installed models.
    """
    from sovyx.llm.providers.ollama import OllamaProvider

    registry = getattr(request.app.state, "registry", None)
    mind_config = getattr(request.app.state, "mind_config", None)

    if registry is None:
        return JSONResponse(
            {"error": "Engine not running"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    providers_out: list[dict[str, object]] = []

    try:
        from sovyx.llm.router import LLMRouter

        if registry.is_registered(LLMRouter):
            router_svc = await registry.resolve(LLMRouter)

            for p in router_svc._providers:
                entry: dict[str, object] = {
                    "name": p.name,
                    "configured": p.is_available,
                    "available": p.is_available,
                }
                if isinstance(p, OllamaProvider):
                    reachable = await p.ping()
                    models = await p.list_models() if reachable else []
                    entry.update(
                        {
                            "configured": True,  # always registered
                            "available": reachable,
                            "reachable": reachable,
                            "models": models,
                            "base_url": p.base_url,
                        }
                    )
                providers_out.append(entry)
    except Exception:  # noqa: BLE001
        logger.debug("providers_list_failed")

    active: dict[str, str] = {"provider": "", "model": "", "fast_model": ""}
    if mind_config is not None:
        active = {
            "provider": mind_config.llm.default_provider,
            "model": mind_config.llm.default_model,
            "fast_model": mind_config.llm.fast_model,
        }

    return JSONResponse({"providers": providers_out, "active": active})


@router.put("/providers")
async def update_provider(request: Request) -> JSONResponse:
    """Change active LLM provider and model at runtime.

    Updates ``MindConfig.llm`` in-place (no restart needed — ThinkPhase
    reads config per-call) and persists to ``mind.yaml``.

    Request body::

        {"provider": "ollama", "model": "llama3.1:latest"}
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(
            {"ok": False, "error": "Invalid JSON body"},
            status_code=422,
        )

    if not isinstance(body, dict):
        return JSONResponse(
            {"ok": False, "error": "Expected JSON object"},
            status_code=422,
        )

    new_provider = body.get("provider", "")
    new_model = body.get("model", "")
    if not new_provider or not new_model:
        return JSONResponse(
            {"ok": False, "error": "Both 'provider' and 'model' are required"},
            status_code=422,
        )

    mind_config = getattr(request.app.state, "mind_config", None)
    if mind_config is None:
        return JSONResponse(
            {"ok": False, "error": "No mind configuration loaded"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"ok": False, "error": "Engine not running"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Validate provider exists and is available.
    try:
        from sovyx.llm.providers.ollama import OllamaProvider
        from sovyx.llm.router import LLMRouter

        if not registry.is_registered(LLMRouter):
            return JSONResponse(
                {"ok": False, "error": "LLM router not available"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

        router_svc = await registry.resolve(LLMRouter)
        target = next((p for p in router_svc._providers if p.name == new_provider), None)

        if target is None:
            return JSONResponse(
                {"ok": False, "error": f"Unknown provider: {new_provider}"},
                status_code=422,
            )

        if isinstance(target, OllamaProvider):
            reachable = await target.ping()
            if not reachable:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": f"Ollama not reachable at {target.base_url}",
                    },
                    status_code=422,
                )
        elif not target.is_available:
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"Provider '{new_provider}' is not configured (missing API key?)",
                },
                status_code=422,
            )
    except Exception:
        logger.warning("provider_switch_validation_failed", exc_info=True)
        return JSONResponse(
            {"ok": False, "error": "Provider validation failed"},
            status_code=500,
        )

    # Apply runtime update (immediate — no restart needed).
    old_provider = mind_config.llm.default_provider
    old_model = mind_config.llm.default_model
    mind_config.llm.default_provider = new_provider
    mind_config.llm.default_model = new_model

    changes = {
        "provider": f"{old_provider} → {new_provider}",
        "model": f"{old_model} → {new_model}",
    }

    # Persist to mind.yaml.
    mind_yaml_path = getattr(request.app.state, "mind_yaml_path", None)
    if mind_yaml_path is not None:
        from sovyx.dashboard.config import _persist_to_yaml

        _persist_to_yaml(mind_config, mind_yaml_path)
        logger.info(
            "provider_switch_persisted",
            provider=new_provider,
            model=new_model,
        )

    ws_manager = getattr(request.app.state, "ws_manager", None)
    if ws_manager is not None:
        await ws_manager.broadcast({"type": "config_updated", "changes": changes})

    logger.info(
        "provider_switched",
        old_provider=old_provider,
        new_provider=new_provider,
        old_model=old_model,
        new_model=new_model,
    )

    return JSONResponse({"ok": True, "changes": changes})
