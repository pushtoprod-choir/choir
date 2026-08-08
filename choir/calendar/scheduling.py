"""Post-convergence scheduling: turns a negotiated decision into a concrete
event, then creates it on every connected participant's own calendar. Reuses
the Anthropic client already built in choir/engine/agent.py rather than
constructing a second one.
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

from choir.calendar.client import create_event, delete_event
from choir.calendar.oauth import get_valid_access_token
from choir.engine.agent import MODEL, client
from choir.schemas import CalendarCreationSummary, EventDetails, UserProfile
from choir.store.calendar_tokens import is_calendar_connected
from choir.store.profiles import delete_calendar_event_records, get_calendar_events

DEFAULT_DURATION_MINUTES = 90

# Same structured-output pattern as choir/engine/agent.py's RESPONSE_SCHEMA —
# a malformed reply here would otherwise be exactly the kind of demo-day
# crash structured outputs are meant to rule out.
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "start_iso": {"type": "string"},
        "duration_minutes": {"type": "integer"},
        "location_text": {"type": "string"},
    },
    "required": ["title", "start_iso", "duration_minutes", "location_text"],
    "additionalProperties": False,
}


def _local_tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("CHOIR_TIMEZONE", "Asia/Kolkata"))


def _local_now_iso() -> str:
    return datetime.now(_local_tz()).isoformat()


def extract_event_details(goal_text: str, decision: str) -> EventDetails | None:
    """One structured Claude call turning a free-text negotiated decision
    (e.g. "Cafe Coffee Day, HSR, 7:30pm") into concrete event fields. The
    "always produce a best guess" fallback lives in the prompt itself — even
    if the decision has no explicit time, the model is told to pick a
    reasonable one rather than leaving it blank. The only failure mode left
    to handle here is the API call or parsing failing outright, in which case
    this returns None and the caller skips event creation entirely rather
    than guessing further in code."""
    system_prompt = f"""Turn a group's negotiated plan into concrete calendar event details.
Current local time: {_local_now_iso()}

Always produce a specific start_iso (ISO 8601, with a timezone offset), even
if the plan text has no explicit time — pick today evening if that's still
reasonable given the current time, otherwise tomorrow evening. Default
duration_minutes to {DEFAULT_DURATION_MINUTES} unless the plan implies
otherwise. location_text should be the venue/place name from the plan, or a
short descriptive phrase if no specific venue is named."""

    user_message = f"The group wanted to: {goal_text}\nWhat they agreed on: {decision}"

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=300,
            system=system_prompt,
            thinking={"type": "disabled"},
            output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
            messages=[{"role": "user", "content": user_message}],
        )
        text = next(block.text for block in response.content if block.type == "text")
        data = json.loads(text)
        # Normalize start_iso to a timezone-aware, canonical-offset string
        # before it goes any further downstream — the model is only
        # prompt-instructed to include a UTC offset, not forced to, and a
        # naive datetime here silently became a naive dateTime sent to
        # Google Calendar with no timeZone field, which different
        # participants' accounts can render at different clock times for
        # the same event.
        parsed = datetime.fromisoformat(data["start_iso"])
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_local_tz())
        else:
            parsed = parsed.astimezone(_local_tz())
        data["start_iso"] = parsed.isoformat()
    except (anthropic.APIError, json.JSONDecodeError, KeyError, ValueError, StopIteration):
        return None

    return EventDetails(
        title=data["title"],
        start_iso=data["start_iso"],
        duration_minutes=data["duration_minutes"],
        location_text=data["location_text"],
    )


def create_events_for_connected(
    profiles: list[UserProfile], goal_text: str, decision: str
) -> CalendarCreationSummary | None:
    """None means "nobody in this group is connected" — the caller does
    nothing in that case, which is what keeps unconnected groups' /choir
    output byte-for-byte identical to before this feature existed."""
    connected = [p for p in profiles if is_calendar_connected(p.telegram_user_id)]
    if not connected:
        return None

    details = extract_event_details(goal_text, decision)
    if details is None:
        return CalendarCreationSummary(connected=len(connected), created=0, extraction_failed=True)

    created = 0
    event_ids: dict[int, str] = {}
    for profile in connected:
        access_token = get_valid_access_token(profile.telegram_user_id)
        if access_token is None:
            continue
        event_id = create_event(access_token, details)
        if event_id:
            created += 1
            event_ids[profile.telegram_user_id] = event_id

    return CalendarCreationSummary(
        connected=len(connected), created=created, extraction_failed=False, event_ids=event_ids
    )


def delete_events_for_negotiation(negotiation_id: int) -> None:
    """Cleans up whatever real calendar events a previous negotiation
    created, before a /choir update creates new ones for the revised
    decision — without this, revising a plan just keeps stacking a fresh
    event on top of the stale one instead of replacing it. Best-effort and
    silent on any failure per-person (expired token, already-deleted event,
    network hiccup): a cleanup miss here is not worth blocking or failing
    the new negotiation's own calendar creation over."""
    event_ids = get_calendar_events(negotiation_id)
    if not event_ids:
        return
    for telegram_user_id, event_id in event_ids.items():
        access_token = get_valid_access_token(telegram_user_id)
        if access_token is not None:
            delete_event(access_token, event_id)
    delete_calendar_event_records(negotiation_id)
