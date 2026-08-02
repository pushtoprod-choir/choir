from telegram import Update
from telegram.ext import ContextTypes

from choir.store.profiles import get_profile, record_seen_member, get_seen_members
from choir.schemas import NegotiationRequest, AgentSignal
from choir.engine.orchestrator import run_negotiation


async def track_group_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message is None or message.from_user is None:
        return
    record_seen_member(message.chat_id, message.from_user.id)


async def handle_choir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    goal_text = " ".join(context.args)  # everything after "/choir"

    if not goal_text:
        await message.reply_text("Tell me what you want to plan — e.g. /choir plan lunch for us")
        return

    # /choir itself counts as being "seen" in this chat
    record_seen_member(message.chat_id, message.from_user.id)

    member_ids = get_seen_members(message.chat_id)
    profiles = []
    missing_users = []

    for user_id in member_ids:
        profile = get_profile(user_id)
        if profile is None:
            missing_users.append(user_id)
        else:
            profiles.append(profile)

    if missing_users:
        bot_username = context.bot.username
        await message.reply_text(
            f"Some of you haven't set up Choir yet. Tap this and hit Start: "
            f"t.me/{bot_username}"
        )
        return

    request = NegotiationRequest(
        group_chat_id=message.chat_id,
        goal_text=goal_text,
        profiles=profiles,
    )

    async def on_round(round_num: int, signals: list[AgentSignal]):
        countered = [s for s in signals if s.stance == "COUNTER"]
        accepted = [s for s in signals if s.stance == "ACCEPT"]
        if countered:
            await message.reply_text(
                f"Round {round_num + 1}: {len(accepted)}/{len(signals)} agreed so far — "
                f"someone's countering with a new idea..."
            )
        else:
            await message.reply_text(f"Round {round_num + 1}: {len(accepted)}/{len(signals)} agreed so far...")

    # run_negotiation is synchronous; on_round is a coroutine, so drive it from a sync callback
    import asyncio
    loop = asyncio.get_event_loop()

    def sync_on_round(round_num, signals):
        asyncio.run_coroutine_threadsafe(on_round(round_num, signals), loop)

    try:
        result = await asyncio.to_thread(run_negotiation, request, sync_on_round)
    except NotImplementedError:
        await message.reply_text("The negotiation engine isn't wired up yet — check back once it's built.")
        return

    if result.converged:
        reply = f"{result.decision}\n\n{result.explanation}"
        if result.tradeoffs:
            reply += "\n\nWhy:\n" + "\n".join(f"- {t}" for t in result.tradeoffs)
        await message.reply_text(reply)
    else:
        options_text = "\n".join(f"- {opt}" for opt in result.top_options)
        await message.reply_text(f"Couldn't fully agree — here are the top options:\n{options_text}")
