"""Onboarding endpoints — first-run wizard for LLM provider + personality."""

from __future__ import annotations

import contextlib
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/onboarding", dependencies=[Depends(verify_token)])

_ENV_VAR_MAP: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "xai": "XGROK_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
}


@router.get("/state")
async def get_onboarding_state(request: Request) -> JSONResponse:
    """Return onboarding progress for the active mind."""
    mind_config = getattr(request.app.state, "mind_config", None)
    provider_configured = False
    ollama_available = False
    ollama_models: list[str] = []

    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        from sovyx.llm.router import LLMRouter

        if registry.is_registered(LLMRouter):
            router_svc = await registry.resolve(LLMRouter)
            provider_configured = any(
                p.is_available for p in router_svc._providers if p.name != "ollama"
            )
            from sovyx.llm.providers.ollama import OllamaProvider

            for p in router_svc._providers:
                if isinstance(p, OllamaProvider):
                    ollama_available = await p.ping()
                    if ollama_available:
                        ollama_models = await p.list_models()
                    break

    return JSONResponse(
        {
            "complete": mind_config.onboarding_complete if mind_config else False,
            "provider_configured": provider_configured or ollama_available,
            "default_provider": mind_config.llm.default_provider if mind_config else "",
            "default_model": mind_config.llm.default_model if mind_config else "",
            "ollama_available": ollama_available,
            "ollama_models": ollama_models,
        }
    )


@router.post("/provider")
async def configure_provider(request: Request) -> JSONResponse:
    """Validate API key, persist, and hot-register in LLM router."""
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=422)

    provider_name = body.get("provider", "")
    api_key = body.get("api_key", "")
    model = body.get("model", "")

    if not provider_name:
        return JSONResponse({"ok": False, "error": "'provider' is required"}, status_code=422)

    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse({"ok": False, "error": "Engine not running"}, status_code=503)

    from sovyx.llm.router import LLMRouter

    if not registry.is_registered(LLMRouter):
        return JSONResponse({"ok": False, "error": "LLM router not available"}, status_code=503)

    router_svc = await registry.resolve(LLMRouter)

    # ── Handle Ollama (no API key) ──
    if provider_name == "ollama":
        from sovyx.llm.providers.ollama import OllamaProvider

        ollama = next((p for p in router_svc._providers if isinstance(p, OllamaProvider)), None)
        if ollama is None:
            return JSONResponse(
                {"ok": False, "error": "Ollama provider not registered"}, status_code=422
            )
        reachable = await ollama.ping()
        if not reachable:
            return JSONResponse(
                {"ok": False, "error": "Ollama not reachable. Is it running?"}, status_code=422
            )
        if not model:
            models = await ollama.list_models()
            model = models[0] if models else "llama3.1:latest"

        return await _apply_provider(request, router_svc, "ollama", model)

    # ── Handle cloud providers (API key required) ──
    env_var = _ENV_VAR_MAP.get(provider_name)
    if env_var is None:
        return JSONResponse(
            {"ok": False, "error": f"Unknown provider: {provider_name}"}, status_code=422
        )
    if not api_key:
        return JSONResponse(
            {"ok": False, "error": "'api_key' is required for cloud providers"}, status_code=422
        )

    # Create and validate the provider
    provider_instance = _create_provider(provider_name, api_key)
    if provider_instance is None:
        return JSONResponse(
            {"ok": False, "error": f"Failed to create provider: {provider_name}"}, status_code=500
        )

    # Test the key with a minimal call
    test_ok, test_msg = await _test_provider(provider_instance)
    if not test_ok:
        return JSONResponse(
            {"ok": False, "error": f"API key validation failed: {test_msg}"}, status_code=422
        )

    # Persist to secrets.env
    _persist_api_key(request, env_var, api_key)

    # Set in current process env so subsequent bootstrap reads work
    os.environ[env_var] = api_key

    # Hot-register in router
    router_svc.add_provider(provider_instance)

    # Resolve default model if not specified
    if not model:
        model = _default_model_for(provider_name)

    return await _apply_provider(request, router_svc, provider_name, model)


@router.post("/personality")
async def configure_personality(request: Request) -> JSONResponse:
    """Save personality preset or custom values to mind.yaml.

    Body::

        {"preset": "warm"}
        // or
        {"personality": {"tone": "direct", ...}, "language": "pt"}
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=422)

    mind_config = getattr(request.app.state, "mind_config", None)
    if mind_config is None:
        return JSONResponse({"ok": False, "error": "No mind loaded"}, status_code=503)

    presets: dict[str, dict[str, object]] = {
        "warm": {
            "tone": "warm",
            "humor": 0.4,
            "formality": 0.3,
            "empathy": 0.8,
            "assertiveness": 0.5,
            "curiosity": 0.7,
        },
        "direct": {
            "tone": "direct",
            "humor": 0.1,
            "formality": 0.6,
            "empathy": 0.5,
            "assertiveness": 0.8,
            "curiosity": 0.5,
        },
        "playful": {
            "tone": "playful",
            "humor": 0.8,
            "formality": 0.2,
            "empathy": 0.6,
            "assertiveness": 0.4,
            "curiosity": 0.9,
        },
        "professional": {
            "tone": "neutral",
            "humor": 0.1,
            "formality": 0.9,
            "empathy": 0.4,
            "assertiveness": 0.7,
            "curiosity": 0.5,
        },
    }

    preset = body.get("preset")
    if preset and preset in presets:
        for k, v in presets[preset].items():
            setattr(mind_config.personality, k, v)

    custom = body.get("personality")
    if isinstance(custom, dict):
        for k, v in custom.items():
            if hasattr(mind_config.personality, k):
                setattr(mind_config.personality, k, v)

    lang = body.get("language")
    if lang and isinstance(lang, str):
        mind_config.language = lang

    # Persist
    mind_yaml_path = getattr(request.app.state, "mind_yaml_path", None)
    if mind_yaml_path is not None:
        from sovyx.dashboard.config import _persist_to_yaml

        _persist_to_yaml(mind_config, mind_yaml_path)

    return JSONResponse({"ok": True})


@router.post("/complete")
async def complete_onboarding(request: Request) -> JSONResponse:
    """Mark onboarding as complete."""
    mind_config = getattr(request.app.state, "mind_config", None)
    if mind_config is None:
        return JSONResponse({"ok": False, "error": "No mind loaded"}, status_code=503)

    mind_config.onboarding_complete = True

    mind_yaml_path = getattr(request.app.state, "mind_yaml_path", None)
    if mind_yaml_path is not None:
        from sovyx.dashboard.config import _persist_to_yaml

        _persist_to_yaml(mind_config, mind_yaml_path)

    logger.info("onboarding_completed")
    return JSONResponse({"ok": True})


# ── Helpers ──────────────────────────────────────────────────────────


def _create_provider(name: str, api_key: str) -> object | None:
    """Instantiate a provider by name with the given API key."""
    try:
        if name == "anthropic":
            from sovyx.llm.providers.anthropic import AnthropicProvider

            return AnthropicProvider(api_key=api_key)
        if name == "openai":
            from sovyx.llm.providers.openai import OpenAIProvider

            return OpenAIProvider(api_key=api_key)
        if name == "google":
            from sovyx.llm.providers.google import GoogleProvider

            return GoogleProvider(api_key=api_key)
        if name == "xai":
            from sovyx.llm.providers.xai import XAIProvider

            return XAIProvider(api_key=api_key)
        if name == "deepseek":
            from sovyx.llm.providers.deepseek import DeepSeekProvider

            return DeepSeekProvider(api_key=api_key)
        if name == "mistral":
            from sovyx.llm.providers.mistral import MistralProvider

            return MistralProvider(api_key=api_key)
        if name == "groq":
            from sovyx.llm.providers.groq import GroqProvider

            return GroqProvider(api_key=api_key)
        if name == "together":
            from sovyx.llm.providers.together import TogetherProvider

            return TogetherProvider(api_key=api_key)
        if name == "fireworks":
            from sovyx.llm.providers.fireworks import FireworksProvider

            return FireworksProvider(api_key=api_key)
    except Exception:  # noqa: BLE001
        logger.warning("provider_creation_failed", provider=name, exc_info=True)
    return None


async def _test_provider(provider: object) -> tuple[bool, str]:
    """Validate a provider by attempting a minimal generation."""
    try:
        from sovyx.engine.protocols import LLMProvider

        if not isinstance(provider, LLMProvider):
            return False, "Not a valid provider"
        default_model = _default_model_for(getattr(provider, "name", ""))
        resp = await provider.generate(
            messages=[{"role": "user", "content": "Hi"}],
            model=default_model,
            temperature=0.0,
            max_tokens=5,
        )
        if resp and hasattr(resp, "content") and resp.content:
            return True, "OK"
        return True, "Connected (empty response)"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _default_model_for(provider_name: str) -> str:
    """Return sensible default model for a provider."""
    defaults: dict[str, str] = {
        "anthropic": "claude-sonnet-4-20250514",
        "openai": "gpt-4o",
        "google": "gemini-2.5-pro-preview-03-25",
        "xai": "grok-2",
        "deepseek": "deepseek-chat",
        "mistral": "mistral-large-latest",
        "groq": "llama-3.1-70b-versatile",
        "together": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        "fireworks": "accounts/fireworks/models/llama-v3p1-70b-instruct",
        "ollama": "llama3.1:latest",
    }
    return defaults.get(provider_name, "")


def _persist_api_key(request: Request, env_var: str, api_key: str) -> None:
    """Append API key to secrets.env in the data directory."""
    from pathlib import Path

    data_dir = getattr(request.app.state, "data_dir", None)
    if data_dir is None:
        engine_config = getattr(request.app.state, "engine_config", None)
        if engine_config is not None:
            data_dir = engine_config.data_dir
    if data_dir is None:
        data_dir = Path.home() / ".sovyx"

    secrets_path = Path(data_dir) / "secrets.env"
    secrets_path.parent.mkdir(parents=True, exist_ok=True)

    existing_lines: list[str] = []
    if secrets_path.exists():
        existing_lines = secrets_path.read_text(encoding="utf-8").splitlines()

    # Replace if key already exists, otherwise append
    found = False
    new_lines: list[str] = []
    for line in existing_lines:
        if line.strip().startswith(f"{env_var}="):
            new_lines.append(f"{env_var}={api_key}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{env_var}={api_key}")

    secrets_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        secrets_path.chmod(0o600)

    logger.info("api_key_persisted", env_var=env_var, path=str(secrets_path))


async def _apply_provider(
    request: Request,
    router_svc: object,
    provider_name: str,
    model: str,
) -> JSONResponse:
    """Update mind config with the selected provider/model and persist."""
    mind_config = getattr(request.app.state, "mind_config", None)
    if mind_config is not None:
        mind_config.llm.default_provider = provider_name
        mind_config.llm.default_model = model
        if not mind_config.llm.fast_model:
            mind_config.llm.fast_model = model

        mind_yaml_path = getattr(request.app.state, "mind_yaml_path", None)
        if mind_yaml_path is not None:
            from sovyx.dashboard.config import _persist_to_yaml

            _persist_to_yaml(mind_config, mind_yaml_path)

    logger.info("onboarding_provider_configured", provider=provider_name, model=model)
    return JSONResponse(
        {
            "ok": True,
            "provider": provider_name,
            "model": model,
        }
    )
