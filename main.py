import asyncio
import os
import signal

from dotenv import load_dotenv

# Must run before any choir.* import: choir.engine.agent builds its Anthropic
# client at module import time, so ANTHROPIC_API_KEY has to already be in the
# environment by the time that import happens, not after.
load_dotenv()

from aiohttp import web
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from choir.store.profiles import init_db
from choir.bot.calendar_commands import cmd_connect_calendar
from choir.bot.handlers import handle_choir_command, track_group_member
from choir.bot.onboarding import (
    start_onboarding,
    handle_budget,
    handle_preferences,
    handle_area,
    handle_anything_else,
    cancel_onboarding,
    BUDGET,
    PREFERENCES,
    AREA,
    ANYTHING_ELSE,
)
from choir.calendar.server import build_web_app

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CALLBACK_PORT = int(os.environ.get("OAUTH_CALLBACK_PORT", "8765"))


async def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("choir", handle_choir_command))
    app.add_handler(CommandHandler("connect_calendar", cmd_connect_calendar))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.ALL, track_group_member), group=1)

    onboarding_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_onboarding, filters=filters.ChatType.PRIVATE)],
        states={
            BUDGET: [CallbackQueryHandler(handle_budget, pattern="^budget_")],
            PREFERENCES: [CallbackQueryHandler(handle_preferences, pattern="^pref_")],
            AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_area)],
            ANYTHING_ELSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_anything_else)],
        },
        fallbacks=[CommandHandler("cancel", cancel_onboarding)],
    )
    app.add_handler(onboarding_handler)

    # PTB's polling and the OAuth callback server share one asyncio event
    # loop, composed by hand via Application's init/start primitives instead
    # of the blocking app.run_polling() — the same sequence that method uses
    # internally, just interleaved with aiohttp's own runner so both can run
    # at once in this single process.
    web_runner = web.AppRunner(build_web_app(app.bot))
    await web_runner.setup()
    site = web.TCPSite(web_runner, "0.0.0.0", CALLBACK_PORT)

    async with app:
        await app.start()
        await app.updater.start_polling()
        await site.start()
        print(f"Bot is running, OAuth callback server on :{CALLBACK_PORT} (Ctrl+C to stop)")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()

        await app.updater.stop()
        await app.stop()
        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
