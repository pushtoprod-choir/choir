import os
from dotenv import load_dotenv

# Must run before any choir.* import: choir.engine.agent builds its Anthropic
# client at module import time, so ANTHROPIC_API_KEY has to already be in the
# environment by the time that import happens, not after.
load_dotenv()

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

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("choir", handle_choir_command))
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

    print("Bot is running... press Ctrl+C to stop")
    app.run_polling()


if __name__ == "__main__":
    main()
