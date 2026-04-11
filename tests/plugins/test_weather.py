"""Tests for Sovyx Weather Plugin (TASK-444)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.plugins.official.weather import (
    _RAIN_CODES,
    _WMO_CODES,
    WeatherPlugin,
    _fetch_weather,
    _geocode,
)


def _mock_geocode_response(
    name: str = "Berlin",
    country: str = "Germany",
    lat: float = 52.52,
    lon: float = 13.41,
) -> dict:
    return {
        "results": [
            {
                "name": name,
                "country": country,
                "latitude": lat,
                "longitude": lon,
            }
        ]
    }


def _mock_weather_response(
    temp: float = 22.5,
    humidity: int = 55,
    wind: float = 12.0,
    code: int = 0,
    forecast_days: int = 1,
) -> dict:
    daily_codes = [code] * forecast_days
    return {
        "current": {
            "temperature_2m": temp,
            "relative_humidity_2m": humidity,
            "weather_code": code,
            "wind_speed_10m": wind,
        },
        "daily": {
            "time": [f"2026-04-{11 + i}" for i in range(forecast_days)],
            "weather_code": daily_codes,
            "temperature_2m_max": [temp + 5] * forecast_days,
            "temperature_2m_min": [temp - 5] * forecast_days,
            "precipitation_sum": [0.0 if code not in _RAIN_CODES else 5.2] * forecast_days,
        },
    }


class TestWeatherPlugin:
    """Tests for WeatherPlugin."""

    def test_name(self) -> None:
        assert WeatherPlugin().name == "weather"

    def test_version(self) -> None:
        assert WeatherPlugin().version == "1.0.0"

    def test_description(self) -> None:
        assert "Open-Meteo" in WeatherPlugin().description


class TestGetWeather:
    """Tests for get_weather tool."""

    @pytest.mark.anyio()
    async def test_success(self) -> None:
        p = WeatherPlugin()
        with (
            patch(
                "sovyx.plugins.official.weather._geocode",
                new_callable=AsyncMock,
                return_value=(52.52, 13.41, "Berlin, Germany"),
            ),
            patch(
                "sovyx.plugins.official.weather._fetch_weather",
                new_callable=AsyncMock,
                return_value=_mock_weather_response(),
            ),
        ):
            result = await p.get_weather("Berlin")

        assert "Berlin" in result
        assert "22.5°C" in result
        assert "Clear sky" in result

    @pytest.mark.anyio()
    async def test_city_not_found(self) -> None:
        p = WeatherPlugin()
        with patch(
            "sovyx.plugins.official.weather._geocode",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await p.get_weather("Nonexistentville")
        assert "Could not find" in result

    @pytest.mark.anyio()
    async def test_api_error(self) -> None:
        p = WeatherPlugin()
        with (
            patch(
                "sovyx.plugins.official.weather._geocode",
                new_callable=AsyncMock,
                return_value=(0, 0, "X"),
            ),
            patch(
                "sovyx.plugins.official.weather._fetch_weather",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await p.get_weather("X")
        assert "Error" in result


class TestGetForecast:
    """Tests for get_forecast tool."""

    @pytest.mark.anyio()
    async def test_3_day_forecast(self) -> None:
        p = WeatherPlugin()
        with (
            patch(
                "sovyx.plugins.official.weather._geocode",
                new_callable=AsyncMock,
                return_value=(52.52, 13.41, "Berlin, Germany"),
            ),
            patch(
                "sovyx.plugins.official.weather._fetch_weather",
                new_callable=AsyncMock,
                return_value=_mock_weather_response(forecast_days=3),
            ),
        ):
            result = await p.get_forecast("Berlin", days=3)

        assert "Berlin" in result
        assert "3 days" in result
        assert "2026-04-11" in result

    @pytest.mark.anyio()
    async def test_days_clamped(self) -> None:
        """Days clamped to 1-7 range."""
        p = WeatherPlugin()
        with (
            patch(
                "sovyx.plugins.official.weather._geocode",
                new_callable=AsyncMock,
                return_value=(0, 0, "X"),
            ),
            patch(
                "sovyx.plugins.official.weather._fetch_weather",
                new_callable=AsyncMock,
                return_value=_mock_weather_response(forecast_days=7),
            ),
        ):
            result = await p.get_forecast("X", days=99)
        assert "7 days" in result

    @pytest.mark.anyio()
    async def test_forecast_city_not_found(self) -> None:
        p = WeatherPlugin()
        with patch(
            "sovyx.plugins.official.weather._geocode",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await p.get_forecast("Ghost")
        assert "Could not find" in result

    @pytest.mark.anyio()
    async def test_forecast_api_error(self) -> None:
        p = WeatherPlugin()
        with (
            patch(
                "sovyx.plugins.official.weather._geocode",
                new_callable=AsyncMock,
                return_value=(0, 0, "X"),
            ),
            patch(
                "sovyx.plugins.official.weather._fetch_weather",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await p.get_forecast("X")
        assert "Error" in result


class TestWillItRain:
    """Tests for will_it_rain tool."""

    @pytest.mark.anyio()
    async def test_no_rain(self) -> None:
        p = WeatherPlugin()
        with (
            patch(
                "sovyx.plugins.official.weather._geocode",
                new_callable=AsyncMock,
                return_value=(0, 0, "SP, Brazil"),
            ),
            patch(
                "sovyx.plugins.official.weather._fetch_weather",
                new_callable=AsyncMock,
                return_value=_mock_weather_response(code=0, forecast_days=2),
            ),
        ):
            result = await p.will_it_rain("SP")
        assert "No rain" in result

    @pytest.mark.anyio()
    async def test_rain_expected(self) -> None:
        p = WeatherPlugin()
        with (
            patch(
                "sovyx.plugins.official.weather._geocode",
                new_callable=AsyncMock,
                return_value=(0, 0, "London, UK"),
            ),
            patch(
                "sovyx.plugins.official.weather._fetch_weather",
                new_callable=AsyncMock,
                return_value=_mock_weather_response(code=63, forecast_days=2),
            ),
        ):
            result = await p.will_it_rain("London")
        assert "Yes" in result
        assert "mm" in result

    @pytest.mark.anyio()
    async def test_rain_city_not_found(self) -> None:
        p = WeatherPlugin()
        with patch(
            "sovyx.plugins.official.weather._geocode",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await p.will_it_rain("Ghost")
        assert "Could not find" in result

    @pytest.mark.anyio()
    async def test_rain_api_error(self) -> None:
        p = WeatherPlugin()
        with (
            patch(
                "sovyx.plugins.official.weather._geocode",
                new_callable=AsyncMock,
                return_value=(0, 0, "X"),
            ),
            patch(
                "sovyx.plugins.official.weather._fetch_weather",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await p.will_it_rain("X")
        assert "Error" in result


class TestGeocode:
    """Tests for _geocode helper."""

    @pytest.mark.anyio()
    async def test_success(self) -> None:

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _mock_geocode_response()

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await _geocode("Berlin")

        assert result is not None
        assert result[0] == 52.52
        assert result[2] == "Berlin, Germany"

    @pytest.mark.anyio()
    async def test_no_results(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await _geocode("ZZZZZ")
        assert result is None

    @pytest.mark.anyio()
    async def test_api_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await _geocode("Berlin")
        assert result is None

    @pytest.mark.anyio()
    async def test_exception(self) -> None:
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("network"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await _geocode("Berlin")
        assert result is None

    @pytest.mark.anyio()
    async def test_no_country(self) -> None:
        """City without country field."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": [{"name": "X", "latitude": 0, "longitude": 0}]}

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await _geocode("X")
        assert result is not None
        assert result[2] == "X"


class TestFetchWeather:
    """Tests for _fetch_weather helper."""

    @pytest.mark.anyio()
    async def test_success(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _mock_weather_response()

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await _fetch_weather(52.52, 13.41)
        assert result is not None
        assert "current" in result

    @pytest.mark.anyio()
    async def test_api_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await _fetch_weather(0, 0)
        assert result is None

    @pytest.mark.anyio()
    async def test_exception(self) -> None:
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await _fetch_weather(0, 0)
        assert result is None


class TestWMOCodes:
    """Tests for WMO weather code mappings."""

    def test_rain_codes_subset(self) -> None:
        """All rain codes exist in WMO codes."""
        for code in _RAIN_CODES:
            assert code in _WMO_CODES

    def test_clear_sky(self) -> None:
        assert _WMO_CODES[0] == "Clear sky"
