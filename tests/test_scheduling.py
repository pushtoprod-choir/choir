"""Offline tests for choir/calendar/scheduling.py's deterministic event
building — no live API calls, no LLM call at all: plan_date and decided_time
are guaranteed present by the negotiation contract (plan_date is fixed by the
/choir plan command, decided_time is what the agents converged on — see
choir/engine/orchestrator.py), so build_event_details only needs to combine
them, not guess them from free text.
"""
import os
import unittest

from choir.calendar.scheduling import build_event_details
from choir.schemas import VenueResult

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


if __name__ == "__main__":
    unittest.main()
