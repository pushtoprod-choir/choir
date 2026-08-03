"""Offline unit tests for the negotiation orchestrator — no API key needed.

These replace choir.engine.agent.get_agent_response with a scripted stand-in,
so they test the round/convergence LOGIC in orchestrator.py in isolation from
whatever Claude actually says. That's deliberate: LLM behavior needs a live
key to test (see test_engine.py), but bugs in the round-counting, convergence-
detection, and top-options logic are pure Python bugs you can catch for free.

Run: python -m unittest tests/test_orchestrator.py -v
"""
import unittest
from unittest.mock import patch

from choir.engine.orchestrator import run_negotiation
from choir.schemas import AgentSignal, NegotiationRequest, UserProfile

USER_A = UserProfile(telegram_user_id=1, budget_min=100, budget_max=300, preferences=[], area="HSR")
USER_B = UserProfile(telegram_user_id=2, budget_min=200, budget_max=600, preferences=[], area="HSR")


def scripted(responses: dict[tuple[int, str | None], AgentSignal]):
    """Builds a get_agent_response stand-in keyed by (user_id, current_proposal),
    so each test can spell out exactly what every person says on every round."""

    def fake(profile, goal_text, current_proposal):
        return responses[(profile.telegram_user_id, current_proposal)]

    return fake


class TestOrchestrator(unittest.TestCase):
    @patch("choir.engine.orchestrator.get_agent_response")
    def test_converges_after_a_counter_is_accepted(self, mock_get):
        mock_get.side_effect = scripted({
            (1, None): AgentSignal(user_id=1, stance="COUNTER", reason="fits my budget", counter_proposal="Cafe A"),
            (2, None): AgentSignal(user_id=2, stance="REJECT", reason="want to see other options"),
            (1, "Cafe A"): AgentSignal(user_id=1, stance="ACCEPT", reason="still works"),
            (2, "Cafe A"): AgentSignal(user_id=2, stance="ACCEPT", reason="fine by me"),
        })

        rounds_seen = []
        result = run_negotiation(
            NegotiationRequest(group_chat_id=0, goal_text="dinner", profiles=[USER_A, USER_B]),
            on_round=lambda round_num, signals: rounds_seen.append((round_num, len(signals))),
        )

        self.assertTrue(result.converged)
        self.assertEqual(result.decision, "Cafe A")
        self.assertEqual(result.tradeoffs, ["still works", "fine by me"])
        # on_round fired once per round actually run (2 here), each with both signals
        self.assertEqual(rounds_seen, [(0, 2), (1, 2)])

    @patch("choir.engine.orchestrator.get_agent_response")
    def test_round_one_all_accept_does_not_falsely_converge(self, mock_get):
        # Regression test for the bug this exercise caught: if the model ever
        # ignores the "no proposal yet, you can't ACCEPT" instruction and every
        # agent replies ACCEPT before anything is on the table, the engine must
        # NOT report "converged" on a None decision.
        mock_get.side_effect = scripted({
            (1, None): AgentSignal(user_id=1, stance="ACCEPT", reason="sure, whatever"),
            (2, None): AgentSignal(user_id=2, stance="ACCEPT", reason="works for me"),
        })

        result = run_negotiation(
            NegotiationRequest(group_chat_id=0, goal_text="dinner", profiles=[USER_A, USER_B])
        )

        self.assertFalse(result.converged)
        self.assertIsNone(result.decision)
        self.assertEqual(result.top_options, [])

    @patch("choir.engine.orchestrator.get_agent_response")
    def test_non_convergence_dedups_and_caps_top_options(self, mock_get):
        mock_get.side_effect = scripted({
            (1, None): AgentSignal(user_id=1, stance="COUNTER", reason="r1", counter_proposal="Cafe A"),
            (2, None): AgentSignal(user_id=2, stance="COUNTER", reason="r1", counter_proposal="Cafe B"),
            (1, "Cafe A"): AgentSignal(user_id=1, stance="REJECT", reason="r2"),
            (2, "Cafe A"): AgentSignal(user_id=2, stance="COUNTER", reason="r2", counter_proposal="Cafe B"),  # repeat
            (1, "Cafe B"): AgentSignal(user_id=1, stance="COUNTER", reason="r3", counter_proposal="Cafe C"),
            (2, "Cafe B"): AgentSignal(user_id=2, stance="REJECT", reason="r3"),
            (1, "Cafe C"): AgentSignal(user_id=1, stance="REJECT", reason="r4"),
            (2, "Cafe C"): AgentSignal(user_id=2, stance="REJECT", reason="r4"),
        })

        result = run_negotiation(
            NegotiationRequest(group_chat_id=0, goal_text="dinner", profiles=[USER_A, USER_B])
        )

        self.assertFalse(result.converged)
        # "Cafe A" and "Cafe B" surfaced first (round 1); "Cafe B" repeating in
        # round 2 must not duplicate; "Cafe C" (round 3) is cut off by the cap.
        self.assertEqual(result.top_options, ["Cafe A", "Cafe B"])
        self.assertEqual(mock_get.call_count, 8)  # exactly 4 rounds x 2 people


if __name__ == "__main__":
    unittest.main()
