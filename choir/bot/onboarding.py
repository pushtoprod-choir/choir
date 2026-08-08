from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from choir.calendar.oauth import generate_auth_url
from choir.schemas import UserProfile
from choir.store.profiles import save_profile, record_seen_member

BUDGET, PREFERENCES, AREA, DIETARY, ANYTHING_ELSE = range(5)

PREFERENCE_OPTIONS = {
    "pref_cafe": "cafe_person",
    "pref_foodie": "foodie",
    "pref_budget": "budget_conscious",
    "pref_quiet": "quiet_places",
    "pref_vegetarian": "vegetarian",
}


def _preferences_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    # Selected tags get a checkmark so tapping one gives immediate visible
    # feedback instead of the button looking untouched.
    rows = [
        [InlineKeyboardButton(
            f"✅ {tag.replace('_', ' ').title()}" if tag in selected else tag.replace("_", " ").title(),
            callback_data=key,
        )]
        for key, tag in PREFERENCE_OPTIONS.items()
    ]
    rows.append([InlineKeyboardButton("Done picking ➡️", callback_data="pref_done")])
    return InlineKeyboardMarkup(rows)


def _preferences_step_text(selected: list[str]) -> str:
    text = "*Step 2/5:* Pick a couple that fit you:"
    if selected:
        labels = ", ".join(tag.replace("_", " ").title() for tag in selected)
        text += f"\n\nSelected: {labels}"
    return text


async def start_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["preferences"] = []
    keyboard = [
        [InlineKeyboardButton("Under ₹500", callback_data="budget_0_500")],
        [InlineKeyboardButton("₹500–800", callback_data="budget_500_800")],
        [InlineKeyboardButton("₹800–1500", callback_data="budget_800_1500")],
    ]
    await update.message.reply_text(
        "*Hi, I'm your Choir representative.* Quick setup, about 30 seconds — this is what I'll "
        "privately negotiate on your behalf with, no one else in the group sees your exact answers.\n\n"
        "*Step 1/5:* Usual budget for an outing?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )
    return BUDGET


async def handle_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, low, high = query.data.split("_")
    context.user_data["budget_min"], context.user_data["budget_max"] = int(low), int(high)

    await query.edit_message_text(
        _preferences_step_text(context.user_data["preferences"]),
        reply_markup=_preferences_keyboard(context.user_data["preferences"]),
        parse_mode=ParseMode.MARKDOWN,
    )
    return PREFERENCES


async def handle_preferences(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "pref_done":
        await query.edit_message_text(
            "*Step 3/5:* Where are you usually around? (e.g. HSR Layout)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return AREA

    tag = PREFERENCE_OPTIONS.get(query.data)
    if tag:
        # Toggle — tapping a selected preference again deselects it, so a
        # mis-tap is correctable without restarting the whole flow.
        if tag in context.user_data["preferences"]:
            context.user_data["preferences"].remove(tag)
        else:
            context.user_data["preferences"].append(tag)

    await query.edit_message_text(
        _preferences_step_text(context.user_data["preferences"]),
        reply_markup=_preferences_keyboard(context.user_data["preferences"]),
        parse_mode=ParseMode.MARKDOWN,
    )
    return PREFERENCES


async def handle_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["area"] = update.message.text.strip()
    await update.message.reply_text(
        "*Step 4/5:* Any dietary restrictions or allergies we should know? "
        "(e.g. vegan, nut allergy — or send \"none\")",
        parse_mode=ParseMode.MARKDOWN,
    )
    return DIETARY


async def handle_dietary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["dietary_notes"] = None if text.lower() == "none" else text
    await update.message.reply_text(
        "*Step 5/5:* Anything else specific about you we should know? "
        "(a scheduling quirk, anything else useful — or send \"skip\")",
        parse_mode=ParseMode.MARKDOWN,
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
        dietary_notes=context.user_data.get("dietary_notes"),
        notes=notes,
    )
    save_profile(profile)
    # Records/backfills first_name off onboarding (a DM, so there's no real
    # group chat_id here) so get_member_name resolves correctly in every
    # group this user is later seen in, even if they never send a fresh
    # group message after this.
    record_seen_member(update.effective_chat.id, update.effective_user.id, update.effective_user.first_name)

    keyboard = [[InlineKeyboardButton("Connect Google Calendar", url=generate_auth_url(profile.telegram_user_id))]]
    await update.message.reply_text(
        f"*All set ✅* Budget ₹{profile.budget_min}-₹{profile.budget_max}, "
        f"around {profile.area}. I'll negotiate on your behalf from now on — privately, using what you told me here.\n\n"
        "One optional extra: connect your Google Calendar so I know when you're actually free "
        "(never a blocker — skip it and everything still works). You can always do this later "
        "with /connect_calendar too.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Setup cancelled. Send /start whenever you're ready.")
    return ConversationHandler.END
