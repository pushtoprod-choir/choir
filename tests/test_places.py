"""Offline unit tests for the venue service — no LocationIQ key needed.

Mocks requests.get so these test the purpose-detection and response-parsing
LOGIC in places.py, not LocationIQ's actual API (same split as
tests/test_orchestrator.py vs. test_engine.py for the negotiation engine).

Run: python -m unittest tests/test_places.py -v
"""
import unittest
from unittest.mock import patch

from choir.schemas import UserProfile, VenueQuery
from choir.venues.places import build_venue_query, detect_purpose, find_venues


def fake_response(json_body):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return json_body

    return FakeResponse()


class TestDetectPurpose(unittest.TestCase):
    def test_detects_drinks(self):
        self.assertEqual(detect_purpose("let's grab drinks tonight"), "drinks")

    def test_detects_work_meeting(self):
        self.assertEqual(detect_purpose("need a place for a work sync"), "work_meeting")

    def test_defaults_to_casual_lunch(self):
        self.assertEqual(detect_purpose("plan something for us"), "casual_lunch")


class TestBuildVenueQuery(unittest.TestCase):
    def test_uses_the_most_constrained_budget(self):
        profiles = [
            UserProfile(telegram_user_id=1, budget_min=100, budget_max=300, preferences=[], area="HSR"),
            UserProfile(telegram_user_id=2, budget_min=200, budget_max=900, preferences=[], area="Indiranagar"),
        ]

        query = build_venue_query(profiles, "plan lunch")

        self.assertEqual(query.budget_max, 300)
        self.assertEqual(query.areas, ["HSR", "Indiranagar"])
        self.assertEqual(query.purpose, "casual_lunch")


class TestFindVenues(unittest.TestCase):
    @patch("choir.venues.places.requests.get")
    def test_geocodes_every_area_and_averages_the_midpoint(self, mock_get):
        mock_get.side_effect = [
            fake_response([{"lat": "12.90", "lon": "77.60"}]),  # geocode area 1
            fake_response([{"lat": "12.98", "lon": "77.64"}]),  # geocode area 2
            fake_response([  # nearby search
                {"name": "Cafe A", "display_name": "Cafe A, HSR, Bengaluru"},
                {"display_name": "Only Address Cafe, Indiranagar, Bengaluru"},
            ]),
        ]

        results = find_venues(
            VenueQuery(purpose="casual_lunch", areas=["HSR", "Indiranagar"], budget_max=500),
            api_key="test-key",
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].name, "Cafe A")
        self.assertEqual(results[0].address, "Cafe A, HSR, Bengaluru")
        self.assertEqual(results[0].rating, 0.0)
        # falls back to the first display_name segment when "name" is missing
        self.assertEqual(results[1].name, "Only Address Cafe")

        nearby_call = mock_get.call_args_list[-1]
        self.assertAlmostEqual(nearby_call.kwargs["params"]["lat"], (12.90 + 12.98) / 2)
        self.assertEqual(nearby_call.kwargs["params"]["tag"], "restaurant")

    @patch("choir.venues.places.requests.get")
    def test_returns_empty_when_no_area_geocodes(self, mock_get):
        mock_get.return_value = fake_response([])

        results = find_venues(VenueQuery(purpose="drinks", areas=["Nowhere"], budget_max=500), api_key="test-key")

        self.assertEqual(results, [])
        mock_get.assert_called_once()  # never got as far as the nearby search

    @patch("choir.venues.places.requests.get")
    def test_returns_empty_when_nearby_search_finds_nothing(self, mock_get):
        mock_get.side_effect = [
            fake_response([{"lat": "12.90", "lon": "77.60"}]),
            fake_response({"error": "Unable to geocode"}),  # LocationIQ's no-match shape
        ]

        results = find_venues(VenueQuery(purpose="casual_lunch", areas=["HSR"], budget_max=500), api_key="test-key")

        self.assertEqual(results, [])

    @patch("choir.venues.places.requests.get")
    def test_uses_the_tag_matching_purpose(self, mock_get):
        mock_get.side_effect = [
            fake_response([{"lat": "12.90", "lon": "77.60"}]),
            fake_response([]),
        ]

        find_venues(VenueQuery(purpose="drinks", areas=["HSR"], budget_max=500), api_key="test-key")

        nearby_call = mock_get.call_args_list[-1]
        self.assertEqual(nearby_call.kwargs["params"]["tag"], "bar")


if __name__ == "__main__":
    unittest.main()
