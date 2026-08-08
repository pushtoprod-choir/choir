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

from choir.calendar.scheduling import create_events_for_connected, delete_events_for_negotiation, extract_event_details
from choir.schemas import UserProfile

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


class TestCreateEventsForConnected(unittest.TestCase):
    def setUp(self):
        self.profile = UserProfile(
            telegram_user_id=1, budget_min=100, budget_max=500, preferences=[], area="HSR",
        )

    @patch("choir.calendar.scheduling.create_event", return_value="evt-123")
    @patch("choir.calendar.scheduling.get_valid_access_token", return_value="token")
    @patch("choir.calendar.scheduling.is_calendar_connected", return_value=True)
    @patch("choir.calendar.scheduling.extract_event_details")
    def test_summary_carries_back_the_real_event_id_per_person(
        self, mock_extract, mock_connected, mock_token, mock_create
    ):
        mock_extract.return_value = SimpleNamespace(
            title="Lunch", start_iso="2026-08-09T19:30:00+05:30", duration_minutes=90, location_text="Cafe X",
        )

        summary = create_events_for_connected([self.profile], "plan lunch", "Cafe X")

        self.assertEqual(summary.created, 1)
        self.assertEqual(summary.event_ids, {1: "evt-123"})

    @patch("choir.calendar.scheduling.create_event", return_value=None)
    @patch("choir.calendar.scheduling.get_valid_access_token", return_value="token")
    @patch("choir.calendar.scheduling.is_calendar_connected", return_value=True)
    @patch("choir.calendar.scheduling.extract_event_details")
    def test_a_failed_create_is_not_counted_or_recorded(self, mock_extract, mock_connected, mock_token, mock_create):
        mock_extract.return_value = SimpleNamespace(
            title="Lunch", start_iso="2026-08-09T19:30:00+05:30", duration_minutes=90, location_text="Cafe X",
        )

        summary = create_events_for_connected([self.profile], "plan lunch", "Cafe X")

        self.assertEqual(summary.created, 0)
        self.assertEqual(summary.event_ids, {})


class TestDeleteEventsForNegotiation(unittest.TestCase):
    @patch("choir.calendar.scheduling.delete_calendar_event_records")
    @patch("choir.calendar.scheduling.delete_event", return_value=True)
    @patch("choir.calendar.scheduling.get_valid_access_token", return_value="token")
    @patch("choir.calendar.scheduling.get_calendar_events", return_value={1: "evt-a", 2: "evt-b"})
    def test_deletes_every_recorded_event_then_clears_the_records(
        self, mock_get_events, mock_token, mock_delete, mock_clear
    ):
        delete_events_for_negotiation(negotiation_id=42)

        self.assertEqual(mock_delete.call_count, 2)
        mock_clear.assert_called_once_with(42)

    @patch("choir.calendar.scheduling.delete_calendar_event_records")
    @patch("choir.calendar.scheduling.delete_event")
    @patch("choir.calendar.scheduling.get_calendar_events", return_value={})
    def test_nothing_recorded_is_a_safe_no_op(self, mock_get_events, mock_delete, mock_clear):
        delete_events_for_negotiation(negotiation_id=42)

        mock_delete.assert_not_called()
        mock_clear.assert_not_called()

    @patch("choir.calendar.scheduling.delete_calendar_event_records")
    @patch("choir.calendar.scheduling.delete_event")
    @patch("choir.calendar.scheduling.get_valid_access_token", return_value=None)
    @patch("choir.calendar.scheduling.get_calendar_events", return_value={1: "evt-a"})
    def test_a_disconnected_persons_missing_token_is_skipped_not_crashed(
        self, mock_get_events, mock_token, mock_delete, mock_clear
    ):
        delete_events_for_negotiation(negotiation_id=42)  # must not raise

        mock_delete.assert_not_called()
        mock_clear.assert_called_once_with(42)


if __name__ == "__main__":
    unittest.main()
