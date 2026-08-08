"""aiohttp app serving the Google OAuth callback. Runs on the same asyncio
event loop as python-telegram-bot's polling — composed together in main.py.
This project has no other web server, so this stays intentionally tiny: one
route, no templates, no session framework.
"""
import asyncio
import logging
import time

from aiohttp import web
from telegram import Bot

from choir.calendar.oauth import exchange_code, pop_pending_user
from choir.store.calendar_tokens import save_calendar_tokens

CONFIRMATION_MESSAGE = (
    "✅ Google Calendar connected — I'll factor your real availability into future negotiations."
)


def build_web_app(bot: Bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/oauth/callback", _oauth_callback)
    return app


async def _oauth_callback(request: web.Request) -> web.Response:
    error = request.query.get("error")
    code = request.query.get("code")
    state = request.query.get("state")

    if error or not code or not state:
        return web.Response(text="Connection failed or was cancelled. You can close this tab.", status=400)

    telegram_user_id = pop_pending_user(state)
    if telegram_user_id is None:
        return web.Response(text="This link expired — run /connect_calendar again.", status=400)

    # Blocking network call — offloaded so it doesn't stall the shared event
    # loop that's also serving Telegram polling.
    tokens = await asyncio.to_thread(exchange_code, code)
    if tokens is None or "access_token" not in tokens or "refresh_token" not in tokens:
        return web.Response(text="Couldn't reach Google to finish connecting. Try again.", status=502)

    expiry = int(time.time()) + int(tokens.get("expires_in", 3600))
    save_calendar_tokens(telegram_user_id, tokens["access_token"], tokens["refresh_token"], expiry)

    bot: Bot = request.app["bot"]
    try:
        await bot.send_message(chat_id=telegram_user_id, text=CONFIRMATION_MESSAGE)
    except Exception:
        # Best-effort — e.g. the user blocked the bot since starting the
        # flow. The connection itself already succeeded, so this shouldn't
        # turn into a failed HTTP response.
        logging.exception("Couldn't DM calendar-connected confirmation to %s", telegram_user_id)

    return web.Response(text="Google Calendar connected. You can close this tab.", content_type="text/html")
