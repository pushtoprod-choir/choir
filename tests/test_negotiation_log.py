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


if __name__ == "__main__":
    unittest.main()
