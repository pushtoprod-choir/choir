"""Offline tests for choir/calendar/scheduling.py's deterministic event
building — no live API calls, no LLM call at all: plan_date and decided_time
are guaranteed present by the negotiation contract (plan_date is fixed by the
/choir plan command, decided_time is what the agents converged on — see
choir/engine/orchestrator.py), so build_event_details only needs to combine
them, not guess them from free text.
"""
import os
import unittest
from unittest.mock import patch

from choir.calendar.scheduling import build_event_details, create_events_for_connected, delete_events_for_negotiation
from choir.schemas import UserProfile, VenueResult

os.environ.setdefault("CHOIR_TIMEZONE", "Asia/Kolkata")


class TestBuildEventDetails(unittest.TestCase):
    def test_combines_plan_date_and_decided_time_into_a_local_aware_start_iso(self):
        details = build_event_details("Cafe X", "2026-08-15", "19:30", decided_venue=None)

        self.assertIsNotNone(details)
        self.assertTrue(details.start_iso.startswith("2026-08-15T19:30:00"))
        self.assertTrue(details.start_iso.endswith("+05:30"))

    def test_uses_decided_venue_name_as_location_when_present(self):
        venue = VenueResult(name="Cafe X", address="123 Some Road, HSR", rating=0, price_level=0)
        details = build_event_details("Cafe X", "2026-08-15", "19:30", decided_venue=venue)

        self.assertEqual(details.location_text, "Cafe X")

    def test_falls_back_to_decision_text_as_location_when_ungrounded(self):
        details = build_event_details("Dinner at whichever place works", "2026-08-15", "19:30", decided_venue=None)

        self.assertEqual(details.location_text, "Dinner at whichever place works")

    def test_title_is_the_decision_text(self):
        details = build_event_details("Cafe X", "2026-08-15", "19:30", decided_venue=None)

        self.assertEqual(details.title, "Cafe X")

    def test_malformed_date_or_time_returns_none_instead_of_raising(self):
        self.assertIsNone(build_event_details("Cafe X", "not-a-date", "19:30", decided_venue=None))
        self.assertIsNone(build_event_details("Cafe X", "2026-08-15", "not-a-time", decided_venue=None))


class TestCreateEventsForConnected(unittest.TestCase):
    def setUp(self):
        self.profile = UserProfile(
            telegram_user_id=1, budget_min=100, budget_max=500, preferences=[], area="HSR",
        )

    @patch("choir.calendar.scheduling.create_event", return_value="evt-123")
    @patch("choir.calendar.scheduling.get_valid_access_token", return_value="token")
    @patch("choir.calendar.scheduling.is_calendar_connected", return_value=True)
    def test_summary_carries_back_the_real_event_id_per_person(self, mock_connected, mock_token, mock_create):
        # No need to mock event-detail construction anymore — build_event_details
        # is deterministic (see TestBuildEventDetails above), so real plan_date/
        # decided_time values exercise the actual combination logic too.
        summary = create_events_for_connected(
            [self.profile], "Cafe X", "2026-08-15", "19:30", decided_venue=None,
        )

        self.assertEqual(summary.created, 1)
        self.assertEqual(summary.event_ids, {1: "evt-123"})

    @patch("choir.calendar.scheduling.create_event", return_value=None)
    @patch("choir.calendar.scheduling.get_valid_access_token", return_value="token")
    @patch("choir.calendar.scheduling.is_calendar_connected", return_value=True)
    def test_a_failed_create_is_not_counted_or_recorded(self, mock_connected, mock_token, mock_create):
        summary = create_events_for_connected(
            [self.profile], "Cafe X", "2026-08-15", "19:30", decided_venue=None,
        )

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
