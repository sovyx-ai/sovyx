"""LLM provider canonical registry — single source of truth.

Mission anchor: ``docs-internal/missions/MISSION-c6-llm-provider-cognitive-
loop-integrity-2026-05-18.md`` §T1.1.

Replaces the duplicated provider lists at:

* ``engine/bootstrap.py:657-700`` — 10 sequential ``os.environ.get``/
  ``providers.append``/``logger.info("llm_provider_registered", ...)`` blocks.
* ``engine/bootstrap.py:776-786`` — hand-written ``metadata.checked_keys``
  literal list.
* ``dashboard/routes/onboarding.py:18-28`` — ``_ENV_VAR_MAP`` dict.

Every downstream consumer imports :class:`LLMProviderKey` and iterates it
instead of hand-writing the provider list. Quality Gate 12 (Mission C6
§T1.3) mechanically enforces wire-discipline across five consumer surfaces;
adding an 11th provider without parallel wiring fails the gate at the
pre-push hook.

Anti-pattern compliance:

* #9 — :class:`StrEnum` for value-based equality and xdist-safe namespace.
* #15 — bounded cardinality (10 fixed members; cannot grow at runtime).
* #34 — registry membership is structural, not a runtime kill-switch.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


class LLMProviderKey(StrEnum):
    """Canonical 10-provider registry.

    Iteration order is the conventional boot-time registration order —
    preserved verbatim from the pre-C6 ``engine/bootstrap.py:657-700``
    sequence so legacy ``llm_provider_registered`` events fire in the
    same order during the ADR-D14 dual-emission window (Mission C6 §4.14).

    Ollama is a member even though it has no API key — uniform shape
    keeps the AST scanner regular. ``OLLAMA.env_var`` returns ``""``
    (sentinel); :meth:`env_var_map` excludes Ollama from the cloud map.
    """

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"
    XAI = "xai"
    DEEPSEEK = "deepseek"
    MISTRAL = "mistral"
    GROQ = "groq"
    TOGETHER = "together"
    FIREWORKS = "fireworks"
    OLLAMA = "ollama"

    @property
    def env_var(self) -> str:
        """Env-var name for cloud providers; empty string for local providers."""
        return _ENV_VAR_BY_KEY[self]

    @property
    def is_cloud(self) -> bool:
        """True for cloud providers (env-var required); False for local Ollama."""
        return self.env_var != ""

    @property
    def default_model(self) -> str:
        """Conservative default model identifier per provider.

        Operator may override via the onboarding flow or ``mind.yaml``.
        Ollama's default is empty — model is resolved dynamically from
        ``OllamaProvider.list_models()`` at boot.
        """
        return _DEFAULT_MODEL_BY_KEY[self]

    @classmethod
    def env_var_map(cls) -> Mapping[str, str]:
        """Backwards-compat replacement for the legacy ``_ENV_VAR_MAP`` dict.

        Returns a mapping ``{provider_value: env_var}`` for cloud providers
        ONLY (Ollama excluded). Currently unused in production —
        ``dashboard/routes/onboarding.py`` keeps a local ``_ENV_VAR_MAP``
        copy; unification is tracked debt.
        """
        return {key.value: key.env_var for key in cls if key.is_cloud}


# Authoritative env-var assignments. The XAI env-var name diverges from
# the provider value (``XGROK_API_KEY`` vs ``xai``) — preserved verbatim
# from the pre-C6 ``bootstrap.py:672`` and ``onboarding.py:22`` strings
# to keep operator playbooks valid.
_ENV_VAR_BY_KEY: Mapping[LLMProviderKey, str] = {
    LLMProviderKey.ANTHROPIC: "ANTHROPIC_API_KEY",
    LLMProviderKey.OPENAI: "OPENAI_API_KEY",
    LLMProviderKey.GOOGLE: "GOOGLE_API_KEY",
    LLMProviderKey.XAI: "XGROK_API_KEY",
    LLMProviderKey.DEEPSEEK: "DEEPSEEK_API_KEY",
    LLMProviderKey.MISTRAL: "MISTRAL_API_KEY",
    LLMProviderKey.GROQ: "GROQ_API_KEY",
    LLMProviderKey.TOGETHER: "TOGETHER_API_KEY",
    LLMProviderKey.FIREWORKS: "FIREWORKS_API_KEY",
    LLMProviderKey.OLLAMA: "",
}


# Conservative default model identifiers. Updated when provider docs flag
# a model as decommissioned. Operator override via the onboarding flow or
# direct ``mind.yaml`` edit always wins.
_DEFAULT_MODEL_BY_KEY: Mapping[LLMProviderKey, str] = {
    LLMProviderKey.ANTHROPIC: "claude-sonnet-4-6",
    LLMProviderKey.OPENAI: "gpt-4o-mini",
    LLMProviderKey.GOOGLE: "gemini-2.0-flash",
    LLMProviderKey.XAI: "grok-2",
    LLMProviderKey.DEEPSEEK: "deepseek-chat",
    LLMProviderKey.MISTRAL: "mistral-large-latest",
    LLMProviderKey.GROQ: "llama-3.3-70b-versatile",
    LLMProviderKey.TOGETHER: "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    LLMProviderKey.FIREWORKS: "accounts/fireworks/models/llama-v3p3-70b-instruct",
    LLMProviderKey.OLLAMA: "",
}


__all__ = ["LLMProviderKey"]
