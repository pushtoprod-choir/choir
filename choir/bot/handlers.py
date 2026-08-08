import asyncio
import logging
import os
from urllib.parse import quote

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ContextTypes

from choir.store.profiles import get_profile, record_seen_member, get_seen_members, get_member_name
from choir.schemas import NegotiationRequest, AgentSignal
from choir.engine.orchestrator import format_transcript, run_negotiation
from choir.venues.places import build_venue_query, find_venues
from choir.venues.enrichment import enrich_venues
from choir.bot.intent import is_planning_request

# Chats with a negotiation currently in flight — guards against a second
# /choir stomping on a running one (e.g. someone double-tapping the command).
# Single-threaded event loop, so plain set membership checks are race-free
# as long as we don't await between the check and the add.
_active_negotiations: set[int] = set()

# Telegram's hard cap is 4096 chars; leave headroom below it since Markdown
# entities and the surrounding reply text add to the same budget — this
# margin also covers the "(+N more, trimmed for length)" suffix _build_
# venues_text appends after its own budget check, which isn't itself
# accounted for in that check.
TELEGRAM_MESSAGE_LIMIT = 4096
VENUES_SAFETY_MARGIN = 200


def _build_venues_text(venues: list, max_chars: int) -> str:
    """Renders the venue block, dropping trailing venues (cheapest ones to
    lose — they're already the least-relevant nearby-search results) until
    it fits max_chars, so a longer venue list can never push the combined
    message over Telegram's cap the way the transcript once did."""
    if not venues or max_chars <= 0:
        return ""

    venue_lines = []
    for v in venues:
        line = f"📍 *{v.name}*\n   {v.address}"
        if v.note:
            line += f"\n   💬 {v.note}"
        # place search resolves to the actual venue; a bare lat/lon query
        # drops you at a generic map pin instead. Query on the full
        # address (which LocationIQ already prefixes with the venue
        # name) rather than just the name, so generic names like "Snack
        # Corner" don't resolve to some other branch across town.
        line += f"\n   🔗 https://www.google.com/maps/place?q={quote(v.address)}"
        venue_lines.append(line)

    header = "\n\n*🍽️ Real options nearby:*\n\n"
    included: list[str] = []
    running_len = len(header)
    for line in venue_lines:
        addition = ("\n\n" if included else "") + line
        if running_len + len(addition) > max_chars:
            break
        included.append(line)
        running_len += len(addition)

    if not included:
        return ""
    text = header + "\n\n".join(included)
    omitted = len(venues) - len(included)
    if omitted:
        text += f"\n\n_(+{omitted} more nearby, trimmed for length)_"
    return text


async def track_group_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message is None or message.from_user is None or message.from_user.is_bot:
        return
    record_seen_member(message.chat_id, message.from_user.id, message.from_user.first_name)


async def handle_choir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if message.chat.type == ChatType.PRIVATE:
        await message.reply_text(
            "/choir only works in a group — add me to one and try there.", parse_mode=ParseMode.MARKDOWN
        )
        return

    goal_text = " ".join(context.args)  # everything after "/choir"

    if not goal_text:
        await message.reply_text(
            "Tell me what you want to plan — e.g. /choir plan lunch for us", parse_mode=ParseMode.MARKDOWN
        )
        return

    # Cheap gate so "/choir what's up" doesn't spin up a full negotiation —
    # only actually negotiate when this reads like a genuine planning ask.
    if not await asyncio.to_thread(is_planning_request, goal_text):
        await message.reply_text(
            "I'm here to help plan outings — try something like `/choir plan lunch for us` 🙂",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if message.chat_id in _active_negotiations:
        await message.reply_text(
            "Already negotiating for this group — hang tight for that one to finish.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # /choir itself counts as being "seen" in this chat
    record_seen_member(message.chat_id, message.from_user.id, message.from_user.first_name)

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
            f"say something here first, then try /choir again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not profiles:
        # Defensive — shouldn't happen since the /choir sender is always recorded above,
        # but a silent no-op is worse than a clear message if it ever does.
        await message.reply_text("Couldn't find anyone set up in this group yet.", parse_mode=ParseMode.MARKDOWN)
        return

    request = NegotiationRequest(
        group_chat_id=message.chat_id,
        goal_text=goal_text,
        profiles=profiles,
    )

    async def on_round(round_num: int, signals: list[AgentSignal]):
        lines = [f"🔁 *Round {round_num + 1}*"]
        for s in signals:
            name = get_member_name(message.chat_id, s.user_id)
            if s.stance == "ACCEPT":
                lines.append(f"✅ *{name}*: accepted")
            elif s.stance == "COUNTER":
                lines.append(f"🔄 *{name}*: countered — {s.reason}")
            else:
                lines.append(f"❌ *{name}*: rejected — {s.reason}")
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    # run_negotiation is synchronous; on_round is a coroutine, so drive it from a sync callback
    loop = asyncio.get_event_loop()

    def sync_on_round(round_num, signals):
        asyncio.run_coroutine_threadsafe(on_round(round_num, signals), loop)

    _active_negotiations.add(message.chat_id)
    try:
        result = await asyncio.to_thread(run_negotiation, request, sync_on_round)
    except NotImplementedError:
        await message.reply_text(
            "The negotiation engine isn't wired up yet — check back once it's built.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    finally:
        _active_negotiations.discard(message.chat_id)

    # Grounds the negotiated decision in real, nearby places. Best-effort —
    # find_venues() already fails soft on lookup problems, so a flaky venue
    # API should never take down the core negotiation result.
    venue_query = build_venue_query(profiles, goal_text)
    venues = await asyncio.to_thread(find_venues, venue_query, os.environ["LOCATION_IQ_API_KEY"])
    if venues:
        venues = await asyncio.to_thread(enrich_venues, venues, venue_query.purpose)

    if result.converged:
        reply = f"🎉 *Decision:* {result.decision}\n\n{result.explanation}"
        if result.tradeoffs:
            reply += "\n\n*Why:*\n" + "\n".join(f"• {t}" for t in result.tradeoffs)
        budget = TELEGRAM_MESSAGE_LIMIT - VENUES_SAFETY_MARGIN - len(reply)
        try:
            await message.reply_text(reply + _build_venues_text(venues, budget), parse_mode=ParseMode.MARKDOWN)
        except Exception:
            logging.exception("Failed to send decision message; retrying without venues")
            await message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
    else:
        options_text = "\n".join(f"• {opt}" for opt in result.top_options)
        base = f"🤔 *Couldn't fully agree — here are the top options:*\n{options_text}"
        budget = TELEGRAM_MESSAGE_LIMIT - VENUES_SAFETY_MARGIN - len(base)
        try:
            await message.reply_text(base + _build_venues_text(venues, budget), parse_mode=ParseMode.MARKDOWN)
        except Exception:
            logging.exception("Failed to send top-options message; retrying without venues")
            await message.reply_text(base, parse_mode=ParseMode.MARKDOWN)

    # The proof this was a real negotiation, not a single hidden API call —
    # sent as a follow-up so the main decision stays the headline message.
    if result.rounds:
        resolve_name = lambda user_id: get_member_name(message.chat_id, user_id)
        transcript = format_transcript(result.rounds, resolve_name)
        if len(transcript) > 3500:
            transcript = format_transcript(result.rounds[-2:], resolve_name) + "\n\n(earlier rounds omitted for length)"
        try:
            await message.reply_text(f"*📜 See how we got here:*\n\n{transcript}", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            logging.exception("Failed to send negotiation transcript")
