"""Calendar read/write operations for an already-connected person. Hits the
Calendar REST API directly with `requests` — same approach as
choir/venues/places.py takes with LocationIQ, no Google API SDK. Fails soft
(returns None / False) on any lookup or write problem: calendar awareness is
a bonus on top of the negotiation, never something worth crashing /choir over.
"""
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from choir.calendar.oauth import get_valid_access_token
from choir.schemas import EventDetails, UserProfile

EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
BUSY_WINDOW_HOURS = 48
MAX_EVENTS_IN_SUMMARY = 5
REQUEST_TIMEOUT_SECONDS = 10


def _local_tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("CHOIR_TIMEZONE", "Asia/Kolkata"))


def _format_event(item: dict, tz: ZoneInfo) -> str:
    summary = item.get("summary") or "busy"
    start_raw = item.get("start", {}).get("dateTime")
    end_raw = item.get("end", {}).get("dateTime")
    if not start_raw or not end_raw:
        return f"{summary} (all day)"
    start = datetime.fromisoformat(start_raw).astimezone(tz)
    end = datetime.fromisoformat(end_raw).astimezone(tz)
    return f"{summary} ({start.strftime('%b %d, %I:%M %p')}-{end.strftime('%I:%M %p')})"


def fetch_busy_summary(telegram_user_id: int) -> str | None:
    """One short line describing this person's real calendar events in the
    next 48h. None if they're not connected, have nothing on, or the lookup
    fails for any reason — that None is read by the caller as "no
    availability data," identical in effect to never having connected."""
    access_token = get_valid_access_token(telegram_user_id)
    if access_token is None:
        return None

    now = datetime.now(timezone.utc)
    try:
        response = requests.get(
            EVENTS_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "timeMin": now.isoformat(),
                "timeMax": (now + timedelta(hours=BUSY_WINDOW_HOURS)).isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "fields": "items(summary,start,end)",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        items = response.json().get("items", [])
    except requests.RequestException:
        return None

    if not items:
        return None

    tz = _local_tz()
    return "; ".join(_format_event(item, tz) for item in items[:MAX_EVENTS_IN_SUMMARY])


def attach_calendar_availability(profiles: list[UserProfile]) -> None:
    """Mutates each profile's calendar_busy_text in place — populated only
    for connected people; left as the default None ("not connected") for
    everyone else, which is what keeps unconnected groups unaffected."""
    for profile in profiles:
        profile.calendar_busy_text = fetch_busy_summary(profile.telegram_user_id)


def create_event(access_token: str, details: EventDetails) -> bool:
    start = datetime.fromisoformat(details.start_iso)
    if start.tzinfo is None:
        start = start.replace(tzinfo=_local_tz())
    end = start + timedelta(minutes=details.duration_minutes)
    tz_name = os.environ.get("CHOIR_TIMEZONE", "Asia/Kolkata")
    body = {
        "summary": details.title,
        "location": details.location_text,
        # Explicit timeZone alongside dateTime so Google Calendar can't
        # interpret the same instant differently across participants'
        # accounts — relying on the offset embedded in dateTime alone was
        # what caused two people to see different clock times for one event.
        "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
    }
    try:
        response = requests.post(
            EVENTS_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            json=body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return True
    except requests.RequestException:
        return False
