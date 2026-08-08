"""Person C's venue service — purpose detection + real venue lookup.

See plan.md Phase 5. The plan's sketch used Google Places; this project uses
LocationIQ instead (backed by OpenStreetMap data — the key in .env is
LOCATION_IQ_API_KEY). LocationIQ has no Google-style ratings or price levels,
so VenueResult.rating/price_level are always 0 for results from here: showing
nothing is more honest than inventing a plausible-looking number.
"""
import logging
import math
import os
from typing import Optional

import requests

from choir.schemas import UserProfile, VenueQuery, VenueResult

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://us1.locationiq.com/v1/search"
NEARBY_URL = "https://us1.locationiq.com/v1/nearby"
SEARCH_RADIUS_METERS = 2000
REQUEST_TIMEOUT_SECONDS = 10
EARTH_RADIUS_KM = 6371.0
# Bare area names ("Whitefield") are ambiguous worldwide — LocationIQ's
# unscoped search once resolved "Whitefield" to a town in New Hampshire, USA
# instead of the Bangalore neighborhood, which silently poisoned the
# midpoint calc in find_venues() and emptied out every real venue candidate
# for the whole negotiation. Every group member is in the same city for this
# hackathon build, so biasing every geocode call to one country is a safe,
# cheap fix — override via LOCATION_IQ_COUNTRY_BIAS if that ever changes.
GEOCODE_COUNTRY_BIAS = os.environ.get("LOCATION_IQ_COUNTRY_BIAS", "in")
# Defense-in-depth on top of the country bias above: if an area still
# geocodes somewhere wildly far from the rest of the group's areas (a bad
# match within the same country, a data glitch, etc.), averaging it into the
# search midpoint produces a meaningless location instead of a bad-but-still
# vaguely-local one. Generous on purpose — same spirit as
# orchestrator.MAX_VENUE_DISTANCE_KM, but wider, since this is a "did
# geocoding go completely off the rails" check, not the actual proximity
# constraint (that's still enforced by orchestrator._verify_decision).
MAX_AREA_SPREAD_KM = 100.0
# handlers.py's venue display now has its own length guard (_build_venues_text),
# so this can be tuned up from LocationIQ's smaller default without risking an
# over-length Telegram message.
NEARBY_RESULTS_LIMIT = 8

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
    """Keyword-spots the /choir goal text for purpose.

    This is also, deliberately, occasion-aware: choir/bot/handlers.py's
    occasion-capture flow appends the answer straight onto goal_text
    ("...(occasion: work catchup)") before it ever reaches this function, so
    "work catchup" or "birthday drinks" already steer purpose detection
    correctly with no extra plumbing — see tests/test_places.py's
    TestDetectPurpose.test_occasion_text_appended_by_handlers_influences_purpose
    for the regression test locking this in."""
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
            params={
                "key": api_key,
                "q": area,
                "format": "json",
                "limit": 1,
                "countrycodes": GEOCODE_COUNTRY_BIAS,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        results = response.json()
    except requests.RequestException:
        logger.warning("Geocoding request failed for area %r", area, exc_info=True)
        return None

    if not isinstance(results, list) or not results:
        logger.warning("Geocoding returned no match for area %r", area)
        return None
    coord = float(results[0]["lat"]), float(results[0]["lon"])
    logger.info("Geocoded area %r -> %s", area, coord)
    return coord


def geocode_areas(areas: list[str], api_key: str) -> dict[str, Optional[tuple[float, float]]]:
    """Geocodes each area once. find_venues() (search midpoint) and the
    orchestrator's per-person distance verification both need real
    coordinates for the same areas — sharing this means a negotiation
    geocodes each area exactly once, not twice."""
    return {area: _geocode(area, api_key) for area in areas}


def _drop_geocoding_outliers(
    area_coords: dict[str, Optional[tuple[float, float]]],
) -> dict[str, tuple[float, float]]:
    """Filters out any successfully-geocoded area that's implausibly far
    (> MAX_AREA_SPREAD_KM) from every other geocoded area, before it gets
    averaged into find_venues()'s search midpoint. A single bad geocode
    (wrong country/continent match) would otherwise drag the midpoint
    somewhere meaningless and silently empty out the nearby-venue search for
    the whole group — see GEOCODE_COUNTRY_BIAS's docstring for the real
    incident this guards against. Only compares against *other* areas, so it
    still works correctly with as few as two areas."""
    valid = {area: coord for area, coord in area_coords.items() if coord is not None}
    if len(valid) < 2:
        return valid

    kept: dict[str, tuple[float, float]] = {}
    for area, coord in valid.items():
        others = [c for a, c in valid.items() if a != area]
        if any(haversine_distance_km(coord, other) <= MAX_AREA_SPREAD_KM for other in others):
            kept[area] = coord
        else:
            logger.warning(
                "Dropping area %r (%s) as a geocoding outlier — implausibly far from every other area",
                area, coord,
            )
    # If everything got flagged as mutually far apart (a real, spread-out
    # group), don't zero out the whole search — fall back to using all of
    # them rather than none.
    return kept or valid


def haversine_distance_km(coord1: tuple[float, float], coord2: tuple[float, float]) -> float:
    """Real great-circle distance between two (lat, lon) points — used as a
    deterministic hard constraint, not an approximation for display."""
    lat1, lon1 = coord1
    lat2, lon2 = coord2
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def find_venues(
    query: VenueQuery, api_key: str, area_coords: Optional[dict[str, Optional[tuple[float, float]]]] = None
) -> list[VenueResult]:
    """Geocodes every stated area (or reuses area_coords if the caller
    already has it — see geocode_areas), averages the coordinates as a rough
    midpoint (good enough for a hackathon — same approach as the original
    Google Places sketch in plan.md), then searches LocationIQ's nearby-POI
    endpoint around that point for the tag matching the meetup's purpose.
    Fails soft (returns []) on any lookup problem — venue suggestions are a
    bonus on top of the negotiated decision, not something worth crashing
    the whole /choir command over."""
    if area_coords is None:
        area_coords = geocode_areas(query.areas, api_key)
    coords = list(_drop_geocoding_outliers(area_coords).values())
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
                "limit": NEARBY_RESULTS_LIMIT,
                "dedupe": 1,
                "format": "json",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        places = response.json()
    except requests.RequestException:
        logger.warning(
            "Nearby venue search request failed (midpoint=%s, %s)", midpoint_lat, midpoint_lon, exc_info=True
        )
        return []

    if not isinstance(places, list):  # LocationIQ returns {"error": "..."} when nothing matches
        logger.warning(
            "Nearby venue search found nothing (midpoint=%s, %s, tag=%s): %r",
            midpoint_lat, midpoint_lon, PURPOSE_TAGS.get(query.purpose, PURPOSE_TAGS[DEFAULT_PURPOSE]), places,
        )
        return []

    logger.info("Nearby venue search found %d candidates around (%s, %s)", len(places), midpoint_lat, midpoint_lon)
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
