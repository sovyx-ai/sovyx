"""Sovyx Weather Plugin — Open-Meteo API (free, no key).

Built-in plugin that tests network sandbox, permissions, and brain
integration. Uses Open-Meteo geocoding + weather APIs.

Permissions required: network:internet (open-meteo.com)

Ref: SPE-008 §7.2, Appendix A.2
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from sovyx.plugins.sdk import ISovyxPlugin, tool

if TYPE_CHECKING:  # pragma: no cover
    from sovyx.plugins.sandbox_http import SandboxedHttpClient

# Open-Meteo API endpoints (free, no key)
_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes → descriptions
_WMO_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

_RAIN_CODES = {51, 53, 55, 61, 63, 65, 80, 81, 82, 95, 96, 99}


class WeatherPlugin(ISovyxPlugin):
    """Weather plugin using Open-Meteo (free, no API key)."""

    config_schema: ClassVar[dict[str, object]] = {
        "properties": {
            "default_city": {"type": "string"},
            "units": {"type": "string"},
        },
    }

    @property
    def name(self) -> str:
        return "weather"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Weather data via Open-Meteo (free, no API key)."

    @tool(description="Get current weather for a city.")
    async def get_weather(self, city: str) -> str:
        """Get current weather for a city.

        Args:
            city: City name (e.g. "São Paulo", "Berlin").

        Returns:
            Current weather description with temperature, humidity, wind.
        """
        coords = await _geocode(city)
        if coords is None:
            return f"Could not find city: {city}"

        lat, lon, display_name = coords
        data = await _fetch_weather(lat, lon, forecast_days=1)
        if data is None:
            return "Error fetching weather data."

        current = data.get("current", {})
        temp = current.get("temperature_2m", "?")
        humidity = current.get("relative_humidity_2m", "?")
        wind = current.get("wind_speed_10m", "?")
        code = current.get("weather_code", 0)
        condition = _WMO_CODES.get(int(code), "Unknown")

        return (
            f"Weather in {display_name}:\n"
            f"  {condition}, {temp}°C\n"
            f"  Humidity: {humidity}%\n"
            f"  Wind: {wind} km/h"
        )

    @tool(description="Get weather forecast for a city (up to 7 days).")
    async def get_forecast(self, city: str, days: int = 3) -> str:
        """Get weather forecast.

        Args:
            city: City name.
            days: Number of days (1-7, default 3).

        Returns:
            Daily forecast with high/low temps and conditions.
        """
        days = max(1, min(7, days))
        coords = await _geocode(city)
        if coords is None:
            return f"Could not find city: {city}"

        lat, lon, display_name = coords
        data = await _fetch_weather(lat, lon, forecast_days=days)
        if data is None:
            return "Error fetching forecast data."

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])
        lows = daily.get("temperature_2m_min", [])
        codes = daily.get("weather_code", [])

        lines = [f"Forecast for {display_name} ({days} days):"]
        for i in range(min(days, len(dates))):
            code = codes[i] if i < len(codes) else 0
            condition = _WMO_CODES.get(int(code), "Unknown")
            high = highs[i] if i < len(highs) else "?"
            low = lows[i] if i < len(lows) else "?"
            lines.append(f"  {dates[i]}: {condition}, {low}°C — {high}°C")

        return "\n".join(lines)

    @tool(description="Check if it will rain in a city today or tomorrow.")
    async def will_it_rain(self, city: str) -> str:
        """Check rain forecast.

        Args:
            city: City name.

        Returns:
            Rain prediction for today and tomorrow.
        """
        coords = await _geocode(city)
        if coords is None:
            return f"Could not find city: {city}"

        lat, lon, display_name = coords
        data = await _fetch_weather(lat, lon, forecast_days=2)
        if data is None:
            return "Error fetching weather data."

        daily = data.get("daily", {})
        codes = daily.get("weather_code", [])
        precip = daily.get("precipitation_sum", [])
        dates = daily.get("time", [])

        lines = [f"Rain forecast for {display_name}:"]
        for i in range(min(2, len(dates))):
            day_label = "Today" if i == 0 else "Tomorrow"
            code = codes[i] if i < len(codes) else 0
            rain = code in _RAIN_CODES
            mm = precip[i] if i < len(precip) else 0
            if rain:
                lines.append(f"  {day_label}: 🌧 Yes ({mm}mm expected)")
            else:
                lines.append(f"  {day_label}: ☀️ No rain expected")

        return "\n".join(lines)


# ── API Helpers ─────────────────────────────────────────────────────


_ALLOWED_DOMAINS = ["api.open-meteo.com", "geocoding-api.open-meteo.com"]


def _make_client() -> SandboxedHttpClient:
    """Build a sandboxed HTTP client scoped to Open-Meteo domains.

    Routes the plugin through :class:`sovyx.plugins.sandbox_http.SandboxedHttpClient`
    so that domain allowlist, local-network blocking, rate limit, and
    response size cap apply uniformly — keeping official plugins honest
    with the sandbox they ship with.
    """
    from sovyx.plugins.sandbox_http import SandboxedHttpClient

    return SandboxedHttpClient(
        plugin_name="weather",
        allowed_domains=_ALLOWED_DOMAINS,
        timeout_s=10.0,
    )


async def _geocode(city: str) -> tuple[float, float, str] | None:
    """Geocode a city name to coordinates via Open-Meteo.

    Returns (latitude, longitude, display_name) or None.
    """
    client = _make_client()
    try:
        resp = await client.get(
            _GEOCODING_URL,
            params={"name": city, "count": 1, "language": "en"},
        )
        if resp.status_code != 200:  # noqa: PLR2004
            return None
        data: dict[str, Any] = resp.json()
        results = data.get("results", [])
        if not results:
            return None
        r = results[0]
        lat = float(r["latitude"])
        lon = float(r["longitude"])
        name = r.get("name", city)
        country = r.get("country", "")
        display = f"{name}, {country}" if country else name
        return (lat, lon, display)
    except Exception:  # noqa: BLE001
        return None
    finally:
        await client.close()


async def _fetch_weather(
    lat: float,
    lon: float,
    forecast_days: int = 1,
) -> dict[str, Any] | None:
    """Fetch weather data from Open-Meteo.

    Returns parsed JSON or None on error.
    """
    params: dict[str, str | float | int] = {
        "latitude": lat,
        "longitude": lon,
        "current": ("temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"),
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum"),
        "forecast_days": forecast_days,
        "timezone": "auto",
    }
    client = _make_client()
    try:
        resp = await client.get(_WEATHER_URL, params=params)
        if resp.status_code != 200:  # noqa: PLR2004
            return None
        result: dict[str, Any] = resp.json()
        return result
    except Exception:  # noqa: BLE001
        return None
    finally:
        await client.close()
