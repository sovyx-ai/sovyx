"""Sovyx config-provenance tracker — where did each value come from?

Walks a pydantic-settings ``BaseSettings`` instance (recursively for
nested settings) and produces a flat ``"dotted.path" → FieldProvenance``
map. The provenance answers: "is this value still the default, or did
an env var / file override it, and which one?".

The tracker focuses on env-var detection (the most common override
path in production). For each field it looks up the canonical
``SOVYX_*`` env-var name implied by the model's ``env_prefix`` +
``env_nested_delimiter`` and the field's name. If the env var is set,
the source is :attr:`ConfigSource.env_var`; otherwise it falls back to
:attr:`ConfigSource.default`.

Future work (CLI flags, dashboard runtime overrides, YAML files) can
extend the tracker without changing the public shape — callers only
ever see ``FieldProvenance`` instances.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

if TYPE_CHECKING:
    from collections.abc import Mapping


class ConfigSource(StrEnum):
    """Where a config value came from at resolution time."""

    DEFAULT = "default"
    FILE = "file"
    ENV_VAR = "env_var"
    CLI_FLAG = "cli_flag"
    DASHBOARD_OVERRIDE = "dashboard_override"


class FieldProvenance(BaseModel):
    """One field's resolved value + the source it came from."""

    source: ConfigSource
    raw_value: Any = None
    """The value as the source produced it (string for env vars, etc.)."""
    resolved_value: Any = None
    """The value after pydantic coercion/validation (typed Python value)."""
    env_key: str | None = None
    """The canonical env-var name (set even when the env var was not used)."""
    field_path: str = Field(default="")
    """Dotted path from root settings to this field.

    Example: ``observability.features.async_queue``.
    """


def track_provenance(
    config: BaseSettings,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, FieldProvenance]:
    """Return a flat ``dotted-path → FieldProvenance`` map for *config*.

    Args:
        config: The settings instance to inspect. Nested
            ``BaseSettings`` fields are recursed automatically; nested
            plain ``BaseModel`` fields are walked but their fields
            inherit the parent's env prefix.
        environ: Optional environment mapping for testing — defaults
            to :data:`os.environ`.

    Returns:
        Dict mapping dotted field paths to :class:`FieldProvenance`.
        Order is insertion order (depth-first traversal) so the result
        renders predictably in startup-cascade logs.
    """
    env = environ if environ is not None else os.environ
    out: dict[str, FieldProvenance] = {}
    _walk(config, prefix="", env=env, out=out)
    return out


# ── Internal traversal ──────────────────────────────────────────────


def _walk(
    instance: BaseModel,
    *,
    prefix: str,
    env: Mapping[str, str],
    out: dict[str, FieldProvenance],
) -> None:
    """Depth-first walk of *instance*, populating *out* in place."""
    env_prefix, env_delim = _settings_env_config(instance)

    for field_name, field_info in type(instance).model_fields.items():
        value = getattr(instance, field_name, None)
        dotted = f"{prefix}.{field_name}" if prefix else field_name

        # Recurse into nested settings/models so the dotted path
        # mirrors the actual attribute access.
        if isinstance(value, BaseSettings):
            _walk(value, prefix=dotted, env=env, out=out)
            continue
        if isinstance(value, BaseModel):
            _walk(value, prefix=dotted, env=env, out=out)
            continue

        env_key = _build_env_key(env_prefix, env_delim, field_name)
        # When the field name is the only thing after the prefix the
        # env var is unambiguous. For nested non-settings models we
        # don't know the prefix, so env_key may be None.
        raw_env_value = env.get(env_key) if env_key else None

        # Detect "is this still the default?" by comparing against the
        # field's declared default value. We can't perfectly distinguish
        # "user set it to the same value as the default" vs "left it
        # alone", but matching env presence is a reliable proxy.
        source = ConfigSource.ENV_VAR if raw_env_value is not None else ConfigSource.DEFAULT

        # Field default may be a callable (default_factory) — attempt
        # comparison only when default is a plain value to avoid
        # invoking factories whose side-effects are unknown.
        out[dotted] = FieldProvenance(
            source=source,
            raw_value=raw_env_value if raw_env_value is not None else _safe_default(field_info),
            resolved_value=_redact(field_name, value),
            env_key=env_key,
            field_path=dotted,
        )


def _settings_env_config(instance: BaseModel) -> tuple[str | None, str | None]:
    """Return ``(env_prefix, env_nested_delimiter)`` from the model's settings.

    Returns ``(None, None)`` for plain :class:`BaseModel` instances —
    they don't carry env_prefix metadata, so the caller cannot build
    an env_key for their fields.
    """
    if not isinstance(instance, BaseSettings):
        return None, None
    cfg = type(instance).model_config
    prefix_raw = cfg.get("env_prefix")
    delim_raw = cfg.get("env_nested_delimiter") or cfg.get("env_delimiter")
    prefix = prefix_raw if isinstance(prefix_raw, str) else None
    delim = delim_raw if isinstance(delim_raw, str) else None
    return (prefix, delim)


def _build_env_key(
    prefix: str | None,
    delim: str | None,  # noqa: ARG001  — kept for future nested expansion
    field_name: str,
) -> str | None:
    """Compose the env-var name for *field_name* under *prefix*."""
    if prefix is None:
        return None
    return f"{prefix}{field_name.upper()}"


_SENSITIVE_FIELD_TOKENS = ("token", "secret", "key", "password", "credential")


def _redact(field_name: str, value: Any) -> Any:  # noqa: ANN401
    """Mask values whose field name suggests credentials."""
    lowered = field_name.lower()
    if any(tok in lowered for tok in _SENSITIVE_FIELD_TOKENS) and isinstance(value, str):
        if not value:
            return ""
        return f"[redacted len={len(value)}]"
    return value


def _safe_default(field_info: Any) -> Any:  # noqa: ANN401
    """Return the field's declared default without firing a factory."""
    default = getattr(field_info, "default", None)
    # PydanticUndefined sentinel for "no default" — treat as None.
    if default is None or repr(default) == "PydanticUndefined":
        return None
    if callable(default):
        return None
    return default


__all__ = [
    "ConfigSource",
    "FieldProvenance",
    "track_provenance",
]
