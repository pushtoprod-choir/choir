"""Standalone entry point for connecting Google Calendar, for anyone who
onboarded before this feature existed (the onboarding flow itself also offers
this — see choir/bot/onboarding.py)."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from choir.calendar.oauth import generate_auth_url
from choir.store.calendar_tokens import is_calendar_connected


async def cmd_connect_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.chat.type != ChatType.PRIVATE:
        await message.reply_text(f"DM me to connect your calendar — t.me/{context.bot.username}")
        return

    user_id = message.from_user.id
    if is_calendar_connected(user_id):
        await message.reply_text("Your Google Calendar is already connected.")
        return

    keyboard = [[InlineKeyboardButton("Connect Google Calendar", url=generate_auth_url(user_id))]]
    await message.reply_text(
        "Connect your Google Calendar so I can factor your real availability into negotiations "
        "(optional — everything still works without it).",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
