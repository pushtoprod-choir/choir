"""Offline tests for choir/calendar/client.py's create_event — no live API
calls. Regression coverage for the bug where the Google Calendar POST body
never included an explicit timeZone, letting different participants'
accounts render the same dateTime at different clock times.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

from choir.calendar.client import create_event
from choir.schemas import EventDetails

os.environ.setdefault("CHOIR_TIMEZONE", "Asia/Kolkata")


class TestCreateEvent(unittest.TestCase):
    @patch("choir.calendar.client.requests.post")
    def test_sends_explicit_timezone_alongside_datetime(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        details = EventDetails(
            title="Lunch", start_iso="2026-08-09T19:30:00+05:30",
            duration_minutes=90, location_text="Cafe X",
        )

        result = create_event("token", details)

        self.assertTrue(result)
        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body["start"]["timeZone"], "Asia/Kolkata")
        self.assertEqual(body["end"]["timeZone"], "Asia/Kolkata")

    @patch("choir.calendar.client.requests.post")
    def test_naive_start_iso_is_treated_as_local_time_not_left_naive(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        details = EventDetails(
            title="Lunch", start_iso="2026-08-09T19:30:00",
            duration_minutes=90, location_text="Cafe X",
        )

        create_event("token", details)

        body = mock_post.call_args.kwargs["json"]
        self.assertTrue(body["start"]["dateTime"].startswith("2026-08-09T19:30:00+05:30"))
        self.assertEqual(body["start"]["timeZone"], "Asia/Kolkata")


if __name__ == "__main__":
    unittest.main()
