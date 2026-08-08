"""Offline tests for the negotiation append-only audit log in
choir/store/profiles.py — uses a temp SQLite file, never touches the real
choir.db. This is what makes negotiations recoverable/inspectable after a
crash or restart instead of living only in local variables for the duration
of one run_negotiation() call.
"""
import json
import os
import tempfile
import unittest

import choir.store.profiles as profiles
from choir.schemas import UserProfile


class TestNegotiationLog(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        self._original_db_path = profiles.DB_PATH
        profiles.DB_PATH = path
        self.addCleanup(setattr, profiles, "DB_PATH", self._original_db_path)
        profiles.init_db()

    def test_full_round_trip_start_record_finish_and_read_back(self):
        negotiation_id = profiles.start_negotiation_log(chat_id=100, goal_text="plan lunch")
        self.assertIsInstance(negotiation_id, int)

        profiles.record_negotiation_round(negotiation_id, 0, json.dumps([{"stance": "COUNTER"}]))
        profiles.record_negotiation_round(negotiation_id, 1, json.dumps([{"stance": "ACCEPT"}]))
        profiles.finish_negotiation_log(negotiation_id, converged=True, decision="Cafe X")

        history = profiles.get_negotiation_history(chat_id=100)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["decision"], "Cafe X")
        self.assertTrue(history[0]["converged"])
        self.assertIsNotNone(history[0]["finished_at"])

    def test_a_negotiation_that_crashed_before_finishing_still_leaves_a_record(self):
        # This is the actual point of the audit log: a negotiation that
        # started and recorded rounds but never called finish_negotiation_log
        # (e.g. the process crashed mid-loop) is still on disk, not lost.
        negotiation_id = profiles.start_negotiation_log(chat_id=200, goal_text="plan dinner")
        profiles.record_negotiation_round(negotiation_id, 0, json.dumps([{"stance": "COUNTER"}]))

        history = profiles.get_negotiation_history(chat_id=200)
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0]["finished_at"])
        self.assertIsNone(history[0]["converged"])

    def test_history_is_scoped_per_chat(self):
        profiles.start_negotiation_log(chat_id=1, goal_text="lunch")
        profiles.start_negotiation_log(chat_id=2, goal_text="dinner")

        self.assertEqual(len(profiles.get_negotiation_history(chat_id=1)), 1)
        self.assertEqual(len(profiles.get_negotiation_history(chat_id=2)), 1)
        self.assertEqual(profiles.get_negotiation_history(chat_id=1)[0]["goal_text"], "lunch")

    def test_history_orders_newest_first(self):
        first_id = profiles.start_negotiation_log(chat_id=1, goal_text="first")
        profiles.finish_negotiation_log(first_id, converged=True, decision="A")
        second_id = profiles.start_negotiation_log(chat_id=1, goal_text="second")
        profiles.finish_negotiation_log(second_id, converged=True, decision="B")

        history = profiles.get_negotiation_history(chat_id=1)
        self.assertEqual(history[0]["decision"], "B")
        self.assertEqual(history[1]["decision"], "A")


class TestTrips(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        self._original_db_path = profiles.DB_PATH
        profiles.DB_PATH = path
        self.addCleanup(setattr, profiles, "DB_PATH", self._original_db_path)
        profiles.init_db()

    def test_no_active_trip_by_default(self):
        self.assertIsNone(profiles.get_active_trip(chat_id=100))

    def test_start_trip_makes_it_the_active_trip(self):
        trip_id = profiles.start_trip(chat_id=100, started_by=1, title="Goa")
        active = profiles.get_active_trip(chat_id=100)
        self.assertEqual(active["id"], trip_id)
        self.assertEqual(active["title"], "Goa")

    def test_negotiations_started_with_a_trip_id_are_linked_to_it(self):
        trip_id = profiles.start_trip(chat_id=100, started_by=1, title="Goa")
        profiles.start_negotiation_log(chat_id=100, goal_text="plan lunch", trip_id=trip_id)
        profiles.start_negotiation_log(chat_id=100, goal_text="untagged plan")

        linked = profiles.get_trip_negotiations(trip_id)
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]["goal_text"], "plan lunch")

    def test_end_trip_clears_active_status_and_stores_summary(self):
        trip_id = profiles.start_trip(chat_id=100, started_by=1, title="Goa")
        profiles.end_trip(trip_id, summary="• plan lunch: Cafe X")

        self.assertIsNone(profiles.get_active_trip(chat_id=100))
        history = profiles.get_trip_history(chat_id=100)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "ended")
        self.assertEqual(history[0]["summary"], "• plan lunch: Cafe X")
        self.assertIsNotNone(history[0]["ended_at"])

    def test_starting_a_new_trip_after_ending_the_last_one_is_allowed(self):
        first_id = profiles.start_trip(chat_id=100, started_by=1, title="Goa")
        profiles.end_trip(first_id, summary=None)
        second_id = profiles.start_trip(chat_id=100, started_by=1, title="Manali")

        active = profiles.get_active_trip(chat_id=100)
        self.assertEqual(active["id"], second_id)
        self.assertNotEqual(first_id, second_id)


class TestCalendarEvents(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        self._original_db_path = profiles.DB_PATH
        profiles.DB_PATH = path
        self.addCleanup(setattr, profiles, "DB_PATH", self._original_db_path)
        profiles.init_db()

    def test_no_events_recorded_by_default(self):
        self.assertEqual(profiles.get_calendar_events(negotiation_id=1), {})

    def test_records_and_reads_back_events_per_negotiation(self):
        profiles.record_calendar_events(negotiation_id=1, event_ids={10: "evt-a", 20: "evt-b"})

        self.assertEqual(profiles.get_calendar_events(negotiation_id=1), {10: "evt-a", 20: "evt-b"})
        # A different negotiation_id must not see these — this is what lets a
        # /choir update clean up exactly the OLD negotiation's events, not
        # every event ever created for the chat.
        self.assertEqual(profiles.get_calendar_events(negotiation_id=2), {})

    def test_recording_with_an_empty_dict_is_a_safe_no_op(self):
        profiles.record_calendar_events(negotiation_id=1, event_ids={})
        self.assertEqual(profiles.get_calendar_events(negotiation_id=1), {})

    def test_delete_calendar_event_records_removes_only_that_negotiations_rows(self):
        profiles.record_calendar_events(negotiation_id=1, event_ids={10: "evt-a"})
        profiles.record_calendar_events(negotiation_id=2, event_ids={10: "evt-c"})

        profiles.delete_calendar_event_records(negotiation_id=1)

        self.assertEqual(profiles.get_calendar_events(negotiation_id=1), {})
        self.assertEqual(profiles.get_calendar_events(negotiation_id=2), {10: "evt-c"})


class TestPreferenceConfidence(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        self._original_db_path = profiles.DB_PATH
        profiles.DB_PATH = path
        self.addCleanup(setattr, profiles, "DB_PATH", self._original_db_path)
        profiles.init_db()

        self.profile = UserProfile(
            telegram_user_id=1, budget_min=100, budget_max=500,
            preferences=["vegetarian", "quiet_places"], area="HSR",
        )
        profiles.save_profile(self.profile)

    def test_freshly_saved_profile_has_no_confidence_entries_yet(self):
        loaded = profiles.get_profile(1)
        self.assertEqual(loaded.preference_confidence, {})

    def test_acceptance_nudges_every_stated_tag_up_from_the_default(self):
        profiles.update_preference_confidence(telegram_user_id=1, accepted=True)

        loaded = profiles.get_profile(1)
        expected = round(profiles.DEFAULT_CONFIDENCE + profiles.CONFIDENCE_STEP, 3)
        self.assertEqual(loaded.preference_confidence["vegetarian"], expected)
        self.assertEqual(loaded.preference_confidence["quiet_places"], expected)

    def test_non_acceptance_nudges_down_from_the_default(self):
        profiles.update_preference_confidence(telegram_user_id=1, accepted=False)

        loaded = profiles.get_profile(1)
        expected = round(profiles.DEFAULT_CONFIDENCE - profiles.CONFIDENCE_STEP, 3)
        self.assertEqual(loaded.preference_confidence["vegetarian"], expected)

    def test_repeated_negotiations_compound_and_visibly_shift_over_time(self):
        for _ in range(3):
            profiles.update_preference_confidence(telegram_user_id=1, accepted=True)

        loaded = profiles.get_profile(1)
        expected = round(profiles.DEFAULT_CONFIDENCE + 3 * profiles.CONFIDENCE_STEP, 3)
        self.assertEqual(loaded.preference_confidence["vegetarian"], expected)

    def test_confidence_is_clamped_to_the_0_to_1_range(self):
        for _ in range(50):
            profiles.update_preference_confidence(telegram_user_id=1, accepted=True)
        self.assertEqual(profiles.get_profile(1).preference_confidence["vegetarian"], 1.0)

        for _ in range(50):
            profiles.update_preference_confidence(telegram_user_id=1, accepted=False)
        self.assertEqual(profiles.get_profile(1).preference_confidence["vegetarian"], 0.0)

    def test_unknown_user_is_a_safe_no_op(self):
        profiles.update_preference_confidence(telegram_user_id=999, accepted=True)  # must not raise

    def test_profile_with_no_stated_preferences_is_a_safe_no_op(self):
        profiles.save_profile(UserProfile(
            telegram_user_id=2, budget_min=100, budget_max=500, preferences=[], area="HSR",
        ))
        profiles.update_preference_confidence(telegram_user_id=2, accepted=True)
        self.assertEqual(profiles.get_profile(2).preference_confidence, {})


if __name__ == "__main__":
    unittest.main()
