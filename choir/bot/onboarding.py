from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from choir.schemas import UserProfile
from choir.store.profiles import save_profile, record_seen_member

BUDGET, PREFERENCES, AREA, ANYTHING_ELSE = range(4)

PREFERENCE_OPTIONS = {
    "pref_cafe": "cafe_person",
    "pref_foodie": "foodie",
    "pref_budget": "budget_conscious",
    "pref_quiet": "quiet_places",
    "pref_vegetarian": "vegetarian",
}


async def start_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["preferences"] = []
    keyboard = [
        [InlineKeyboardButton("Under ₹500", callback_data="budget_0_500")],
        [InlineKeyboardButton("₹500–800", callback_data="budget_500_800")],
        [InlineKeyboardButton("₹800–1500", callback_data="budget_800_1500")],
    ]
    await update.message.reply_text(
        "Hi, I'm your Choir representative. Quick setup, about 30 seconds.\n\nUsual budget for an outing?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return BUDGET


async def handle_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, low, high = query.data.split("_")
    context.user_data["budget_min"], context.user_data["budget_max"] = int(low), int(high)

    keyboard = [
        [InlineKeyboardButton(label.replace("_", " ").title(), callback_data=key)]
        for key, label in PREFERENCE_OPTIONS.items()
    ]
    keyboard.append([InlineKeyboardButton("Done picking", callback_data="pref_done")])
    await query.edit_message_text("Pick a couple that fit you:", reply_markup=InlineKeyboardMarkup(keyboard))
    return PREFERENCES


async def handle_preferences(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "pref_done":
        await query.edit_message_text("Last thing — where are you usually around? (e.g. HSR Layout)")
        return AREA

    tag = PREFERENCE_OPTIONS.get(query.data)
    if tag and tag not in context.user_data["preferences"]:
        context.user_data["preferences"].append(tag)
    return PREFERENCES


async def handle_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["area"] = update.message.text.strip()
    await update.message.reply_text(
        "Last thing — anything specific about you we should know? "
        "(dietary needs, a scheduling quirk, whatever's useful — or send \"skip\")"
    )
    return ANYTHING_ELSE


async def handle_anything_else(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    notes = None if text.lower() == "skip" else text

    profile = UserProfile(
        telegram_user_id=update.message.from_user.id,
        budget_min=context.user_data["budget_min"],
        budget_max=context.user_data["budget_max"],
        preferences=context.user_data.get("preferences", []),
        area=context.user_data["area"],
        notes=notes,
    )
    save_profile(profile)
    # Records/backfills first_name off onboarding (a DM, so there's no real
    # group chat_id here) so get_member_name resolves correctly in every
    # group this user is later seen in, even if they never send a fresh
    # group message after this.
    record_seen_member(update.effective_chat.id, update.effective_user.id, update.effective_user.first_name)
    await update.message.reply_text(
        f"All set. Budget ₹{profile.budget_min}-₹{profile.budget_max}, "
        f"around {profile.area}. I'll negotiate on your behalf from now on."
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Setup cancelled. Send /start whenever you're ready.")
    return ConversationHandler.END
