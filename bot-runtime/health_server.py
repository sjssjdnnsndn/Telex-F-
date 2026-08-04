"""Small unauthenticated health server that shares the bot's event loop."""

from __future__ import annotations

import asyncio
import json
import os
from aiohttp import web


def _payload() -> dict[str, object]:
    return {
        "status": "ok",
        "service": "telegram-bot",
        "polling": True,
    }


async def health(_: web.Request) -> web.Response:
    return web.json_response(_payload())


async def root(_: web.Request) -> web.Response:
    return web.Response(
        text=json.dumps(_payload()),
        content_type="application/json",
    )


async def start_health_server() -> tuple[web.AppRunner, web.TCPSite]:
    raw_port = os.environ.get("PORT")
    if not raw_port:
        raise RuntimeError("PORT environment variable is required for health server")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise RuntimeError(f"PORT must be an integer, got {raw_port!r}") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError(f"PORT is outside the valid range: {port}")

    app = web.Application()
    for path in ("/", "/health", "/api", "/api/health", "/ping"):
        app.router.add_get(path, health if path != "/" else root)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner, site


async def stop_health_server(runner: web.AppRunner) -> None:
    await runner.cleanup()
