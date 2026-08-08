"""Offline tests for choir/calendar/scheduling.py's start_iso normalization —
no live API calls. Regression coverage for the bug where two participants
ended up with different clock times for the same negotiated event: the model
is only prompt-instructed to include a UTC offset, not forced to, so a naive
start_iso needs to be caught and normalized here rather than flowing through
unchanged.
"""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from choir.calendar.scheduling import extract_event_details

os.environ.setdefault("CHOIR_TIMEZONE", "Asia/Kolkata")


def text_response(payload: dict):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])


class TestExtractEventDetails(unittest.TestCase):
    @patch("choir.calendar.scheduling.client.messages.create")
    def test_naive_start_iso_is_given_the_local_offset(self, mock_create):
        mock_create.return_value = text_response({
            "title": "Lunch", "start_iso": "2026-08-09T19:30:00",
            "duration_minutes": 90, "location_text": "Cafe X",
        })

        details = extract_event_details("plan lunch", "Cafe X at 7:30pm")

        self.assertTrue(details.start_iso.endswith("+05:30"))
        self.assertTrue(details.start_iso.startswith("2026-08-09T19:30:00"))

    @patch("choir.calendar.scheduling.client.messages.create")
    def test_aware_but_different_offset_is_normalized_to_local_wall_clock(self, mock_create):
        # 19:30 UTC is 01:00 the next day in Asia/Kolkata (+05:30) — this must
        # be an actual astimezone conversion, not just a relabeled offset.
        mock_create.return_value = text_response({
            "title": "Lunch", "start_iso": "2026-08-09T19:30:00+00:00",
            "duration_minutes": 90, "location_text": "Cafe X",
        })

        details = extract_event_details("plan lunch", "Cafe X at 7:30pm UTC")

        self.assertTrue(details.start_iso.endswith("+05:30"))
        self.assertTrue(details.start_iso.startswith("2026-08-10T01:00:00"))

    @patch("choir.calendar.scheduling.client.messages.create")
    def test_unparseable_start_iso_returns_none_instead_of_raising(self, mock_create):
        mock_create.return_value = text_response({
            "title": "Lunch", "start_iso": "not a real date",
            "duration_minutes": 90, "location_text": "Cafe X",
        })

        self.assertIsNone(extract_event_details("plan lunch", "Cafe X"))


if __name__ == "__main__":
    unittest.main()
