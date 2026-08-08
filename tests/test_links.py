"""Offline tests for choir/actions/links.py — no network calls, no API keys.
Pure functions only: link-building and distance math.
"""
import unittest

from choir.actions.links import (
    build_ride_deeplink,
    describe_ride_suggestion,
    find_carpool_pairs,
    WALK_DISTANCE_THRESHOLD_KM,
    CARPOOL_DISTANCE_THRESHOLD_KM,
)

HSR = (12.9121, 77.6446)
KORAMANGALA = (12.9352, 77.6245)  # ~4km from HSR
NEXT_DOOR = (12.9125, 77.6450)    # a few hundred meters from HSR


class TestBuildRideDeeplink(unittest.TestCase):
    def test_encodes_pickup_and_dropoff_coordinates(self):
        link = build_ride_deeplink(pickup=HSR, dropoff=KORAMANGALA)

        self.assertTrue(link.startswith("https://m.uber.com/ul/?"))
        self.assertIn("pickup%5Blatitude%5D=12.9121", link)
        self.assertIn("dropoff%5Blatitude%5D=12.9352", link)
        self.assertIn("action=setPickup", link)


class TestDescribeRideSuggestion(unittest.TestCase):
    def test_below_walk_threshold_returns_none(self):
        self.assertIsNone(describe_ride_suggestion(WALK_DISTANCE_THRESHOLD_KM - 0.1, "http://example.com"))

    def test_at_or_above_walk_threshold_returns_distance_and_link(self):
        text = describe_ride_suggestion(4.2, "http://example.com/ride")
        self.assertIn("4.2km", text)
        self.assertIn("http://example.com/ride", text)

    def test_never_fabricates_a_time_estimate(self):
        # No routing API exists to produce a real travel-time number —
        # asserting "min" is absent locks in that we never invent one.
        text = describe_ride_suggestion(10.0, "http://example.com")
        self.assertNotIn("min", text)


class TestFindCarpoolPairs(unittest.TestCase):
    def test_finds_a_pair_within_threshold(self):
        coords = {1: HSR, 2: NEXT_DOOR}
        pairs = find_carpool_pairs(coords)

        self.assertEqual(len(pairs), 1)
        user_a, user_b, distance = pairs[0]
        self.assertEqual({user_a, user_b}, {1, 2})
        self.assertLess(distance, CARPOOL_DISTANCE_THRESHOLD_KM)

    def test_no_pair_when_everyone_is_far_apart(self):
        coords = {1: HSR, 2: KORAMANGALA}
        self.assertEqual(find_carpool_pairs(coords), [])

    def test_three_people_two_close_one_far(self):
        coords = {1: HSR, 2: NEXT_DOOR, 3: KORAMANGALA}
        pairs = find_carpool_pairs(coords)

        matched = [{a, b} for a, b, _ in pairs]
        self.assertIn({1, 2}, matched)
        self.assertNotIn({1, 3}, matched)
        self.assertNotIn({2, 3}, matched)

    def test_single_person_has_no_pairs(self):
        self.assertEqual(find_carpool_pairs({1: HSR}), [])


if __name__ == "__main__":
    unittest.main()
