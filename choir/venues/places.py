"""Person C's venue service — purpose detection + real venue lookup.

See plan.md Phase 5. The plan's sketch used Google Places; this project uses
LocationIQ instead (backed by OpenStreetMap data — the key in .env is
LOCATION_IQ_API_KEY). LocationIQ has no Google-style ratings or price levels,
so VenueResult.rating/price_level are always 0 for results from here: showing
nothing is more honest than inventing a plausible-looking number.
"""
from typing import Optional

import requests

from choir.schemas import UserProfile, VenueQuery, VenueResult

GEOCODE_URL = "https://us1.locationiq.com/v1/search"
NEARBY_URL = "https://us1.locationiq.com/v1/nearby"
SEARCH_RADIUS_METERS = 2000
REQUEST_TIMEOUT_SECONDS = 10

# purpose -> LocationIQ nearby "tag" filter. Simplest working version, same
# spirit as the keyword_map in plan.md's Phase 5 sketch.
PURPOSE_TAGS = {
    "casual_lunch": "restaurant",
    "drinks": "bar",
    "work_meeting": "cafe",
    "activity": "bowling alley OR arcade OR escape room",
    "outdoor": "park",
}
DEFAULT_PURPOSE = "casual_lunch"

# Keyword spotting on the raw /choir goal text. This is the one place the
# purpose vocabulary is defined, since build_venue_query() and PURPOSE_TAGS
# above both need to agree on what these strings mean.
PURPOSE_KEYWORDS = {
    "drinks": ["drink", "bar", "beer", "cocktail", "pub"],
    "work_meeting": ["work", "meeting", "call", "sync", "standup", "wifi"],
    "casual_lunch": ["lunch", "dinner", "breakfast", "eat", "food", "meal", "brunch"],
    "activity": ["bowling", "arcade", "escape room", "escape", "mini golf", "minigolf", "game", "activity"],
    "outdoor": ["park", "picnic", "outdoor", "hike", "hiking", "trail", "walk"],
}


def detect_purpose(goal_text: str) -> str:
    lowered = goal_text.lower()
    for purpose, keywords in PURPOSE_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return purpose
    return DEFAULT_PURPOSE


def build_venue_query(profiles: list[UserProfile], goal_text: str) -> VenueQuery:
    """budget_max is the tightest of everyone's budgets, not an average —
    a venue suggestion that only half the group can afford isn't useful."""
    return VenueQuery(
        purpose=detect_purpose(goal_text),
        areas=[profile.area for profile in profiles],
        budget_max=min(profile.budget_max for profile in profiles),
    )


def _geocode(area: str, api_key: str) -> Optional[tuple[float, float]]:
    try:
        response = requests.get(
            GEOCODE_URL,
            params={"key": api_key, "q": area, "format": "json", "limit": 1},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        results = response.json()
    except requests.RequestException:
        return None

    if not isinstance(results, list) or not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


def find_venues(query: VenueQuery, api_key: str) -> list[VenueResult]:
    """Geocodes every stated area, averages the coordinates as a rough
    midpoint (good enough for a hackathon — same approach as the original
    Google Places sketch in plan.md), then searches LocationIQ's nearby-POI
    endpoint around that point for the tag matching the meetup's purpose.
    Fails soft (returns []) on any lookup problem — venue suggestions are a
    bonus on top of the negotiated decision, not something worth crashing
    the whole /choir command over."""
    coords = [c for c in (_geocode(area, api_key) for area in query.areas) if c is not None]
    if not coords:
        return []

    midpoint_lat = sum(lat for lat, _ in coords) / len(coords)
    midpoint_lon = sum(lon for _, lon in coords) / len(coords)

    try:
        response = requests.get(
            NEARBY_URL,
            params={
                "key": api_key,
                "lat": midpoint_lat,
                "lon": midpoint_lon,
                "tag": PURPOSE_TAGS.get(query.purpose, PURPOSE_TAGS[DEFAULT_PURPOSE]),
                "radius": SEARCH_RADIUS_METERS,
                "limit": 5,
                "dedupe": 1,
                "format": "json",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        places = response.json()
    except requests.RequestException:
        return []

    if not isinstance(places, list):  # LocationIQ returns {"error": "..."} when nothing matches
        return []

    return [
        VenueResult(
            name=place.get("name") or place.get("display_name", "").split(",")[0],
            address=place.get("display_name", ""),
            rating=0.0,
            price_level=0,
            lat=_safe_float(place.get("lat")),
            lon=_safe_float(place.get("lon")),
        )
        for place in places
    ]


def _safe_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
