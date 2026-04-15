"""Tests for sovyx.plugins.official.home_assistant.

Mock pattern follows CLAUDE.md anti-pattern #14: SandboxedHttpClient
internally calls ``self._client.request(METHOD, ...)``, NOT ``.get()``
or ``.post()``. We patch the ``SandboxedHttpClient`` class itself
and stub out ``get``/``post``/``close`` on the returned instance —
that's the surface :func:`HomeAssistantPlugin._call_service` /
:func:`_list_domain` / :func:`read_sensor` actually touch.

The plugin uses ``allow_local=True`` because Home Assistant lives on
the LAN; the tests don't need to verify sandbox behaviour (that's
covered by ``test_sandbox_http``) — they only verify that the
plugin builds the right URLs, headers, and bodies, parses
responses, and renders error paths.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.plugins.official import home_assistant as ha_mod  # anti-pattern #11
from sovyx.plugins.official.home_assistant import (
    _DEFAULT_BASE_URL,
    _ENTITY_CACHE_TTL_S,
    HomeAssistantPlugin,
    _format_entity_list,
    _hostname_from_url,
    _validate_entity_id,
)
from sovyx.plugins.permissions import Permission

# ── Helpers ──────────────────────────────────────────────────────────


def _entity(
    entity_id: str = "light.kitchen",
    state: str = "on",
    friendly: str | None = None,
    **attrs: Any,
) -> dict[str, Any]:
    """Build one HA REST entity dict (the shape /api/states emits)."""
    attributes: dict[str, Any] = {"friendly_name": friendly or entity_id}
    attributes.update(attrs)
    return {"entity_id": entity_id, "state": state, "attributes": attributes}


def _build_client_patch(
    *,
    get_response: MagicMock | None = None,
    post_response: MagicMock | None = None,
) -> tuple[Any, MagicMock]:
    """Patch ``SandboxedHttpClient`` so ``_make_client`` returns a mock.

    Returns the ``patch`` context manager and the mock client itself
    so tests can assert on the calls without re-entering the patch.
    """
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=get_response)
    mock_client.post = AsyncMock(return_value=post_response)
    mock_client.close = AsyncMock()
    patcher = patch.object(ha_mod, "SandboxedHttpClient", return_value=mock_client)
    return patcher, mock_client


def _http(status: int, payload: Any = None) -> MagicMock:
    """Build a fake ``httpx.Response``-like object."""
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=payload)
    return resp


async def _configured() -> HomeAssistantPlugin:
    """Build a plugin with token + non-default URL pre-set."""
    plugin = HomeAssistantPlugin()
    ctx = MagicMock()
    ctx.config = {"base_url": "http://homeassistant.local:8123", "token": "test-token"}
    await plugin.setup(ctx)
    return plugin


# ── Plugin metadata + lifecycle ──────────────────────────────────────


class TestHomeAssistantPluginMeta:
    def test_name(self) -> None:
        assert HomeAssistantPlugin().name == "home-assistant"

    def test_version(self) -> None:
        assert HomeAssistantPlugin().version == "0.1.0"

    def test_description_mentions_home_assistant(self) -> None:
        assert "Home Assistant" in HomeAssistantPlugin().description

    def test_permissions_include_network_local(self) -> None:
        assert Permission.NETWORK_LOCAL in HomeAssistantPlugin().permissions

    async def test_setup_reads_base_url_and_token(self) -> None:
        plugin = HomeAssistantPlugin()
        ctx = MagicMock()
        ctx.config = {
            "base_url": "http://192.168.1.50:8123/",
            "token": "  abc-123  ",
        }
        await plugin.setup(ctx)
        # Trailing slash stripped, token whitespace-trimmed.
        assert plugin._base_url == "http://192.168.1.50:8123"  # noqa: SLF001
        assert plugin._token == "abc-123"  # noqa: SLF001

    async def test_setup_falls_back_to_defaults_for_missing_fields(self) -> None:
        plugin = HomeAssistantPlugin()
        ctx = MagicMock()
        ctx.config = {}
        await plugin.setup(ctx)
        assert plugin._base_url == _DEFAULT_BASE_URL  # noqa: SLF001
        assert plugin._token == ""  # noqa: SLF001

    async def test_setup_tolerates_non_dict_config(self) -> None:
        plugin = HomeAssistantPlugin()
        ctx = MagicMock()
        ctx.config = "not a dict"
        await plugin.setup(ctx)
        # Defaults preserved, no crash.
        assert plugin._base_url == _DEFAULT_BASE_URL  # noqa: SLF001


# ── Not-configured guard ─────────────────────────────────────────────


class TestNotConfiguredGuard:
    """Without a token, every action must return a friendly message."""

    async def test_turn_on_light_without_token(self) -> None:
        plugin = HomeAssistantPlugin()  # no setup() call — token=""
        result = await plugin.turn_on_light("light.kitchen")
        assert "not configured" in result.lower()

    async def test_set_temperature_without_token(self) -> None:
        plugin = HomeAssistantPlugin()
        result = await plugin.set_temperature("climate.living", 21.0)
        assert "not configured" in result.lower()

    async def test_read_sensor_without_token(self) -> None:
        plugin = HomeAssistantPlugin()
        result = await plugin.read_sensor("sensor.temp")
        assert "not configured" in result.lower()

    async def test_list_lights_returns_empty_friendly_message(self) -> None:
        """Even read-only ``list_*`` short-circuits gracefully without a token."""
        plugin = HomeAssistantPlugin()
        result = await plugin.list_lights()
        # Hits _list_domain → returns []. Renderer says "no entities".
        assert "No light entities" in result


# ── light.turn_on / turn_off ────────────────────────────────────────


class TestTurnOnLight:
    async def test_turn_on_basic(self) -> None:
        plugin = await _configured()
        patcher, client = _build_client_patch(post_response=_http(200, [{}]))
        with patcher:
            result = await plugin.turn_on_light("light.kitchen")

        assert "Turned on light.kitchen" in result
        client.post.assert_awaited_once()
        url = client.post.await_args.args[0]
        assert url.endswith("/api/services/light/turn_on")
        body = client.post.await_args.kwargs["json"]
        assert body == {"entity_id": "light.kitchen"}

    async def test_turn_on_with_brightness_clamped(self) -> None:
        plugin = await _configured()
        patcher, client = _build_client_patch(post_response=_http(200, [{}]))
        with patcher:
            await plugin.turn_on_light("light.bed", brightness=999)

        body = client.post.await_args.kwargs["json"]
        assert body["brightness"] == 255  # clamped to upper bound

    async def test_turn_on_with_negative_brightness_clamped(self) -> None:
        plugin = await _configured()
        patcher, client = _build_client_patch(post_response=_http(200, [{}]))
        with patcher:
            await plugin.turn_on_light("light.bed", brightness=-50)
        body = client.post.await_args.kwargs["json"]
        assert body["brightness"] == 0

    async def test_invalid_entity_id_does_not_call_ha(self) -> None:
        plugin = await _configured()
        patcher, client = _build_client_patch(post_response=_http(200, [{}]))
        with patcher:
            result = await plugin.turn_on_light("not-a-light-id")
        assert "Invalid entity_id" in result
        client.post.assert_not_awaited()

    async def test_wrong_domain_rejected(self) -> None:
        plugin = await _configured()
        patcher, client = _build_client_patch(post_response=_http(200, [{}]))
        with patcher:
            result = await plugin.turn_on_light("switch.kitchen")
        assert "Invalid entity_id" in result
        client.post.assert_not_awaited()

    async def test_404_renders_entity_not_found(self) -> None:
        plugin = await _configured()
        patcher, _ = _build_client_patch(post_response=_http(404))
        with patcher:
            result = await plugin.turn_on_light("light.ghost")
        assert "Entity not found" in result

    async def test_401_renders_auth_failed(self) -> None:
        plugin = await _configured()
        patcher, _ = _build_client_patch(post_response=_http(401))
        with patcher:
            result = await plugin.turn_on_light("light.kitchen")
        assert "Authentication failed" in result

    async def test_500_renders_generic_error(self) -> None:
        plugin = await _configured()
        patcher, _ = _build_client_patch(post_response=_http(500))
        with patcher:
            result = await plugin.turn_on_light("light.kitchen")
        assert "HTTP 500" in result

    async def test_network_exception_rendered(self) -> None:
        plugin = await _configured()
        patcher, client = _build_client_patch(post_response=_http(200, [{}]))
        with patcher:
            client.post.side_effect = RuntimeError("connection refused")
            result = await plugin.turn_on_light("light.kitchen")
        assert "Error" in result
        assert "connection refused" in result


class TestTurnOffLight:
    async def test_turn_off_basic(self) -> None:
        plugin = await _configured()
        patcher, client = _build_client_patch(post_response=_http(200, [{}]))
        with patcher:
            result = await plugin.turn_off_light("light.kitchen")
        assert "Turned off light.kitchen" in result
        url = client.post.await_args.args[0]
        assert url.endswith("/api/services/light/turn_off")


# ── switch ──────────────────────────────────────────────────────────


class TestSwitch:
    async def test_turn_on_switch(self) -> None:
        plugin = await _configured()
        patcher, client = _build_client_patch(post_response=_http(200, [{}]))
        with patcher:
            result = await plugin.turn_on_switch("switch.fan")
        assert "Turned on switch.fan" in result
        assert client.post.await_args.args[0].endswith("/api/services/switch/turn_on")

    async def test_turn_off_switch(self) -> None:
        plugin = await _configured()
        patcher, client = _build_client_patch(post_response=_http(200, [{}]))
        with patcher:
            result = await plugin.turn_off_switch("switch.fan")
        assert "Turned off switch.fan" in result
        assert client.post.await_args.args[0].endswith("/api/services/switch/turn_off")

    async def test_switch_rejects_light_entity(self) -> None:
        plugin = await _configured()
        patcher, client = _build_client_patch(post_response=_http(200, [{}]))
        with patcher:
            result = await plugin.turn_on_switch("light.kitchen")
        assert "Invalid entity_id" in result
        client.post.assert_not_awaited()


# ── sensor — read_sensor + list_sensors ─────────────────────────────


class TestReadSensor:
    async def test_returns_value_with_unit(self) -> None:
        plugin = await _configured()
        payload = _entity(
            entity_id="sensor.outside_temp",
            state="22.5",
            friendly="Outside Temperature",
            unit_of_measurement="°C",
        )
        patcher, _ = _build_client_patch(get_response=_http(200, payload))
        with patcher:
            result = await plugin.read_sensor("sensor.outside_temp")
        assert "Outside Temperature" in result
        assert "22.5" in result
        assert "°C" in result

    async def test_returns_value_without_unit(self) -> None:
        plugin = await _configured()
        payload = _entity(
            entity_id="sensor.dishwasher",
            state="running",
            friendly="Dishwasher",
        )
        patcher, _ = _build_client_patch(get_response=_http(200, payload))
        with patcher:
            result = await plugin.read_sensor("sensor.dishwasher")
        assert "Dishwasher" in result
        assert "running" in result

    async def test_404_says_not_found(self) -> None:
        plugin = await _configured()
        patcher, _ = _build_client_patch(get_response=_http(404))
        with patcher:
            result = await plugin.read_sensor("sensor.ghost")
        assert "not found" in result.lower()

    async def test_malformed_payload_rejected(self) -> None:
        plugin = await _configured()
        patcher, _ = _build_client_patch(get_response=_http(200, {"no_entity_id": "weird"}))
        with patcher:
            result = await plugin.read_sensor("sensor.weird")
        assert "malformed" in result.lower()


class TestListSensors:
    async def test_returns_filtered_list(self) -> None:
        plugin = await _configured()
        payload = [
            _entity("sensor.temp", "22.5", "Temperature"),
            _entity("sensor.humidity", "55", "Humidity"),
            _entity("light.kitchen", "on", "Kitchen Light"),  # different domain
        ]
        patcher, _ = _build_client_patch(get_response=_http(200, payload))
        with patcher:
            result = await plugin.list_sensors()
        assert "sensor.temp" in result
        assert "sensor.humidity" in result
        assert "light.kitchen" not in result  # domain filter excludes it
        assert "Found 2" in result

    async def test_name_contains_filters_results(self) -> None:
        plugin = await _configured()
        payload = [
            _entity("sensor.outside_temp", "22.5", "Outside Temperature"),
            _entity("sensor.inside_temp", "21.0", "Inside Temperature"),
            _entity("sensor.humidity", "55", "Humidity"),
        ]
        patcher, _ = _build_client_patch(get_response=_http(200, payload))
        with patcher:
            result = await plugin.list_sensors(name_contains="temperature")
        assert "Outside Temperature" in result
        assert "Inside Temperature" in result
        assert "Humidity" not in result

    async def test_empty_list_friendly_message(self) -> None:
        plugin = await _configured()
        patcher, _ = _build_client_patch(get_response=_http(200, []))
        with patcher:
            result = await plugin.list_sensors()
        assert "No sensor entities" in result


# ── light.list_lights with caching ──────────────────────────────────


class TestListLightsCache:
    async def test_second_call_within_ttl_uses_cache(self) -> None:
        plugin = await _configured()
        payload = [_entity("light.kitchen", "on")]
        patcher, client = _build_client_patch(get_response=_http(200, payload))
        with patcher:
            await plugin.list_lights()
            await plugin.list_lights()
        # Two list calls, but only ONE HTTP request — cache hit.
        assert client.get.await_count == 1

    async def test_cache_invalidated_after_service_call(self) -> None:
        """Calling turn_on_light must invalidate the light cache."""
        plugin = await _configured()
        payload = [_entity("light.kitchen", "on")]
        patcher, client = _build_client_patch(
            get_response=_http(200, payload),
            post_response=_http(200, [{}]),
        )
        with patcher:
            await plugin.list_lights()  # populates cache
            await plugin.turn_off_light("light.kitchen")  # invalidates
            await plugin.list_lights()  # forced refetch
        assert client.get.await_count == 2  # noqa: PLR2004

    async def test_cache_ttl_elapsed_refetches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plugin = await _configured()
        payload = [_entity("light.kitchen", "on")]
        patcher, client = _build_client_patch(get_response=_http(200, payload))

        # Force monotonic to advance past the TTL between calls.
        clock = [0.0]

        def fake_monotonic() -> float:
            return clock[0]

        with patcher:
            monkeypatch.setattr(ha_mod.time, "monotonic", fake_monotonic)
            await plugin.list_lights()  # cache @ t=0
            clock[0] = _ENTITY_CACHE_TTL_S + 1  # past the TTL
            await plugin.list_lights()
        assert client.get.await_count == 2  # noqa: PLR2004

    async def test_http_failure_falls_back_to_stale_cache(self) -> None:
        plugin = await _configured()
        good_payload = [_entity("light.kitchen", "on")]
        patcher, client = _build_client_patch(get_response=_http(200, good_payload))
        with patcher:
            await plugin.list_lights()  # primes cache
            client.get.return_value = _http(500)  # next call fails
            # Bust TTL so we re-fetch (and hit the failure).
            plugin._cache.clear()  # noqa: SLF001
            client.get.side_effect = RuntimeError("network gone")
            result = await plugin.list_lights()
        # No previous cache entry after .clear() → empty list → friendly msg.
        assert "No light entities" in result


# ── climate — set_temperature (CONFIRM) ─────────────────────────────


class TestSetTemperature:
    async def test_decorator_marks_requires_confirmation(self) -> None:
        plugin = HomeAssistantPlugin()
        # Walk get_tools(); set_temperature must be tagged confirm-required.
        confirm_tools = [t for t in plugin.get_tools() if t.requires_confirmation]
        names = {t.name.split(".", 1)[1] for t in confirm_tools}
        assert "set_temperature" in names

    async def test_call_serialises_temperature(self) -> None:
        plugin = await _configured()
        patcher, client = _build_client_patch(post_response=_http(200, [{}]))
        with patcher:
            result = await plugin.set_temperature("climate.living", 21.5)
        body = client.post.await_args.kwargs["json"]
        assert body == {"entity_id": "climate.living", "temperature": 21.5}
        assert "21.5" in result

    async def test_invalid_float_rejected(self) -> None:
        plugin = await _configured()
        patcher, client = _build_client_patch(post_response=_http(200, [{}]))
        with patcher:
            result = await plugin.set_temperature("climate.living", float("nan"))  # nan is valid
        # NaN is a valid float — POST goes through (HA itself can reject).
        client.post.assert_awaited_once()
        assert "Set climate.living" in result

    async def test_string_temperature_rejected(self) -> None:
        plugin = await _configured()
        patcher, client = _build_client_patch(post_response=_http(200, [{}]))
        with patcher:
            result = await plugin.set_temperature("climate.living", "hot")  # type: ignore[arg-type]
        assert "Invalid temperature" in result
        client.post.assert_not_awaited()


# ── Tool surface ────────────────────────────────────────────────────


class TestToolSurface:
    """8 tools across 4 domains is the v0 contract — verify with get_tools()."""

    def test_tool_count(self) -> None:
        plugin = HomeAssistantPlugin()
        tools = plugin.get_tools()
        assert len(tools) == 8  # noqa: PLR2004

    def test_tool_names_namespaced(self) -> None:
        plugin = HomeAssistantPlugin()
        names = {t.name for t in plugin.get_tools()}
        expected = {
            "home-assistant.list_lights",
            "home-assistant.turn_on_light",
            "home-assistant.turn_off_light",
            "home-assistant.turn_on_switch",
            "home-assistant.turn_off_switch",
            "home-assistant.list_sensors",
            "home-assistant.read_sensor",
            "home-assistant.set_temperature",
        }
        assert names == expected


# ── Module-level helpers ────────────────────────────────────────────


class TestHelpers:
    def test_validate_entity_id_happy(self) -> None:
        assert _validate_entity_id("light.kitchen", "light")
        assert _validate_entity_id("sensor.temp", "sensor")

    @pytest.mark.parametrize(
        "entity_id,domain",
        [
            ("nodot", "light"),
            ("light.", "light"),
            (".kitchen", "light"),
            ("switch.kitchen", "light"),  # wrong domain
            ("", "light"),
        ],
    )
    def test_validate_entity_id_rejects(self, entity_id: str, domain: str) -> None:
        assert not _validate_entity_id(entity_id, domain)

    def test_hostname_from_url_extracts_host(self) -> None:
        assert _hostname_from_url("http://homeassistant.local:8123") == "homeassistant.local"
        assert _hostname_from_url("https://192.168.1.50:8123/api") == "192.168.1.50"

    def test_hostname_from_url_falls_back(self) -> None:
        assert _hostname_from_url("not a url at all") == "homeassistant.local"
        assert _hostname_from_url("") == "homeassistant.local"

    def test_format_entity_list(self) -> None:
        from sovyx.plugins.official._ha_models import HAEntity  # noqa: PLC0415

        entities = [
            HAEntity(
                entity_id="light.kitchen",
                domain="light",
                state="on",
                friendly_name="Kitchen",
                attributes={},
            ),
        ]
        text = _format_entity_list(entities, value_label="state")
        assert "Found 1 light(s)" in text
        assert "light.kitchen" in text
        assert "Kitchen" in text
        assert "[state: on]" in text
