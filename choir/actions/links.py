"""Ride-suggestion stub: pure link/text-building functions, no network calls.

Deliberately NOT a real booking integration — real ride-hailing APIs need a
partner agreement and per-user OAuth, neither of which exists here. This
just builds a deep link Uber's own app understands (no API key required for
a deep link, unlike an actual booking call), plus the distance math to
decide whether showing one is even worth it.

Nobody's asked whether they own a vehicle — that would mean either a new
onboarding question (onboarding is meant to stay a fixed, stable baseline)
or a new per-negotiation dynamic question (which would add friction to a
mechanism that's deliberately kept low-noise). Distance is a good enough
proxy without asking anyone anything: close enough to walk, nobody needs a
suggestion regardless of what they drive; far enough, a distance + optional
link is useful information whether or not they end up tapping it.
"""
from urllib.parse import urlencode

from choir.venues.places import haversine_distance_km

UBER_DEEPLINK_BASE = "https://m.uber.com/ul/"

# Below this, it's a short enough walk that a ride suggestion would be noise
# regardless of how anyone's getting there.
WALK_DISTANCE_THRESHOLD_KM = 1.5

# Participants within this of EACH OTHER (not the venue) are close enough
# that sharing a ride is a reasonable suggestion.
CARPOOL_DISTANCE_THRESHOLD_KM = 2.0


def build_ride_deeplink(pickup: tuple[float, float], dropoff: tuple[float, float]) -> str:
    """Uber's universal link format — opens the Uber app (or a web fallback)
    with pickup/dropoff pre-filled. No API key or OAuth needed: this is just
    a URL, not a booking request, so it can never fail the way a real API
    call could."""
    pickup_lat, pickup_lon = pickup
    dropoff_lat, dropoff_lon = dropoff
    params = {
        "action": "setPickup",
        "pickup[latitude]": pickup_lat,
        "pickup[longitude]": pickup_lon,
        "dropoff[latitude]": dropoff_lat,
        "dropoff[longitude]": dropoff_lon,
    }
    return f"{UBER_DEEPLINK_BASE}?{urlencode(params)}"


def describe_ride_suggestion(distance_km: float, deeplink: str) -> str | None:
    """None below the walk threshold — the caller should send nothing at
    all rather than a suggestion nobody asked for and doesn't need. Only
    shows real, computed distance — no fabricated travel-time estimate,
    since that would need a routing API this project doesn't have."""
    if distance_km < WALK_DISTANCE_THRESHOLD_KM:
        return None
    return f"📍 {distance_km:.1f}km away\n🚗 Get a ride: {deeplink}"


def find_carpool_pairs(
    coords: dict[int, tuple[float, float]], threshold_km: float = CARPOOL_DISTANCE_THRESHOLD_KM
) -> list[tuple[int, int, float]]:
    """Every pair of user_ids whose own areas are within threshold_km of
    EACH OTHER (not the venue) — a cheap, no-new-dependency proxy for "these
    two could reasonably share a ride," using coordinates already geocoded
    for the venue-distance check elsewhere. Not real carpool matching (no
    route optimization, no fare splitting) — just a text nudge."""
    user_ids = list(coords.keys())
    pairs = []
    for i in range(len(user_ids)):
        for j in range(i + 1, len(user_ids)):
            a, b = user_ids[i], user_ids[j]
            distance = haversine_distance_km(coords[a], coords[b])
            if distance <= threshold_km:
                pairs.append((a, b, distance))
    return pairs
