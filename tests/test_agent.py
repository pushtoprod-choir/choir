"""Offline tests for choir/engine/agent.py's response handling — no live API
calls. Mocks choir.engine.agent.client.messages.create to exercise every
failure path identified in the audit: transport errors, safety refusals,
and malformed/truncated JSON that the schema constraint reduces but doesn't
eliminate. Before this file existed, none of these paths had test coverage
and three of them (refusal, empty content, malformed JSON) weren't even
caught by the code — they'd have crashed the whole negotiation.
"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx

from choir.engine.agent import _build_response_schema, get_agent_response
from choir.schemas import AgentSignal, UserProfile, VenueResult

PROFILE = UserProfile(telegram_user_id=1, budget_min=100, budget_max=500, preferences=["foodie"], area="HSR")


def text_response(payload: dict, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        stop_reason=stop_reason,
    )


class TestGetAgentResponse(unittest.TestCase):
    @patch("choir.engine.agent.client.messages.create")
    def test_happy_path_builds_the_expected_signal(self, mock_create):
        mock_create.return_value = text_response(
            {"stance": "COUNTER", "reason": "too far", "counter_proposal": "Cafe X"}
        )
        signal = get_agent_response(PROFILE, "plan lunch", None, 0, 4)

        self.assertEqual(signal.user_id, 1)
        self.assertEqual(signal.stance, "COUNTER")
        self.assertEqual(signal.counter_proposal, "Cafe X")

    @patch("choir.engine.agent.client.messages.create")
    def test_api_error_falls_back_to_reject_instead_of_raising(self, mock_create):
        mock_create.side_effect = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
        signal = get_agent_response(PROFILE, "plan lunch", None, 0, 4)

        self.assertEqual(signal.stance, "REJECT")
        self.assertIn("reach", signal.reason)

    @patch("choir.engine.agent.client.messages.create")
    def test_refusal_stop_reason_falls_back_to_reject_instead_of_crashing(self, mock_create):
        # HTTP 200 with an empty content array — exactly what a safety
        # classifier refusal looks like. Before this fix, this raised an
        # uncaught StopIteration.
        mock_create.return_value = SimpleNamespace(content=[], stop_reason="refusal")
        signal = get_agent_response(PROFILE, "plan lunch", None, 0, 4)

        self.assertEqual(signal.stance, "REJECT")

    @patch("choir.engine.agent.client.messages.create")
    def test_empty_content_without_refusal_falls_back_to_reject(self, mock_create):
        # Belt-and-suspenders: no text block at all, for any reason, must not
        # raise StopIteration regardless of stop_reason.
        mock_create.return_value = SimpleNamespace(content=[], stop_reason="end_turn")
        signal = get_agent_response(PROFILE, "plan lunch", None, 0, 4)

        self.assertEqual(signal.stance, "REJECT")

    @patch("choir.engine.agent.client.messages.create")
    def test_truncated_json_falls_back_to_reject_instead_of_raising(self, mock_create):
        # Simulates stop_reason == "max_tokens" cutting the JSON off mid-object.
        mock_create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"stance": "ACCEPT", "reason": "fi')],
            stop_reason="max_tokens",
        )
        signal = get_agent_response(PROFILE, "plan lunch", "Cafe X", 0, 4)

        self.assertEqual(signal.stance, "REJECT")
        self.assertIn("parse", signal.reason)

    @patch("choir.engine.agent.client.messages.create")
    def test_missing_required_key_falls_back_to_reject_instead_of_raising(self, mock_create):
        # Valid JSON, but missing a key the schema should have required —
        # defense in depth against a future API/schema mismatch.
        mock_create.return_value = text_response({"stance": "ACCEPT"})
        signal = get_agent_response(PROFILE, "plan lunch", "Cafe X", 0, 4)

        self.assertEqual(signal.stance, "REJECT")
        self.assertIn("parse", signal.reason)

    @patch("choir.engine.agent.client.messages.create")
    def test_real_candidates_are_sent_as_a_constraining_enum(self, mock_create):
        # This is the mechanism that makes venue-grounding real rather than
        # cosmetic: when candidates exist, the API request itself constrains
        # counter_proposal to those exact names — the model is structurally
        # unable to invent a venue, not just asked nicely not to.
        mock_create.return_value = text_response(
            {"stance": "COUNTER", "reason": "closer", "counter_proposal": "Cafe A"}
        )
        candidates = [
            VenueResult(name="Cafe A", address="HSR", rating=0, price_level=0),
            VenueResult(name="Cafe B", address="Indiranagar", rating=0, price_level=0),
        ]
        get_agent_response(PROFILE, "plan lunch", None, 0, 4, candidates)

        sent_schema = mock_create.call_args.kwargs["output_config"]["format"]["schema"]
        self.assertEqual(set(sent_schema["properties"]["counter_proposal"]["enum"]), {"Cafe A", "Cafe B", None})

    @patch("choir.engine.agent.client.messages.create")
    def test_other_signals_are_summarized_into_the_prompt(self, mock_create):
        mock_create.return_value = text_response(
            {"stance": "COUNTER", "reason": "compromise", "counter_proposal": "Cafe Mid"}
        )
        other_signals = [
            AgentSignal(user_id=2, stance="COUNTER", reason="too far east", counter_proposal="Whitefield"),
        ]
        get_agent_response(PROFILE, "plan lunch", "Cafe A", 1, 4, other_signals=other_signals)

        system_prompt = mock_create.call_args.kwargs["system"]
        self.assertIn("What everyone else in the group said last round:", system_prompt)
        self.assertIn("too far east", system_prompt)
        self.assertIn("Whitefield", system_prompt)

    @patch("choir.engine.agent.client.messages.create")
    def test_no_other_signals_omits_the_summary_block(self, mock_create):
        mock_create.return_value = text_response(
            {"stance": "COUNTER", "reason": "fine", "counter_proposal": "Cafe A"}
        )
        get_agent_response(PROFILE, "plan lunch", None, 0, 4)

        system_prompt = mock_create.call_args.kwargs["system"]
        self.assertNotIn("What everyone else in the group said last round:", system_prompt)


class TestBuildResponseSchema(unittest.TestCase):
    def test_no_candidates_falls_back_to_free_text(self):
        schema = _build_response_schema([])
        self.assertIn("anyOf", schema["properties"]["counter_proposal"])

    def test_candidates_constrain_to_an_exact_enum(self):
        schema = _build_response_schema(["Cafe A", "Cafe B"])
        self.assertEqual(set(schema["properties"]["counter_proposal"]["enum"]), {"Cafe A", "Cafe B", None})


if __name__ == "__main__":
    unittest.main()
