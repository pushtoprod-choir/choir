"""Offline tests for choir/bot/date_extraction.py — no live API calls. This
is the one gate in the /choir plan command that fails CLOSED (returns None)
rather than open: a wrong guess here means a calendar event on the wrong day,
so any ambiguity or error must come back as "no date", not a best guess.
"""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from choir.bot.date_extraction import extract_plan_date

os.environ.setdefault("CHOIR_TIMEZONE", "Asia/Kolkata")


def text_response(payload: dict):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])


class TestExtractPlanDate(unittest.TestCase):
    @patch("choir.bot.date_extraction.client.messages.create")
    def test_explicit_future_date_is_returned(self, mock_create):
        mock_create.return_value = text_response({"has_date": True, "date": "2026-12-25"})

        self.assertEqual(extract_plan_date("dinner on 25th December"), "2026-12-25")

    @patch("choir.bot.date_extraction.client.messages.create")
    def test_no_date_mentioned_returns_none(self, mock_create):
        mock_create.return_value = text_response({"has_date": False, "date": None})

        self.assertIsNone(extract_plan_date("lunch for us"))

    @patch("choir.bot.date_extraction.client.messages.create")
    def test_has_date_true_but_null_date_is_treated_as_no_date(self, mock_create):
        # Defense in depth against a model reply that's internally
        # inconsistent — the schema doesn't forbid this combination.
        mock_create.return_value = text_response({"has_date": True, "date": None})

        self.assertIsNone(extract_plan_date("dinner sometime"))

    @patch("choir.bot.date_extraction.client.messages.create")
    def test_a_past_date_is_treated_as_no_date(self, mock_create):
        mock_create.return_value = text_response({"has_date": True, "date": "2020-01-01"})

        self.assertIsNone(extract_plan_date("dinner on 1st Jan 2020"))

    @patch("choir.bot.date_extraction.client.messages.create")
    def test_malformed_date_string_fails_closed_instead_of_raising(self, mock_create):
        mock_create.return_value = text_response({"has_date": True, "date": "not a real date"})

        self.assertIsNone(extract_plan_date("dinner whenever"))

    @patch("choir.bot.date_extraction.client.messages.create")
    def test_api_error_fails_closed(self, mock_create):
        mock_create.side_effect = Exception("boom")

        self.assertIsNone(extract_plan_date("dinner this Saturday"))


if __name__ == "__main__":
    unittest.main()
