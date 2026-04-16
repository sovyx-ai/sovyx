"""Sovyx Home Assistant Plugin — control smart-home devices.

Connects to the user's Home Assistant instance via its REST API and
exposes 8 LLM-callable tools across 4 domains:

* ``light``    — list_lights, turn_on_light, turn_off_light
* ``switch``   — turn_on_switch, turn_off_switch
* ``sensor``   — read_sensor, list_sensors
* ``climate``  — set_temperature (requires confirmation)

Permissions
-----------
This plugin needs ``network:local`` because Home Assistant typically
runs on the user's LAN (``http://homeassistant.local:8123``,
``http://192.168.x.x:8123``, …) — addresses the sandbox blocks by
default. The :class:`SandboxedHttpClient` is built with
``allow_local=True`` so the per-request DNS-rebinding + IP checks
let LAN traffic through, while every other guard (allowed-domains
list, rate limit, response size cap, timeout) still applies.

Configuration
-------------
Read from ``~/.sovyx/mind.yaml`` under ``plugins_config.home-assistant``::

    plugins_config:
      home-assistant:
        base_url: "http://homeassistant.local:8123"
        token: "<long-lived access token>"

The user generates the token in HA UI → Profile → Long-Lived Access
Tokens. v0 stores it in mind.yaml (file is 0600 by convention); a
later PR moves it to the still-unwired plugin vault.

What this MVP does *not* do
---------------------------
- No WebSocket subscription — entity state is fetched on demand per
  tool call. Real-time event push is the next PR.
- No mDNS discovery — caller supplies ``base_url`` explicitly.
- No covers / locks / fans / media_player / scenes / scripts — only
  the four high-frequency domains above ship in v0.
- No per-entity ActionSafety rules — ``set_temperature`` is the only
  tool that asks for inline confirmation; everything else is "safe".

Ref: IMPL-008-HOME-ASSISTANT (v0, scope-tightened from spec).
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from sovyx.plugins.official._ha_models import HAEntity, parse_entity, parse_entity_list
from sovyx.plugins.permissions import Permission
from sovyx.plugins.sandbox_http import SandboxedHttpClient
from sovyx.plugins.sdk import ISovyxPlugin, TestResult, tool

# ── Constants ───────────────────────────────────────────────────────

# How long the in-memory entity-list cache stays fresh. The mind
# typically asks for the same domain a few times in quick succession
# inside one ReAct cycle ("show me lights, then turn off the kitchen
# one") — caching for 60 s removes the redundant round-trips without
# letting state get stale.
_ENTITY_CACHE_TTL_S = 60.0

# Per-request HTTP timeout. HA REST is local-network so even a very
# slow Pi answers in well under 5 s; 10 s is the sandbox default and
# leaves comfortable headroom.
_HTTP_TIMEOUT_S = 10.0

# Anything outside [-1, 1] that HA might return as raw state or
# attribute we don't try to coerce. The :func:`_format_*` helpers
# render whatever HA returned verbatim, so we never lie about state.

_DEFAULT_BASE_URL = "http://homeassistant.local:8123"


class HomeAssistantPlugin(ISovyxPlugin):
    """Read state and control devices on the user's Home Assistant.

    Stateless across cycles — we keep only an in-memory cache of the
    last ``/api/states`` snapshot per domain, refreshed on TTL.
    """

    config_schema: ClassVar[dict[str, object]] = {
        "type": "object",
        "properties": {
            "base_url": {
                "type": "string",
                "description": "Home Assistant base URL (e.g. http://homeassistant.local:8123).",
                "default": _DEFAULT_BASE_URL,
            },
            "token": {
                "type": "string",
                "description": "Long-lived access token from HA Profile page.",
            },
        },
        "required": ["token"],
    }

    setup_schema: ClassVar[dict[str, object]] = {
        "fields": [
            {
                "id": "base_url",
                "type": "url",
                "label": "Home Assistant URL",
                "required": True,
                "default": "http://homeassistant.local:8123",
                "placeholder": "http://homeassistant.local:8123",
                "help": "The base URL of your Home Assistant instance.",
            },
            {
                "id": "token",
                "type": "secret",
                "label": "Long-Lived Access Token",
                "required": True,
                "help": "Generate in HA: Profile > Security > Long-Lived Access Tokens.",
                "help_links": {
                    "docs": "https://developers.home-assistant.io/docs/auth_api/#long-lived-access-token",
                },
            },
        ],
        "test_connection": True,
    }

    def __init__(self) -> None:
        # Config is filled in by ``setup(ctx)``. Tools that fire
        # before setup get a friendly "not configured" error instead
        # of crashing — useful when the user enables the plugin but
        # hasn't written the token yet.
        self._base_url: str = _DEFAULT_BASE_URL
        self._token: str = ""
        self._cache: dict[str, tuple[float, list[HAEntity]]] = {}

    # ── Lifecycle ─────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "home-assistant"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return "Read state and control devices on your Home Assistant instance."

    @property
    def permissions(self) -> list[Permission]:
        return [Permission.NETWORK_LOCAL]

    async def setup(self, ctx: object) -> None:
        """Pull base_url + token out of the plugin config dict."""
        cfg = getattr(ctx, "config", None) or {}
        if not isinstance(cfg, dict):
            return
        base_url = cfg.get("base_url")
        if isinstance(base_url, str) and base_url.strip():
            self._base_url = base_url.rstrip("/")
        token = cfg.get("token")
        if isinstance(token, str):
            self._token = token.strip()

    # ── Tools — light ─────────────────────────────────────────

    @tool(description="List all lights known to Home Assistant with on/off state.")
    async def list_lights(self) -> str:
        """Return every ``light.*`` entity, one per line."""
        entities = await self._list_domain("light")
        if not entities:
            return _no_entities("light")
        return _format_entity_list(entities, value_label="state")

    @tool(description="Turn on a Home Assistant light. brightness is optional (0-255).")
    async def turn_on_light(
        self,
        entity_id: str,
        brightness: int | None = None,
    ) -> str:
        """Call ``light/turn_on`` on the named entity.

        Args:
            entity_id: Full HA entity_id (e.g. ``light.kitchen_ceiling``).
            brightness: Optional 0-255. Out-of-range values are clamped.
        """
        service_data: dict[str, Any] = {}
        if brightness is not None:
            service_data["brightness"] = max(0, min(255, int(brightness)))
        return await self._call_service(
            domain="light",
            service="turn_on",
            entity_id=entity_id,
            service_data=service_data,
            success_template="Turned on {entity_id}",
        )

    @tool(description="Turn off a Home Assistant light.")
    async def turn_off_light(self, entity_id: str) -> str:
        return await self._call_service(
            domain="light",
            service="turn_off",
            entity_id=entity_id,
            success_template="Turned off {entity_id}",
        )

    # ── Tools — switch ────────────────────────────────────────

    @tool(description="Turn on a Home Assistant switch (outlet, fan, generic relay).")
    async def turn_on_switch(self, entity_id: str) -> str:
        return await self._call_service(
            domain="switch",
            service="turn_on",
            entity_id=entity_id,
            success_template="Turned on {entity_id}",
        )

    @tool(description="Turn off a Home Assistant switch.")
    async def turn_off_switch(self, entity_id: str) -> str:
        return await self._call_service(
            domain="switch",
            service="turn_off",
            entity_id=entity_id,
            success_template="Turned off {entity_id}",
        )

    # ── Tools — sensor ────────────────────────────────────────

    @tool(
        description=(
            "List Home Assistant sensors. Optional name_contains filters by friendly name."
        )
    )
    async def list_sensors(self, name_contains: str | None = None) -> str:
        """Return every ``sensor.*`` entity, optionally filtered by name."""
        entities = await self._list_domain("sensor")
        if name_contains:
            needle = name_contains.lower()
            entities = [e for e in entities if needle in e.friendly_name.lower()]
        if not entities:
            return _no_entities("sensor")
        return _format_entity_list(entities, value_label="value")

    @tool(description="Read the current value of a single Home Assistant sensor.")
    async def read_sensor(self, entity_id: str) -> str:
        """Fetch ``/api/states/<entity_id>`` and render value + unit."""
        if not self._configured():
            return _not_configured()
        client = self._make_client()
        try:
            resp = await client.get(
                f"{self._base_url}/api/states/{entity_id}",
                headers=self._auth_headers(),
            )
        except Exception as exc:  # noqa: BLE001 — plugin boundary; render & continue.
            return f"Error reading {entity_id}: {exc}"
        finally:
            await client.close()

        if resp.status_code == 404:  # noqa: PLR2004
            return f"Sensor not found: {entity_id}"
        if resp.status_code != 200:  # noqa: PLR2004
            return _http_error(entity_id, resp.status_code)

        entity = parse_entity(resp.json())
        if entity is None:
            return f"Sensor returned malformed payload: {entity_id}"
        unit = str(entity.attributes.get("unit_of_measurement") or "").strip()
        suffix = f" {unit}" if unit else ""
        return f"{entity.friendly_name}: {entity.state}{suffix}"

    # ── Tools — climate (CONFIRM) ─────────────────────────────

    @tool(
        description=(
            "Set the target temperature on a Home Assistant climate entity. "
            "Requires user confirmation."
        ),
        requires_confirmation=True,
    )
    async def set_temperature(self, entity_id: str, temperature: float) -> str:
        """Call ``climate/set_temperature``. Confirmation gated by the framework."""
        try:
            target = float(temperature)
        except (TypeError, ValueError):
            return f"Invalid temperature: {temperature!r}"
        return await self._call_service(
            domain="climate",
            service="set_temperature",
            entity_id=entity_id,
            service_data={"temperature": target},
            success_template=f"Set {{entity_id}} target temperature to {target}°",
        )

    # ── Internals — HTTP + caching ────────────────────────────

    def _configured(self) -> bool:
        return bool(self._token)

    async def test_connection(self, config: dict[str, object]) -> TestResult:
        """Validate Home Assistant credentials via GET /api/states."""
        base_url = str(config.get("base_url", _DEFAULT_BASE_URL)).rstrip("/")
        token = str(config.get("token", ""))
        if not token:
            return TestResult(success=False, message="Long-lived access token is required")

        host = _hostname_from_url(base_url)
        client = SandboxedHttpClient(
            plugin_name=self.name,
            allowed_domains=sorted({host, "homeassistant.local"}),
            allow_local=True,
            timeout_s=_HTTP_TIMEOUT_S,
        )
        try:
            resp = await client.request(
                "GET",
                f"{base_url}/api/",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
        except Exception as exc:  # noqa: BLE001
            return TestResult(success=False, message=f"Connection failed: {exc}")
        finally:
            await client.close()

        if resp.status_code == 200:  # noqa: PLR2004
            return TestResult(success=True, message="Connected to Home Assistant")
        if resp.status_code == 401:  # noqa: PLR2004
            return TestResult(
                success=False,
                message="Invalid token — generate a new long-lived access token in HA settings",
            )
        return TestResult(success=False, message=f"Server returned HTTP {resp.status_code}")

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _make_client(self) -> SandboxedHttpClient:
        """Build a sandboxed HTTP client with LAN access enabled.

        The ``allowed_domains`` list intentionally trusts only the
        host portion of the configured ``base_url`` plus the common
        ``homeassistant.local`` mDNS name — keeping the surface
        narrow even though ``allow_local=True`` lets us hit private
        ranges. ``allow_any_domain`` is *never* used here.
        """
        host = _hostname_from_url(self._base_url)
        allowed = sorted({host, "homeassistant.local"})
        return SandboxedHttpClient(
            plugin_name=self.name,
            allowed_domains=allowed,
            allow_local=True,
            timeout_s=_HTTP_TIMEOUT_S,
        )

    async def _list_domain(self, domain: str) -> list[HAEntity]:
        """Return entities of ``domain`` from cache or by fetching ``/api/states``."""
        if not self._configured():
            return []
        cached = self._cache.get(domain)
        now = time.monotonic()
        if cached is not None and (now - cached[0]) < _ENTITY_CACHE_TTL_S:
            return cached[1]

        client = self._make_client()
        try:
            resp = await client.get(
                f"{self._base_url}/api/states",
                headers=self._auth_headers(),
            )
        except Exception:  # noqa: BLE001 — same boundary rule.
            return cached[1] if cached is not None else []
        finally:
            await client.close()

        if resp.status_code != 200:  # noqa: PLR2004
            return cached[1] if cached is not None else []

        payload = resp.json()
        if not isinstance(payload, list):
            return cached[1] if cached is not None else []
        entities = parse_entity_list(payload, domain_filter=domain)
        self._cache[domain] = (now, entities)
        return entities

    async def _call_service(
        self,
        *,
        domain: str,
        service: str,
        entity_id: str,
        service_data: dict[str, Any] | None = None,
        success_template: str = "Called {domain}.{service} on {entity_id}",
    ) -> str:
        """POST ``/api/services/<domain>/<service>`` and render the outcome.

        On success returns the formatted ``success_template``; on
        every failure path (not configured, HTTP error, invalid
        entity) returns a short message the LLM can relay verbatim.
        """
        if not self._configured():
            return _not_configured()
        if not _validate_entity_id(entity_id, domain):
            return f"Invalid entity_id for {domain}: {entity_id}"

        body: dict[str, Any] = {"entity_id": entity_id}
        if service_data:
            body.update(service_data)

        client = self._make_client()
        try:
            resp = await client.post(
                f"{self._base_url}/api/services/{domain}/{service}",
                headers=self._auth_headers(),
                json=body,
            )
        except Exception as exc:  # noqa: BLE001
            return f"Error calling {domain}.{service}: {exc}"
        finally:
            await client.close()

        if resp.status_code == 401:  # noqa: PLR2004
            return "Authentication failed — check the home-assistant token in config."
        if resp.status_code == 404:  # noqa: PLR2004
            return f"Entity not found: {entity_id}"
        if resp.status_code >= 400:  # noqa: PLR2004
            return _http_error(entity_id, resp.status_code)

        # HA invalidates the snapshot for this domain so the next
        # list_* call sees the change instead of stale cached state.
        self._cache.pop(domain, None)
        return success_template.format(domain=domain, service=service, entity_id=entity_id)


# ── Module-level helpers ────────────────────────────────────────────


def _hostname_from_url(url: str) -> str:
    """Extract just the hostname for the SandboxedHttpClient allowlist.

    Falls back to ``homeassistant.local`` for unparseable input — the
    plugin still refuses to run if the token is missing, so the
    fallback never reaches a real network call accidentally.
    """
    from urllib.parse import urlparse  # noqa: PLC0415

    try:
        host = urlparse(url).hostname
    except (ValueError, TypeError):
        host = None
    return host or "homeassistant.local"


def _validate_entity_id(entity_id: str, expected_domain: str) -> bool:
    """A well-formed entity_id is ``<domain>.<object_id>`` matching ``expected_domain``."""
    if not isinstance(entity_id, str) or "." not in entity_id:
        return False
    domain, _, object_id = entity_id.partition(".")
    return domain == expected_domain and bool(object_id)


def _format_entity_list(entities: list[HAEntity], *, value_label: str) -> str:
    lines = [f"Found {len(entities)} {entities[0].domain}(s):"]
    for entity in entities:
        lines.append(
            f"  • {entity.entity_id} — {entity.friendly_name} [{value_label}: {entity.state}]"
        )
    return "\n".join(lines)


def _no_entities(domain: str) -> str:
    return f"No {domain} entities found in Home Assistant."


def _not_configured() -> str:
    return (
        "Home Assistant plugin is not configured. Set ``token`` (and optionally "
        "``base_url``) under ``plugins_config.home-assistant`` in mind.yaml."
    )


def _http_error(entity_id: str, status: int) -> str:
    return f"Home Assistant returned HTTP {status} for {entity_id}."
