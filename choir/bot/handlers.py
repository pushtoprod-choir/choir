import asyncio
import logging
import os

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ContextTypes

from choir.store.profiles import get_profile, record_seen_member, get_seen_members, get_member_name
from choir.schemas import NegotiationRequest, AgentSignal
from choir.engine.orchestrator import format_transcript, run_negotiation
from choir.venues.places import build_venue_query, find_venues
from choir.venues.enrichment import enrich_venues
from choir.calendar.client import attach_calendar_availability
from choir.calendar.scheduling import create_events_for_connected

# Chats with a negotiation currently in flight — guards against a second
# /choir stomping on a running one (e.g. someone double-tapping the command).
# Single-threaded event loop, so plain set membership checks are race-free
# as long as we don't await between the check and the add.
_active_negotiations: set[int] = set()


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

    # Best-effort — attach_calendar_availability leaves calendar_busy_text as
    # None (its default) for anyone not connected or on any lookup failure,
    # so this is a no-op for groups where nobody's connected their calendar.
    await asyncio.to_thread(attach_calendar_availability, profiles)

    request = NegotiationRequest(
        group_chat_id=message.chat_id,
        goal_text=goal_text,
        profiles=profiles,
    )

    async def on_round(round_num: int, signals: list[AgentSignal]):
        lines = [f"*Round {round_num + 1}:*"]
        for s in signals:
            name = get_member_name(message.chat_id, s.user_id)
            if s.stance == "ACCEPT":
                lines.append(f"  *{name}*: accepted")
            elif s.stance == "COUNTER":
                lines.append(f"  *{name}*: countered — {s.reason}")
            else:
                lines.append(f"  *{name}*: rejected — {s.reason}")
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
    venues_text = ""
    if venues:
        venue_lines = []
        for v in venues:
            line = f"- *{v.name}* ({v.address})"
            if v.note:
                line += f" — {v.note}"
            if v.lat and v.lon:
                line += f"\n  📍 https://www.google.com/maps?q={v.lat},{v.lon}"
            venue_lines.append(line)
        venues_text = "\n\n*Real options nearby:*\n" + "\n".join(venue_lines)

    if result.converged:
        reply = f"{result.decision}\n\n{result.explanation}"
        if result.tradeoffs:
            reply += "\n\n*Why:*\n" + "\n".join(f"- {t}" for t in result.tradeoffs)

        # None means nobody in this group is calendar-connected — a true
        # no-op, keeping this line absent entirely for unconnected groups.
        calendar_summary = await asyncio.to_thread(
            create_events_for_connected, profiles, goal_text, result.decision
        )
        if calendar_summary is not None:
            if calendar_summary.extraction_failed:
                reply += "\n\n📅 Couldn't auto-schedule this — add it to your calendar manually."
            else:
                reply += f"\n\n📅 Added to {calendar_summary.created}/{calendar_summary.connected} connected calendars."

        await message.reply_text(reply + venues_text, parse_mode=ParseMode.MARKDOWN)
    else:
        options_text = "\n".join(f"- {opt}" for opt in result.top_options)
        await message.reply_text(
            f"Couldn't fully agree — here are the top options:\n{options_text}{venues_text}",
            parse_mode=ParseMode.MARKDOWN,
        )

    # The proof this was a real negotiation, not a single hidden API call —
    # sent as a follow-up so the main decision stays the headline message.
    if result.rounds:
        resolve_name = lambda user_id: get_member_name(message.chat_id, user_id)
        transcript = format_transcript(result.rounds, resolve_name)
        if len(transcript) > 3500:
            transcript = format_transcript(result.rounds[-2:], resolve_name) + "\n\n(earlier rounds omitted for length)"
        try:
            await message.reply_text("See how we got here:\n\n" + transcript, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            logging.exception("Failed to send negotiation transcript")
