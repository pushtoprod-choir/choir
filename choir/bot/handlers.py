import asyncio

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from choir.store.profiles import get_profile, record_seen_member, get_seen_members
from choir.schemas import NegotiationRequest, AgentSignal
from choir.engine.orchestrator import format_transcript, run_negotiation

# Chats with a negotiation currently in flight — guards against a second
# /choir stomping on a running one (e.g. someone double-tapping the command).
# Single-threaded event loop, so plain set membership checks are race-free
# as long as we don't await between the check and the add.
_active_negotiations: set[int] = set()


async def track_group_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message is None or message.from_user is None or message.from_user.is_bot:
        return
    record_seen_member(message.chat_id, message.from_user.id)


async def handle_choir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if message.chat.type == ChatType.PRIVATE:
        await message.reply_text("/choir only works in a group — add me to one and try there.")
        return

    goal_text = " ".join(context.args)  # everything after "/choir"

    if not goal_text:
        await message.reply_text("Tell me what you want to plan — e.g. /choir plan lunch for us")
        return

    if message.chat_id in _active_negotiations:
        await message.reply_text("Already negotiating for this group — hang tight for that one to finish.")
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
            f"Some of you haven't set up Choir yet — tap this and hit Start: t.me/{bot_username}\n\n"
            f"Already did that? I can only see people who've sent at least one message in this group — "
            f"say something here first, then try /choir again."
        )
        return

    if not profiles:
        # Defensive — shouldn't happen since the /choir sender is always recorded above,
        # but a silent no-op is worse than a clear message if it ever does.
        await message.reply_text("Couldn't find anyone set up in this group yet.")
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
    loop = asyncio.get_event_loop()

    def sync_on_round(round_num, signals):
        asyncio.run_coroutine_threadsafe(on_round(round_num, signals), loop)

    _active_negotiations.add(message.chat_id)
    try:
        result = await asyncio.to_thread(run_negotiation, request, sync_on_round)
    except NotImplementedError:
        await message.reply_text("The negotiation engine isn't wired up yet — check back once it's built.")
        return
    finally:
        _active_negotiations.discard(message.chat_id)

    if result.converged:
        reply = f"{result.decision}\n\n{result.explanation}"
        if result.tradeoffs:
            reply += "\n\nWhy:\n" + "\n".join(f"- {t}" for t in result.tradeoffs)
        await message.reply_text(reply)
    else:
        options_text = "\n".join(f"- {opt}" for opt in result.top_options)
        await message.reply_text(f"Couldn't fully agree — here are the top options:\n{options_text}")

    # The proof this was a real negotiation, not a single hidden API call —
    # sent as a follow-up so the main decision stays the headline message.
    if result.rounds:
        await message.reply_text("See how we got here:\n\n" + format_transcript(result.rounds))
