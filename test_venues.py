"""Manual test for the venue service — hits the real LocationIQ API.

Run: python test_venues.py

Mirrors test_engine.py's role for the negotiation engine: this is the "does
it actually return real, useful venues" check, not just "does it crash."
"""
import os

from dotenv import load_dotenv

from choir.venues.places import build_venue_query, detect_purpose, find_venues
from choir.schemas import UserProfile

load_dotenv()  # picks up LOCATION_IQ_API_KEY from .env, same as main.py

profiles = [
    UserProfile(telegram_user_id=1, budget_min=100, budget_max=400, preferences=["budget_conscious"], area="HSR Layout, Bengaluru"),
    UserProfile(telegram_user_id=2, budget_min=300, budget_max=900, preferences=["foodie"], area="Koramangala, Bengaluru"),
]

goal_text = "plan lunch for us"

if __name__ == "__main__":
    purpose = detect_purpose(goal_text)
    print(f"goal: {goal_text!r} -> purpose: {purpose}")

    query = build_venue_query(profiles, goal_text)
    print(f"query: {query}")

    venues = find_venues(query, os.environ["LOCATION_IQ_API_KEY"])
    print(f"\n{len(venues)} venue(s) found:")
    for v in venues:
        print(f"  - {v.name} ({v.address})")
