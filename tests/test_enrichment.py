"""Offline tests for choir/venues/enrichment.py — no live API calls, no web
search. Mocks the raw Anthropic response shape to confirm the fall-soft
behavior and the (now schema-guaranteed) response parsing. Zero coverage
existed for this module before this pass — it previously did fragile manual
```json fence-stripping and free-text json.loads(), the same brittle pattern
agent.py deliberately avoids elsewhere in this codebase.
"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from choir.venues.enrichment import enrich_venues
from choir.schemas import VenueResult

VENUES = [
    VenueResult(name="Cafe A", address="HSR", rating=0, price_level=0, lat=12.9, lon=77.6),
    VenueResult(name="Cafe B", address="Indiranagar", rating=0, price_level=0, lat=12.97, lon=77.64),
]


def text_response(payload: dict):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])


class TestEnrichVenues(unittest.TestCase):
    def test_empty_input_short_circuits_without_calling_the_api(self):
        with patch("choir.venues.enrichment.client.with_options") as mock_with_options:
            result = enrich_venues([], "casual_lunch")
            self.assertEqual(result, [])
            mock_with_options.assert_not_called()

    @patch("choir.venues.enrichment.client.with_options")
    def test_happy_path_attaches_notes_in_order(self, mock_with_options):
        mock_with_options.return_value.messages.create.return_value = text_response(
            {"notes": [{"note": "great coffee"}, {"note": "busy on weekends"}]}
        )
        result = enrich_venues(VENUES, "casual_lunch")

        self.assertEqual(result[0].note, "great coffee")
        self.assertEqual(result[1].note, "busy on weekends")
        # Original venue data (name/address/coordinates) must survive untouched.
        self.assertEqual(result[0].name, "Cafe A")
        self.assertEqual(result[0].lat, 12.9)

    @patch("choir.venues.enrichment.client.with_options")
    def test_empty_note_string_becomes_none(self, mock_with_options):
        mock_with_options.return_value.messages.create.return_value = text_response(
            {"notes": [{"note": ""}, {"note": "found it"}]}
        )
        result = enrich_venues(VENUES, "casual_lunch")

        self.assertIsNone(result[0].note)
        self.assertEqual(result[1].note, "found it")

    @patch("choir.venues.enrichment.client.with_options")
    def test_mismatched_note_count_falls_back_to_original_venues(self, mock_with_options):
        mock_with_options.return_value.messages.create.return_value = text_response(
            {"notes": [{"note": "only one note for two venues"}]}
        )
        result = enrich_venues(VENUES, "casual_lunch")

        self.assertEqual(result, VENUES)
        self.assertIsNone(result[0].note)

    @patch("choir.venues.enrichment.client.with_options")
    def test_malformed_json_falls_back_to_original_venues(self, mock_with_options):
        mock_with_options.return_value.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="not valid json at all")]
        )
        result = enrich_venues(VENUES, "casual_lunch")

        self.assertEqual(result, VENUES)

    @patch("choir.venues.enrichment.client.with_options")
    def test_api_exception_falls_back_to_original_venues(self, mock_with_options):
        mock_with_options.return_value.messages.create.side_effect = TimeoutError("took too long")
        result = enrich_venues(VENUES, "casual_lunch")

        self.assertEqual(result, VENUES)


if __name__ == "__main__":
    unittest.main()
