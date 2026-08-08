import asyncio
import dataclasses
import json
import logging
import os
from collections import deque
from urllib.parse import quote

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ContextTypes

from choir.store.profiles import (
    get_profile,
    record_seen_member,
    get_seen_members,
    get_member_name,
    start_negotiation_log,
    record_negotiation_round,
    finish_negotiation_log,
    get_negotiation_history,
)
from choir.schemas import NegotiationRequest, AgentSignal, UserProfile, VenueResult
from choir.engine.orchestrator import format_transcript, run_negotiation
from choir.venues.places import build_venue_query, find_venues
from choir.venues.enrichment import enrich_venues
from choir.bot.intent import is_planning_request
from choir.calendar.client import attach_calendar_availability
from choir.calendar.scheduling import create_events_for_connected

# Chats with a negotiation currently in flight — guards against a second
# /choir stomping on a running one (e.g. someone double-tapping the command).
# Single-threaded event loop, so plain set membership checks are race-free
# as long as we don't await between the check and the add.
_active_negotiations: set[int] = set()

# chat_id -> goal_text awaiting an occasion reply. Asked once per /choir
# trigger before the actual negotiation starts: different reasons for the
# same activity ("it's Priya's birthday" vs. "just a random Tuesday")
# reasonably call for different tradeoffs, and the only way to know which is
# to ask rather than assume.
_pending_occasion: dict[int, str] = {}

# chat_id -> the most recent converged decision, so "/choir update <reason>"
# has something concrete to revise instead of starting blind.
_last_decision: dict[int, str] = {}

_SKIP_WORDS = {"skip", "none", "no", "n/a", "na", ""}

# Telegram's hard cap is 4096 chars; leave headroom below it since Markdown
# entities and the surrounding reply text add to the same budget — this
# margin also covers the "(+N more, trimmed for length)" suffix
# _build_venues_text appends after its own budget check, which isn't itself
# accounted for in that check.
TELEGRAM_MESSAGE_LIMIT = 4096
VENUES_SAFETY_MARGIN = 200

# Telegram long-polling can redeliver the same update — a documented real
# behavior (a dropped ack, a reconnect), not a hypothetical. Without this, a
# redelivered /choir after the original already finished would trigger a
# second full (real-money, real-API-call) negotiation for the same trigger.
# Bounded FIFO, not persisted — matches the in-memory risk level already
# accepted for _active_negotiations/_pending_occasion elsewhere in this file.
_MAX_TRACKED_UPDATE_IDS = 500
_processed_update_ids: set[int] = set()
_processed_update_id_order: deque[int] = deque(maxlen=_MAX_TRACKED_UPDATE_IDS)


def _already_processed(update_id: int | None) -> bool:
    if update_id is None:
        return False
    if update_id in _processed_update_ids:
        return True
    if len(_processed_update_id_order) == _processed_update_id_order.maxlen:
        oldest = _processed_update_id_order.popleft()
        _processed_update_ids.discard(oldest)
    _processed_update_id_order.append(update_id)
    _processed_update_ids.add(update_id)
    return False


def _build_venues_text(venues: list[VenueResult], max_chars: int) -> str:
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
        # Place search resolves to the actual venue; a bare lat/lon query
        # drops you at a generic map pin instead. Query on the full address
        # (which LocationIQ already prefixes with the venue name) rather
        # than just the name, so generic names like "Snack Corner" don't
        # resolve to some other branch across town.
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


async def _describe_decided_venue(venue: VenueResult, goal_text: str) -> str:
    """Formats the map link (+ a best-effort enrichment note) for the venue
    the group actually negotiated over and agreed to — not a separate,
    possibly-mismatched list."""
    from choir.venues.places import detect_purpose

    enriched = await asyncio.to_thread(enrich_venues, [venue], detect_purpose(goal_text))
    venue = enriched[0] if enriched else venue

    text = ""
    if venue.note:
        text += f"\n\n💬 {venue.note}"
    if venue.lat and venue.lon:
        text += f"\n📍 https://www.google.com/maps?q={venue.lat},{venue.lon}"
    return text


async def _fallback_venue_suggestions(profiles: list[UserProfile], goal_text: str, max_chars: int) -> str:
    """Best-effort venue suggestions for when the negotiation itself had no
    real candidates to ground the decision in (LocationIQ unconfigured/down),
    or didn't converge at all — a bonus on top of the result, explicitly not
    guaranteed to match a free-text decision. find_venues() already fails
    soft on lookup problems, and os.environ.get (not bracket access) matters
    here specifically: a missing key must skip venues gracefully, not raise a
    KeyError that kills the reply after a successful negotiation. max_chars
    bounds the rendered block so it can never push the combined message over
    Telegram's limit — see _build_venues_text."""
    location_iq_key = os.environ.get("LOCATION_IQ_API_KEY")
    if not location_iq_key:
        return ""

    venue_query = build_venue_query(profiles, goal_text)
    venues = await asyncio.to_thread(find_venues, venue_query, location_iq_key)
    if not venues:
        return ""
    venues = await asyncio.to_thread(enrich_venues, venues, venue_query.purpose)
    return _build_venues_text(venues, max_chars)


def _gather_profiles(chat_id: int) -> tuple[list[UserProfile], list[int]]:
    member_ids = get_seen_members(chat_id)
    profiles = []
    missing_users = []
    for user_id in member_ids:
        profile = get_profile(user_id)
        if profile is None:
            missing_users.append(user_id)
        else:
            profiles.append(profile)
    return profiles, missing_users


async def track_group_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message is None or message.from_user is None or message.from_user.is_bot:
        return
    record_seen_member(message.chat_id, message.from_user.id, message.from_user.first_name)


async def handle_choir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _already_processed(update.update_id):
        return

    message = update.message

    if message.chat.type == ChatType.PRIVATE:
        await message.reply_text(
            "/choir only works in a group — add me to one and try there.", parse_mode=ParseMode.MARKDOWN
        )
        return

    args = context.args

    if not args:
        await message.reply_text(
            "Tell me what you want to plan — e.g. /choir plan lunch for us\n"
            "Or revise the last plan: /choir update <what changed>",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if message.chat_id in _active_negotiations:
        await message.reply_text(
            "Already negotiating for this group — hang tight for that one to finish.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if message.chat_id in _pending_occasion:
        await message.reply_text(
            "Still waiting on the occasion for the last /choir — answer that first (or reply \"skip\").",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    is_update = args[0].lower() == "update"

    # /choir itself counts as being "seen" in this chat
    record_seen_member(message.chat_id, message.from_user.id, message.from_user.first_name)

    profiles, missing_users = _gather_profiles(message.chat_id)

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

    if is_update:
        previous = _last_decision.get(message.chat_id)
        if previous is None:
            # In-memory state doesn't survive a restart — the persisted log
            # does. Falls back to it so "/choir update" still works after
            # the bot process restarts, not just within one continuous run.
            history = get_negotiation_history(message.chat_id, limit=5)
            previous = next((h["decision"] for h in history if h["converged"] and h["decision"]), None)
        if previous is None:
            await message.reply_text(
                "There's no previous plan for this group to update yet — use /choir <what you want> to start one.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        reason = " ".join(args[1:]).strip()
        if not reason:
            await message.reply_text(
                "Tell me why it needs to change — e.g. /choir update someone's running late",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        # Seeded with the old decision so the agents revise instead of
        # re-negotiating from scratch — an update skips both the occasion
        # question (the reason for changing already provides that context)
        # and intent-gating below (an explicit revision command doesn't need
        # classifying, and a reason like "someone's running late" would very
        # plausibly fail a "is this a planning request" check on its own).
        goal_text = (
            f"We previously agreed on: {previous}. That needs to change because: {reason}. "
            f"Come up with an updated plan that addresses this."
        )
        await _run_negotiation(message, goal_text)
        return

    goal_text = " ".join(args)

    # Cheap gate so "/choir what's up" doesn't spin up a full negotiation —
    # only actually negotiate when this reads like a genuine planning ask.
    # Runs after the profile checks above (not before) so a group that
    # hasn't onboarded yet gets the onboarding prompt without spending an
    # extra API call on intent classification first.
    if not await asyncio.to_thread(is_planning_request, goal_text):
        await message.reply_text(
            "I'm here to help plan outings — try something like `/choir plan lunch for us` 🙂",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    _pending_occasion[message.chat_id] = goal_text
    await message.reply_text(
        "Quick one before I start — what's the occasion? "
        "(e.g. birthday, casual hangout, work catch-up — or reply \"skip\")",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_occasion_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the plain-text reply to the "what's the occasion?" question.
    A no-op for every message except the one specific reply a chat is
    actually waiting on — everything else falls through to track_group_member
    (registered separately) exactly as before."""
    if _already_processed(update.update_id):
        return

    message = update.message
    if message is None or message.from_user is None or message.from_user.is_bot or not message.text:
        return

    chat_id = message.chat_id
    if chat_id not in _pending_occasion:
        return

    goal_text = _pending_occasion.pop(chat_id)
    occasion = message.text.strip()
    if occasion.lower() not in _SKIP_WORDS:
        goal_text = f"{goal_text} (occasion: {occasion})"

    await _run_negotiation(message, goal_text)


async def _run_negotiation(message, goal_text: str):
    # Profiles/membership could in principle change in the gap between the
    # /choir trigger and the occasion reply — recheck here rather than trust
    # state gathered earlier in a different handler invocation.
    profiles, missing_users = _gather_profiles(message.chat_id)
    if missing_users or not profiles:
        await message.reply_text(
            "Something changed before I could start — try /choir again.",
            parse_mode=ParseMode.MARKDOWN,
        )
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

    # Append-only audit log — written incrementally per round (not just at
    # the end) so a crash mid-negotiation still leaves a real, inspectable
    # record instead of total silent loss. This is an audit trail, not a
    # resume mechanism: it doesn't make an interrupted negotiation continue
    # from where it left off, only makes it recoverable to look at afterward.
    negotiation_id = start_negotiation_log(message.chat_id, goal_text)

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
        record_negotiation_round(
            negotiation_id, round_num, json.dumps([dataclasses.asdict(s) for s in signals])
        )
        asyncio.run_coroutine_threadsafe(on_round(round_num, signals), loop)

    _active_negotiations.add(message.chat_id)
    try:
        result = await asyncio.to_thread(run_negotiation, request, sync_on_round)
    except NotImplementedError:
        finish_negotiation_log(negotiation_id, converged=False, decision=None)
        await message.reply_text(
            "The negotiation engine isn't wired up yet — check back once it's built.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    finally:
        _active_negotiations.discard(message.chat_id)

    finish_negotiation_log(negotiation_id, converged=result.converged, decision=result.decision)

    if result.converged:
        # Remembered so a later "/choir update <reason>" has something to revise.
        _last_decision[message.chat_id] = result.decision
        reply = f"🎉 *Decision:* {result.decision}\n\n{result.explanation}"
        if result.tradeoffs:
            reply += "\n\n*Why:*\n" + "\n".join(f"• {t}" for t in result.tradeoffs)

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

        if result.decided_venue:
            # The decision IS a real, negotiated-over venue already (the
            # orchestrator constrained proposals to real LocationIQ
            # candidates and verified the distance itself) — enrich just
            # this one venue for a note instead of running a second,
            # disconnected venue search that might not even match. A single
            # venue's note is short by design (see enrichment.py's prompt),
            # so no length budget is computed here — the try/except below
            # still guards against the combined message overflowing anyway.
            extra = await _describe_decided_venue(result.decided_venue, goal_text)
        else:
            # Venues weren't available during negotiation (LocationIQ
            # unconfigured/down) — the decision is free text. Best-effort
            # fallback suggestions, clearly not guaranteed to match it.
            # Computed after the calendar summary is appended, so the
            # budget accounts for that line too.
            budget = TELEGRAM_MESSAGE_LIMIT - VENUES_SAFETY_MARGIN - len(reply)
            extra = await _fallback_venue_suggestions(profiles, goal_text, budget)

        try:
            await message.reply_text(reply + extra, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            logging.exception("Failed to send decision message; retrying without venues")
            await message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
    else:
        options_text = "\n".join(f"• {opt}" for opt in result.top_options)
        base = f"🤔 *Couldn't fully agree — here are the top options:*\n{options_text}"
        budget = TELEGRAM_MESSAGE_LIMIT - VENUES_SAFETY_MARGIN - len(base)
        fallback = await _fallback_venue_suggestions(profiles, goal_text, budget)
        try:
            await message.reply_text(base + fallback, parse_mode=ParseMode.MARKDOWN)
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
