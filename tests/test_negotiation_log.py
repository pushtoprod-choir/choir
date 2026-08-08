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

    def test_plan_date_and_decided_time_round_trip(self):
        negotiation_id = profiles.start_negotiation_log(chat_id=100, goal_text="plan lunch", plan_date="2026-08-15")
        profiles.finish_negotiation_log(negotiation_id, converged=True, decision="Cafe X", decided_time="19:30")

        history = profiles.get_negotiation_history(chat_id=100)
        self.assertEqual(history[0]["plan_date"], "2026-08-15")
        self.assertEqual(history[0]["decided_time"], "19:30")

    def test_plan_date_and_decided_time_default_to_none(self):
        negotiation_id = profiles.start_negotiation_log(chat_id=100, goal_text="plan lunch")
        profiles.finish_negotiation_log(negotiation_id, converged=False, decision=None)

        history = profiles.get_negotiation_history(chat_id=100)
        self.assertIsNone(history[0]["plan_date"])
        self.assertIsNone(history[0]["decided_time"])


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

    def test_trip_negotiations_include_plan_date_and_decided_time(self):
        trip_id = profiles.start_trip(chat_id=100, started_by=1, title="Goa")
        negotiation_id = profiles.start_negotiation_log(
            chat_id=100, goal_text="plan lunch", trip_id=trip_id, plan_date="2026-08-15"
        )
        profiles.finish_negotiation_log(negotiation_id, converged=True, decision="Cafe X", decided_time="19:30")

        linked = profiles.get_trip_negotiations(trip_id)
        self.assertEqual(linked[0]["plan_date"], "2026-08-15")
        self.assertEqual(linked[0]["decided_time"], "19:30")

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


if __name__ == "__main__":
    unittest.main()
