"""Channel status + Telegram bot-token setup endpoints."""

from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/channels", dependencies=[Depends(verify_token)])


@router.get("")
async def channels(request: Request) -> JSONResponse:
    """Return active channel status.

    Lists all available channels and whether they are connected. Dashboard
    is always active when the engine is running.
    """
    registry = getattr(request.app.state, "registry", None)
    channel_list: list[dict[str, object]] = [
        {
            "name": "dashboard",
            "type": "dashboard",
            "connected": registry is not None,
        },
    ]

    if registry is not None:
        from sovyx.bridge.manager import BridgeManager
        from sovyx.engine.types import ChannelType

        bridge: BridgeManager | None = None
        with contextlib.suppress(Exception):
            bridge = await registry.resolve(BridgeManager)

        active_types: set[str] = set()
        if bridge is not None:
            active_types = {ct.value for ct in bridge._adapters}

        channel_list.append(
            {
                "name": "Telegram",
                "type": "telegram",
                "connected": ChannelType.TELEGRAM.value in active_types,
            }
        )
        channel_list.append(
            {
                "name": "Signal",
                "type": "signal",
                "connected": ChannelType.SIGNAL.value in active_types,
            }
        )
    else:
        channel_list.extend(
            [
                {"name": "Telegram", "type": "telegram", "connected": False},
                {"name": "Signal", "type": "signal", "connected": False},
            ]
        )

    return JSONResponse({"channels": channel_list})


@router.post("/telegram/setup")
async def setup_telegram(request: Request) -> JSONResponse:
    """Validate a Telegram bot token and persist it for next restart.

    1. Calls Telegram getMe to validate the token.
    2. Writes SOVYX_TELEGRAM_TOKEN to ``{data_dir}/channel.env``.
    3. Returns bot info on success.
    """
    import aiohttp

    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(
            {"ok": False, "error": "Invalid JSON body"},
            status_code=422,
        )

    token = (body.get("token") or "").strip() if isinstance(body, dict) else ""
    if not token:
        return JSONResponse(
            {"ok": False, "error": "Token is required"},
            status_code=422,
        )

    # Validate via Telegram API.
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp,
        ):
            data = await resp.json()
            if not data.get("ok"):
                return JSONResponse(
                    {
                        "ok": False,
                        "error": data.get("description", "Invalid token"),
                    },
                    status_code=400,
                )
            bot_info = data["result"]
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": "Could not reach Telegram API"},
            status_code=502,
        )

    # Persist token to channel.env in data_dir.
    engine_config = getattr(request.app.state, "engine_config", None)
    data_dir = engine_config.data_dir if engine_config is not None else Path.home() / ".sovyx"
    env_path = data_dir / "channel.env"
    try:
        existing: dict[str, str] = {}
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()  # noqa: PLW2901
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    existing[k.strip()] = v.strip()
        existing["SOVYX_TELEGRAM_TOKEN"] = token

        env_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{k}={v}" for k, v in existing.items()]
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        env_path.chmod(0o600)
    except Exception:  # noqa: BLE001
        logger.warning("channel_env_write_failed", path=str(env_path))

    bot_username = bot_info.get("username", "")
    logger.info(
        "telegram_token_validated",
        bot_username=bot_username,
    )

    return JSONResponse(
        {
            "ok": True,
            "bot_username": bot_username,
            "bot_name": bot_info.get("first_name", ""),
            "requires_restart": True,
        }
    )
