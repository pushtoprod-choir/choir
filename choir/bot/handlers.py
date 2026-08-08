import asyncio
import dataclasses
import json
import logging
import os
from collections import deque
from datetime import datetime
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
    start_trip,
    get_active_trip,
    end_trip,
    get_trip_negotiations,
    update_preference_confidence,
    record_calendar_events,
)
from choir.schemas import NegotiationRequest, AgentSignal, UserProfile, VenueResult
from choir.engine.agent import get_missing_info_question
from choir.engine.orchestrator import format_transcript, run_negotiation
from choir.venues.places import build_venue_query, find_venues, haversine_distance_km
from choir.venues.enrichment import enrich_venues
from choir.actions.links import build_ride_deeplink, describe_ride_suggestion, find_carpool_pairs
from choir.bot.intent import is_planning_request
from choir.bot.date_extraction import extract_plan_date
from choir.calendar.client import attach_calendar_availability
from choir.calendar.scheduling import create_events_for_connected, delete_events_for_negotiation

# Chats with a negotiation currently in flight — guards against a second
# /choir stomping on a running one (e.g. someone double-tapping the command).
# Single-threaded event loop, so plain set membership checks are race-free
# as long as we don't await between the check and the add.
_active_negotiations: set[int] = set()


@dataclasses.dataclass
class _PendingPlan:
    goal_text: str
    plan_date: str   # "YYYY-MM-DD", already extracted and validated before the occasion question is asked


# chat_id -> plan awaiting an occasion reply. Asked once per /choir plan
# trigger before the actual negotiation starts: different reasons for the
# same activity ("it's Priya's birthday" vs. "just a random Tuesday")
# reasonably call for different tradeoffs, and the only way to know which is
# to ask rather than assume.
_pending_occasion: dict[int, _PendingPlan] = {}

# chat_id -> the most recent converged decision, so "/choir update <reason>"
# has something concrete to revise instead of starting blind.
_last_decision: dict[int, str] = {}

# chat_id -> that decision's negotiation_id, so a later "/choir update" can
# find and delete exactly the calendar events created for it before creating
# new ones for the revised plan — without this a revised plan just stacks a
# duplicate event on top of the stale one instead of replacing it.
_last_negotiation_id: dict[int, int] = {}

# chat_id -> that decision's plan_date, carried forward unchanged by a later
# "/choir update <reason>" unless the reason itself names a new date — most
# revisions (a venue swap, someone running late) don't touch the date at all.
_last_plan_date: dict[int, str] = {}

# chat_id -> that decision's decided_time. A later "/choir update" seeds the
# new negotiation's starting proposal+time with BOTH of these instead of
# discarding the time and starting the whole thing blank — without this, an
# update that only affects one person (e.g. "Pragathi can't make it") reset
# every OTHER person's negotiation to a blank slate too, which is what let
# agents with no real conflict invent one: nothing on the table meant nothing
# to just re-confirm, so every agent had to manufacture a fresh position.
_last_decided_time: dict[int, str] = {}

_SKIP_WORDS = {"skip", "none", "no", "n/a", "na", ""}

# user_id -> (chat_id, future) for an outstanding dynamic-question DM. Keyed
# by user_id (not chat_id) since this is a private DM, not a group flow, and
# is the first per-user (rather than per-chat) pending state in this file —
# see _gather_dynamic_context for how a collision (same person flagged by two
# concurrent negotiations) is handled.
_pending_clarifications: dict[int, tuple[int, "asyncio.Future"]] = {}

# Long enough to glance at a phone and reply, short enough that the group
# doesn't feel like /choir hung. This is optional enrichment, not a hard
# gate — a timeout just means proceeding without that person's answer.
_CLARIFICATION_TIMEOUT_SECONDS = 45

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

    enriched = await asyncio.to_thread(
        enrich_venues, [venue], detect_purpose(goal_text)
    )
    venue = enriched[0] if enriched else venue

    text = ""
    if venue.note:
        text += f"\n\n💬 {venue.note}"
    if venue.lat and venue.lon:
        text += f"\n📍 https://www.google.com/maps?q={venue.lat},{venue.lon}"
    return text


async def _fallback_venue_suggestions(
    profiles: list[UserProfile], goal_text: str, max_chars: int
) -> str:
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


def _format_plan_datetime(plan_date: str, decided_time: str) -> str:
    """Human-readable rendering of the fixed plan_date plus the time the
    agents actually converged on, e.g. "Saturday, August 15, 2026 at 7:30 PM"
    — for the "Decision:" reply, not for anything downstream (the calendar
    event uses the raw ISO values directly, see choir/calendar/scheduling.py)."""
    date_part = datetime.strptime(plan_date, "%Y-%m-%d").strftime("%A, %B %d, %Y")
    time_part = datetime.strptime(decided_time, "%H:%M").strftime("%I:%M %p").lstrip("0")
    return f"{date_part} at {time_part}"


async def _send_ride_suggestions(
    chat_id: int,
    profiles: list[UserProfile],
    venue: VenueResult,
    bot,
    area_coords: dict[str, tuple[float, float] | None],
) -> None:
    """Best-effort, DM-only ride-suggestion stub — not a real booking, just a
    pre-filled Uber deep link plus a distance-based carpool nudge, sent
    privately (pickup area is personal, same reasoning as budget). Skips
    entirely if the venue has no real coordinates; a failed DM to one person
    never blocks another's.

    area_coords is reused from the negotiation's own geocoding pass
    (NegotiationResult.area_coords, populated by orchestrator._fetch_venue_context)
    rather than re-geocoded here — a second independent LocationIQ burst
    moments after the first risked hitting a rate limit or transient failure
    for whoever's area came up in it, silently giving that person no ride
    suggestion at all with nothing logged anywhere to explain why."""
    if not (venue.lat and venue.lon):
        return
    if not area_coords:
        logging.warning("No area_coords available for ride suggestions: chat=%s", chat_id)
        return

    dropoff = (venue.lat, venue.lon)

    user_coords: dict[int, tuple[float, float]] = {}
    for profile in profiles:
        pickup = area_coords.get(profile.area)
        if pickup is None:
            logging.warning(
                "No geocoded coordinate for %r; skipping ride suggestion for user %s",
                profile.area, profile.telegram_user_id,
            )
            continue
        user_coords[profile.telegram_user_id] = pickup

        distance = haversine_distance_km(pickup, dropoff)
        text = describe_ride_suggestion(distance, build_ride_deeplink(pickup, dropoff))
        if not text:
            logging.info(
                "Ride suggestion skipped (within walk distance): user=%s distance=%.2fkm",
                profile.telegram_user_id, distance,
            )
            continue
        try:
            await bot.send_message(chat_id=profile.telegram_user_id, text=text)
        except Exception:
            logging.exception("Failed to DM ride suggestion to user %s", profile.telegram_user_id)

    for user_a, user_b, _distance in find_carpool_pairs(user_coords):
        name_a = get_member_name(chat_id, user_a)
        name_b = get_member_name(chat_id, user_b)
        for this_user, other_name in ((user_a, name_b), (user_b, name_a)):
            try:
                await bot.send_message(
                    chat_id=this_user,
                    text=f"🚗 You and {other_name} are both nearby — might be worth sharing a ride tonight.",
                )
            except Exception:
                logging.exception("Failed to DM carpool suggestion to user %s", this_user)


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


async def _gather_dynamic_context(message, profiles: list[UserProfile], goal_text: str) -> None:
    """Pre-round-1 dynamic-questions phase (MVP: pre-round only, no mid-round
    follow-ups, no changes to orchestrator.py's round loop or the
    ACCEPT/REJECT/COUNTER contract). For each profile, asks its agent whether
    this specific request is missing something onboarding didn't cover; DMs
    whoever needs one, waits (bounded by _CLARIFICATION_TIMEOUT_SECONDS) for
    replies, and folds answers into that profile's temporary_context IN
    PLACE — same runtime-only, never-persisted pattern as
    attach_calendar_availability populating calendar_busy_text. Best-effort:
    a failed DM, a skip, or a timeout all just mean proceeding without that
    person's answer, never blocking or crashing the negotiation."""
    questions = await asyncio.gather(
        *(asyncio.to_thread(get_missing_info_question, p, goal_text) for p in profiles)
    )
    flagged = [(p, q) for p, q in zip(profiles, questions) if q]
    if not flagged:
        return

    loop = asyncio.get_event_loop()
    bot = message.get_bot()
    futures: dict[int, "asyncio.Future"] = {}

    for profile, question in flagged:
        user_id = profile.telegram_user_id
        if user_id in _pending_clarifications:
            # Already waiting on a clarification for this same person from a
            # different concurrent negotiation (a different group) — fail
            # open rather than clobber the other one's pending state or ask
            # this person two questions from two bots-in-their-DMs at once.
            continue
        future = loop.create_future()
        _pending_clarifications[user_id] = (message.chat_id, future)
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"Quick one before I negotiate on your behalf: {question}\n"
                '(or reply "skip")',
            )
            futures[user_id] = future
        except Exception:
            logging.exception("Failed to DM clarifying question to user %s", user_id)
            _pending_clarifications.pop(user_id, None)

    if not futures:
        return

    done, _pending = await asyncio.wait(
        futures.values(), timeout=_CLARIFICATION_TIMEOUT_SECONDS
    )

    for user_id, future in futures.items():
        _pending_clarifications.pop(user_id, None)
        if future in done:
            answer = future.result()
            if answer:
                for profile in profiles:
                    if profile.telegram_user_id == user_id:
                        profile.temporary_context = answer
                        break
        else:
            future.cancel()


async def handle_clarification_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the plain-text DM reply to a dynamic clarifying question. A
    no-op for every message except one from a user with an outstanding
    clarification — mirrors handle_occasion_reply's shape, but keyed by
    user_id (a DM) instead of chat_id (a group)."""
    message = update.message
    if (
        message is None
        or message.from_user is None
        or message.from_user.is_bot
        or not message.text
    ):
        return

    user_id = message.from_user.id
    pending = _pending_clarifications.get(user_id)
    if pending is None:
        return

    _, future = pending
    if future.done():
        return

    answer = message.text.strip()
    future.set_result(None if answer.lower() in _SKIP_WORDS else answer)
    await message.reply_text("Got it, thanks!")


async def track_group_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message is None or message.from_user is None or message.from_user.is_bot:
        return
    record_seen_member(
        message.chat_id, message.from_user.id, message.from_user.first_name
    )


async def handle_choir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _already_processed(update.update_id):
        return

    message = update.message

    if message.chat.type == ChatType.PRIVATE:
        await message.reply_text(
            "/choir only works in a group — add me to one and try there.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    args = context.args

    if not args:
        await message.reply_text(
            "Here's what I understand:\n"
            "/choir start trip <title> — kick off a new trip\n"
            "/choir plan <what you want> — negotiate something within the current trip\n"
            "/choir update <what changed> — revise the last decision\n"
            "/choir end trip — wrap up the current trip",
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
            'Still waiting on the occasion for the last /choir — answer that first (or reply "skip").',
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    command = args[0].lower()
    is_start_trip = command == "start" and len(args) >= 2 and args[1].lower() == "trip"
    is_end_trip = command == "end" and len(args) >= 2 and args[1].lower() == "trip"
    is_plan = command == "plan"
    is_update = command == "update"

    # Only these four forms are recognized — anything else (including the
    # old bare "/choir <freeform goal>" form) is rejected outright rather
    # than guessed at.
    if not (is_start_trip or is_end_trip or is_plan or is_update):
        await message.reply_text(
            "I only understand:\n"
            "/choir start trip <title>\n"
            "/choir plan <what you want>\n"
            "/choir update <what changed>\n"
            "/choir end trip",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if is_start_trip:
        if get_active_trip(message.chat_id) is not None:
            await message.reply_text(
                "A trip's already in progress for this group — /choir end trip first.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        title = " ".join(args[2:]).strip() or None
        start_trip(message.chat_id, message.from_user.id, title)
        label = f' "{title}"' if title else ""
        await message.reply_text(
            f"🧳 Trip started{label}. Every /choir plan from here gets tracked as part of it "
            "— wrap up with /choir end trip when you're done.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if is_end_trip:
        active_trip = get_active_trip(message.chat_id)
        if active_trip is None:
            await message.reply_text(
                "No trip in progress for this group.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        trip_negotiations = get_trip_negotiations(active_trip["id"])
        decided = [n for n in trip_negotiations if n["converged"] and n["decision"]]
        summary = (
            "\n".join(
                f"• {n['goal_text']}: {n['decision']}"
                + (f" on {n['plan_date']} at {n['decided_time']}" if n["plan_date"] and n["decided_time"] else "")
                for n in decided
            )
            if decided
            else "No decisions were finalized during this trip."
        )
        end_trip(active_trip["id"], summary)
        title = active_trip["title"]
        label = f' "{title}"' if title else ""
        await message.reply_text(
            f"🏁 Trip{label} ended.\n\n*Summary:*\n{summary}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # "plan" only makes sense inside a trip — it's the container decisions
    # get tagged against and rolled up into when the trip ends.
    if get_active_trip(message.chat_id) is None:
        await message.reply_text(
            "Start a trip first — /choir start trip <title> — before planning.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # /choir itself counts as being "seen" in this chat
    record_seen_member(
        message.chat_id, message.from_user.id, message.from_user.first_name
    )

    profiles, missing_users = _gather_profiles(message.chat_id)

    if missing_users:
        bot_username = context.bot.username.replace("_", "\\_")
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
        await message.reply_text(
            "Couldn't find anyone set up in this group yet.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if is_update:
        previous = _last_decision.get(message.chat_id)
        previous_negotiation_id = _last_negotiation_id.get(message.chat_id)
        previous_plan_date = _last_plan_date.get(message.chat_id)
        previous_decided_time = _last_decided_time.get(message.chat_id)
        if previous is None:
            # In-memory state doesn't survive a restart — the persisted log
            # does. Falls back to it so "/choir update" still works after
            # the bot process restarts, not just within one continuous run.
            history = get_negotiation_history(message.chat_id, limit=5)
            match = next(
                (h for h in history if h["converged"] and h["decision"]),
                None,
            )
            if match:
                previous = match["decision"]
                previous_negotiation_id = match["id"]
                previous_plan_date = match["plan_date"]
                previous_decided_time = match["decided_time"]
        if previous is None:
            await message.reply_text(
                "There's no previous plan for this group to update yet — use /choir plan <what you want> to start one.",
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
        # The revision reason might itself name a new date (e.g. "let's push
        # it to next Saturday instead") — try that first, otherwise carry the
        # original plan's date forward unchanged, since most revisions (a
        # venue swap, someone running late) don't touch the date at all.
        plan_date = await asyncio.to_thread(extract_plan_date, reason) or previous_plan_date
        if plan_date is None:
            await message.reply_text(
                "What date is this for? Include one and try again — e.g. "
                "/choir update moved to Sunday instead",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await _run_negotiation(
            message, goal_text, plan_date, skip_dynamic_questions=True,
            replaces_negotiation_id=previous_negotiation_id,
            # Seeds round 0 with the PREVIOUS decision (venue + time) already
            # on the table, instead of starting blank — someone whose
            # situation didn't change just re-confirms it; only whoever the
            # revision reason actually concerns has a real reason to counter.
            initial_proposal=previous,
            initial_time=previous_decided_time,
        )
        return

    goal_text = " ".join(args[1:]).strip()
    if not goal_text:
        await message.reply_text(
            "Tell me what you want to plan — e.g. /choir plan lunch for us",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Cheap gate so "/choir plan what's up" doesn't spin up a full negotiation —
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

    # The date is fixed by this command and never negotiated — only the time
    # is left for the agents to decide (see choir/engine). No date mentioned
    # here means the command fails outright rather than guessing one.
    plan_date = await asyncio.to_thread(extract_plan_date, goal_text)
    if plan_date is None:
        await message.reply_text(
            "What date is this for? Include one and try again — e.g. "
            "/choir plan dinner this Saturday",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    _pending_occasion[message.chat_id] = _PendingPlan(goal_text=goal_text, plan_date=plan_date)
    await message.reply_text(
        "Quick one before I start — what's the occasion? "
        '(e.g. birthday, casual hangout, work catch-up — or reply "skip")',
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
    if (
        message is None
        or message.from_user is None
        or message.from_user.is_bot
        or not message.text
    ):
        return

    chat_id = message.chat_id
    if chat_id not in _pending_occasion:
        return

    pending = _pending_occasion.pop(chat_id)
    goal_text = pending.goal_text
    occasion = message.text.strip()
    if occasion.lower() not in _SKIP_WORDS:
        goal_text = f"{goal_text} (occasion: {occasion})"

    await _run_negotiation(message, goal_text, pending.plan_date)


async def _run_negotiation(
    message, goal_text: str, plan_date: str, skip_dynamic_questions: bool = False,
    replaces_negotiation_id: int | None = None,
    initial_proposal: str | None = None,
    initial_time: str | None = None,
):
    """replaces_negotiation_id, when set (only by the /choir update path),
    is the negotiation whose calendar events (if any) should be deleted once
    this one produces a new decision — see the calendar-replace block below.
    A plain new /choir plan on an unrelated topic leaves this None, since
    that's a separate plan, not a revision of an existing one.

    initial_proposal/initial_time (also only set by /choir update) seed round
    0 with the previous negotiation's actual decision already on the table,
    instead of starting from a blank slate — see run_negotiation's docstring
    in orchestrator.py for why this matters."""
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

    # skip_dynamic_questions=True for "/choir update <reason>": an explicit
    # revision command doesn't need re-clarifying — the reason for changing
    # already provides fresh context, same rationale as skipping the
    # occasion question and intent-gating on that path.
    if not skip_dynamic_questions:
        await _gather_dynamic_context(message, profiles, goal_text)

    request = NegotiationRequest(
        group_chat_id=message.chat_id,
        goal_text=goal_text,
        profiles=profiles,
        plan_date=plan_date,
    )

    # Append-only audit log — written incrementally per round (not just at
    # the end) so a crash mid-negotiation still leaves a real, inspectable
    # record instead of total silent loss. This is an audit trail, not a
    # resume mechanism: it doesn't make an interrupted negotiation continue
    # from where it left off, only makes it recoverable to look at afterward.
    # Auto-tagged to the chat's active trip (if any) so /choir end trip can
    # later roll up every decision made while it was open.
    active_trip = get_active_trip(message.chat_id)
    trip_id = active_trip["id"] if active_trip else None
    negotiation_id = start_negotiation_log(message.chat_id, goal_text, trip_id=trip_id, plan_date=plan_date)

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
            negotiation_id,
            round_num,
            json.dumps([dataclasses.asdict(s) for s in signals]),
        )
        asyncio.run_coroutine_threadsafe(on_round(round_num, signals), loop)

    _active_negotiations.add(message.chat_id)
    try:
        result = await asyncio.to_thread(
            run_negotiation, request, sync_on_round, initial_proposal, initial_time
        )
    except NotImplementedError:
        finish_negotiation_log(negotiation_id, converged=False, decision=None)
        await message.reply_text(
            "The negotiation engine isn't wired up yet — check back once it's built.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    finally:
        _active_negotiations.discard(message.chat_id)

    finish_negotiation_log(
        negotiation_id, converged=result.converged, decision=result.decision, decided_time=result.decided_time
    )

    # Nudges each person's own preference confidence based on THEIR final-round
    # stance (not the group's overall convergence) — someone who was still
    # countering when the round budget ran out gets a real negative signal
    # even if two other people happened to accept something. Runs every time,
    # converged or not: a non-convergent negotiation is exactly the kind of
    # outcome that should count against whatever this person was holding out
    # for. See choir.store.profiles.update_preference_confidence for why this
    # is a coarse, un-attributed signal rather than per-tag classification.
    if result.rounds:
        final_stances = {s.user_id: s.stance for s in result.rounds[-1]}
        for profile in profiles:
            stance = final_stances.get(profile.telegram_user_id)
            if stance is not None:
                await asyncio.to_thread(
                    update_preference_confidence, profile.telegram_user_id, stance == "ACCEPT"
                )

    if result.converged:
        # Remembered so a later "/choir update <reason>" has something to
        # revise, knows which negotiation's calendar events to replace, and
        # (absent a new date in the revision reason) which date to carry
        # forward unchanged.
        _last_decision[message.chat_id] = result.decision
        _last_negotiation_id[message.chat_id] = negotiation_id
        _last_plan_date[message.chat_id] = plan_date
        if result.decided_time:
            _last_decided_time[message.chat_id] = result.decided_time
        reply = (
            f"🎉 *Decision:* {result.decision}\n"
            f"📅 {_format_plan_datetime(plan_date, result.decided_time)}\n\n"
            f"{result.explanation}"
        )
        if result.tradeoffs:
            reply += "\n\n*Why:*\n" + "\n".join(f"• {t}" for t in result.tradeoffs)

        # A /choir update revising an earlier decision: clean up whatever
        # calendar events that earlier negotiation created BEFORE creating
        # new ones for this one, so revising a plan replaces its calendar
        # event instead of stacking a duplicate on top of the stale one.
        # Best-effort — a cleanup miss here must never block the new
        # negotiation's own calendar creation below.
        if replaces_negotiation_id is not None:
            await asyncio.to_thread(delete_events_for_negotiation, replaces_negotiation_id)

        # None means nobody in this group is calendar-connected — a true
        # no-op, keeping this line absent entirely for unconnected groups.
        # The event is created at exactly plan_date + result.decided_time —
        # both guaranteed present on convergence, no extraction guess needed.
        calendar_summary = await asyncio.to_thread(
            create_events_for_connected,
            profiles, result.decision, plan_date, result.decided_time, result.decided_venue,
        )
        if calendar_summary is not None:
            if calendar_summary.build_failed:
                reply += "\n\n📅 Couldn't auto-schedule this — add it to your calendar manually."
            else:
                reply += f"\n\n📅 Added to {calendar_summary.created}/{calendar_summary.connected} connected calendars."
            if calendar_summary.event_ids:
                # Tied to THIS negotiation_id, not replaces_negotiation_id —
                # so the next /choir update (if any) cleans up from here.
                await asyncio.to_thread(record_calendar_events, negotiation_id, calendar_summary.event_ids)

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
            logging.exception(
                "Failed to send decision message; retrying without venues"
            )
            await message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

        if result.decided_venue:
            # Trailing, best-effort step — runs after the main decision
            # message so a failure here can never affect or delay it. Reuses
            # the SAME geocoded coordinates the negotiation already computed
            # (result.area_coords) instead of re-geocoding every area again
            # moments later — a second independent LocationIQ burst right
            # after the first risked a rate-limited/failed lookup for
            # whoever's area happened to hit it, silently skipping their ride
            # suggestion with no error surfaced anywhere.
            await _send_ride_suggestions(
                message.chat_id, profiles, result.decided_venue, message.get_bot(), result.area_coords
            )
    else:
        options_text = "\n".join(f"• {opt}" for opt in result.top_options)
        base = f"🤔 *Couldn't fully agree — here are the top options:*\n{options_text}"
        budget = TELEGRAM_MESSAGE_LIMIT - VENUES_SAFETY_MARGIN - len(base)
        fallback = await _fallback_venue_suggestions(profiles, goal_text, budget)
        try:
            await message.reply_text(base + fallback, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            logging.exception(
                "Failed to send top-options message; retrying without venues"
            )
            await message.reply_text(base, parse_mode=ParseMode.MARKDOWN)

    # The proof this was a real negotiation, not a single hidden API call —
    # sent as a follow-up so the main decision stays the headline message.
    if result.rounds:
        resolve_name = lambda user_id: get_member_name(message.chat_id, user_id)
        transcript = format_transcript(result.rounds, resolve_name)
        if len(transcript) > 3500:
            transcript = (
                format_transcript(result.rounds[-2:], resolve_name)
                + "\n\n(earlier rounds omitted for length)"
            )
        try:
            await message.reply_text(
                f"*📜 See how we got here:*\n\n{transcript}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            logging.exception("Failed to send negotiation transcript")
