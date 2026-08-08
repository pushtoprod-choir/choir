"""Post-convergence scheduling: turns the negotiated decision into a concrete
event, then creates it on every connected participant's own calendar.

start_iso is built deterministically from NegotiationRequest.plan_date (fixed
by the user before negotiation ever starts) and NegotiationResult.decided_time
(the time the agents actually converged on — see choir/engine/orchestrator.py)
rather than guessed from free text after the fact: the negotiation contract
guarantees both are present on every convergence, so there's nothing left to
extract or infer here, only combine.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from choir.calendar.client import create_event
from choir.calendar.oauth import get_valid_access_token
from choir.schemas import CalendarCreationSummary, EventDetails, UserProfile, VenueResult
from choir.store.calendar_tokens import is_calendar_connected

DEFAULT_DURATION_MINUTES = 90


def _local_tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("CHOIR_TIMEZONE", "Asia/Kolkata"))


def build_event_details(
    decision: str, plan_date: str, decided_time: str, decided_venue: VenueResult | None
) -> EventDetails | None:
    """Combines the fixed plan_date with the agents' decided_time into a
    timezone-aware start_iso — no LLM call, unlike the free-text extraction
    this replaces. Returns None only if the two strings can't be combined
    into a real datetime, which the negotiation contract shouldn't ever
    produce; guarded rather than trusted blindly so a malformed value fails
    into "couldn't schedule" instead of a bad calendar entry."""
    try:
        naive = datetime.strptime(f"{plan_date} {decided_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    location_text = decided_venue.name if decided_venue else decision
    return EventDetails(
        title=decision,
        start_iso=naive.replace(tzinfo=_local_tz()).isoformat(),
        duration_minutes=DEFAULT_DURATION_MINUTES,
        location_text=location_text,
    )


def create_events_for_connected(
    profiles: list[UserProfile],
    decision: str,
    plan_date: str,
    decided_time: str,
    decided_venue: VenueResult | None,
) -> CalendarCreationSummary | None:
    """None means "nobody in this group is connected" — the caller does
    nothing in that case, which is what keeps unconnected groups' /choir
    output byte-for-byte identical to before this feature existed."""
    connected = [p for p in profiles if is_calendar_connected(p.telegram_user_id)]
    if not connected:
        return None

    details = build_event_details(decision, plan_date, decided_time, decided_venue)
    if details is None:
        return CalendarCreationSummary(connected=len(connected), created=0, build_failed=True)

    created = 0
    for profile in connected:
        access_token = get_valid_access_token(profile.telegram_user_id)
        if access_token is not None and create_event(access_token, details):
            created += 1

    return CalendarCreationSummary(connected=len(connected), created=created, build_failed=False)
