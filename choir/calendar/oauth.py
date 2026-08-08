"""OAuth mechanics for Google Calendar. No Telegram imports — kept separate
so this module only knows about Google, not the bot. Hits Google's OAuth
endpoints directly with `requests`, the same "no SDK" approach
choir/venues/places.py already takes with LocationIQ.
"""
import os
import secrets
import time
from urllib.parse import urlencode

import requests

from choir.store.calendar_tokens import get_calendar_tokens, save_calendar_tokens

AUTH_BASE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
STATE_TTL_SECONDS = 600
TOKEN_EXPIRY_BUFFER_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 10

# state -> (telegram_user_id, created_at). In-memory only: a mid-flow process
# restart just means the user re-runs /connect_calendar — an acceptable
# hackathon-day failure mode, much simpler than persisting short-lived state.
_pending_states: dict[str, tuple[int, float]] = {}


def generate_auth_url(telegram_user_id: int) -> str:
    state = secrets.token_urlsafe(24)
    _pending_states[state] = (telegram_user_id, time.time())
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": os.environ["GOOGLE_OAUTH_REDIRECT_URI"],
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # Without prompt=consent, Google only returns a refresh_token on a
        # user's very first-ever consent — silently breaking reconnection.
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_BASE_URL}?{urlencode(params)}"


def pop_pending_user(state: str) -> int | None:
    """Pop-once (prevents replay) and TTL-checked (prevents a stale link
    being used later). Returns None for anything that doesn't map to a
    currently-pending connection attempt."""
    entry = _pending_states.pop(state, None)
    if entry is None:
        return None
    telegram_user_id, created_at = entry
    if time.time() - created_at > STATE_TTL_SECONDS:
        return None
    return telegram_user_id


def exchange_code(code: str) -> dict | None:
    try:
        response = requests.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "redirect_uri": os.environ["GOOGLE_OAUTH_REDIRECT_URI"],
                "grant_type": "authorization_code",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def refresh_access_token(refresh_token: str) -> dict | None:
    try:
        response = requests.post(
            TOKEN_URL,
            data={
                "refresh_token": refresh_token,
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "grant_type": "refresh_token",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def get_valid_access_token(telegram_user_id: int) -> str | None:
    """Returns a usable access token for this person, refreshing first if
    needed. None means "not connected, or the connection broke" (e.g. access
    was revoked in their Google account) — every caller treats that exactly
    like never having connected at all, no special-casing needed upstream."""
    tokens = get_calendar_tokens(telegram_user_id)
    if tokens is None:
        return None

    if tokens.token_expiry - TOKEN_EXPIRY_BUFFER_SECONDS > time.time():
        return tokens.access_token

    refreshed = refresh_access_token(tokens.refresh_token)
    if refreshed is None or "access_token" not in refreshed:
        return None

    expiry = int(time.time()) + int(refreshed.get("expires_in", 3600))
    # Google's refresh grant doesn't reissue a refresh_token, so keep the one
    # we already have on file.
    save_calendar_tokens(telegram_user_id, refreshed["access_token"], tokens.refresh_token, expiry)
    return refreshed["access_token"]
