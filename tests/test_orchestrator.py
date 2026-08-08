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

from choir.engine.orchestrator import (
    _round_budget,
    _select_next_proposal,
    _verify_decision,
    format_transcript,
    run_negotiation,
)
from choir.schemas import AgentSignal, NegotiationRequest, UserProfile, VenueResult

USER_A = UserProfile(telegram_user_id=1, budget_min=100, budget_max=300, preferences=[], area="HSR")
USER_B = UserProfile(telegram_user_id=2, budget_min=200, budget_max=600, preferences=[], area="HSR")
USER_C = UserProfile(telegram_user_id=3, budget_min=150, budget_max=500, preferences=[], area="HSR")


def scripted(responses: dict[tuple[int, str | None], AgentSignal]):
    """Builds a get_agent_response stand-in keyed by (user_id, current_proposal),
    so each test can spell out exactly what every person says on every round."""

    def fake(profile, goal_text, current_proposal, round_num=0, max_rounds=1, candidates=None):
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
        # rounds is the full transcript, always populated regardless of on_round
        self.assertEqual(len(result.rounds), 2)
        self.assertEqual(result.rounds[0][0].stance, "COUNTER")
        self.assertEqual(result.rounds[1][1].stance, "ACCEPT")

    @patch("choir.engine.orchestrator.get_agent_response")
    def test_format_transcript_reads_back_the_full_negotiation(self, mock_get):
        mock_get.side_effect = scripted({
            (1, None): AgentSignal(user_id=1, stance="COUNTER", reason="fits my budget", counter_proposal="Cafe A"),
            (2, None): AgentSignal(user_id=2, stance="REJECT", reason="want to see other options"),
            (1, "Cafe A"): AgentSignal(user_id=1, stance="ACCEPT", reason="still works"),
            (2, "Cafe A"): AgentSignal(user_id=2, stance="ACCEPT", reason="fine by me"),
        })

        result = run_negotiation(
            NegotiationRequest(group_chat_id=0, goal_text="dinner", profiles=[USER_A, USER_B])
        )
        transcript = format_transcript(result.rounds)

        self.assertIn("Round 1:", transcript)
        self.assertIn("Round 2:", transcript)
        self.assertIn("proposes: Cafe A", transcript)
        self.assertIn("2: ACCEPT — fine by me", transcript)

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

    @patch("choir.engine.orchestrator.get_agent_response")
    def test_top_options_dedup_ignores_case_and_whitespace_differences(self, mock_get):
        # "Cafe A, HSR" and "cafe a,  hsr" describe the same free-text
        # proposal — before normalization these were treated as two
        # different options, inflating (and confusing) the non-convergence
        # report with what's really one repeated suggestion.
        mock_get.side_effect = scripted({
            (1, None): AgentSignal(user_id=1, stance="COUNTER", reason="r1", counter_proposal="Cafe A, HSR"),
            (2, None): AgentSignal(user_id=2, stance="REJECT", reason="r1"),
            (1, "Cafe A, HSR"): AgentSignal(user_id=1, stance="REJECT", reason="r2"),
            (2, "Cafe A, HSR"): AgentSignal(user_id=2, stance="COUNTER", reason="r2", counter_proposal="cafe a,  hsr"),
            (1, "cafe a,  hsr"): AgentSignal(user_id=1, stance="REJECT", reason="r3"),
            (2, "cafe a,  hsr"): AgentSignal(user_id=2, stance="REJECT", reason="r3"),
        })

        result = run_negotiation(
            NegotiationRequest(group_chat_id=0, goal_text="dinner", profiles=[USER_A, USER_B])
        )

        self.assertFalse(result.converged)
        # Only the first-seen spelling is kept, not both variants.
        self.assertEqual(result.top_options, ["Cafe A, HSR"])

    @patch("choir.engine.orchestrator.get_agent_response")
    def test_passes_round_context_so_agents_can_become_more_willing_to_compromise(self, mock_get):
        # Regression test for a real failure mode caught on a live run: without
        # knowing how many rounds are left, agents kept re-countering with
        # their own favorite option indefinitely and never converged even on
        # a reasonable conflict. get_agent_response needs (round_num, max_rounds)
        # so its prompt can make holding-out have a real, escalating cost.
        seen_calls: list[tuple[str | None, int, int]] = []

        def fake(profile, goal_text, current_proposal, round_num, max_rounds, candidates=None):
            seen_calls.append((current_proposal, round_num, max_rounds))
            # Always COUNTER with the same option — keeps the loop alive for
            # the full MAX_ROUNDS without ever converging, so every round
            # actually runs and we can check round_num incremented correctly.
            return AgentSignal(user_id=profile.telegram_user_id, stance="COUNTER", reason="testing", counter_proposal="Option X")

        mock_get.side_effect = fake

        run_negotiation(NegotiationRequest(group_chat_id=0, goal_text="dinner", profiles=[USER_A, USER_B]))

        # 2 people x 4 rounds, round_num incrementing 0..3, max_rounds always 4
        self.assertEqual(len(seen_calls), 8)
        self.assertEqual([c[1] for c in seen_calls], [0, 0, 1, 1, 2, 2, 3, 3])
        self.assertTrue(all(c[2] == 4 for c in seen_calls))

    @patch("choir.engine.orchestrator.get_agent_response")
    def test_majority_acceptance_survives_a_lone_holdouts_counter(self, mock_get):
        # Integration-level regression test for a real failure mode caught on
        # a live run: 2 of 3 people accepted a proposal, but the orchestrator
        # abandoned it anyway because the third person countered, so nobody's
        # agreement ever "stuck" long enough to reach a unanimous round. With
        # the fix, majority support should survive into round 3, giving the
        # holdout a chance to come around instead of the group chasing them.
        #
        # Round 2 and round 3 both have current_proposal == "X" for users 1/2,
        # so the shared scripted() helper (keyed by (user_id, current_proposal))
        # can't distinguish them — this needs round_num too, hence a bespoke
        # stateful fake instead.
        def fake(profile, goal_text, current_proposal, round_num, max_rounds, candidates=None):
            if current_proposal is None:
                return {
                    1: AgentSignal(user_id=1, stance="COUNTER", reason="r1", counter_proposal="X"),
                    2: AgentSignal(user_id=2, stance="COUNTER", reason="r1", counter_proposal="Y"),
                    3: AgentSignal(user_id=3, stance="REJECT", reason="r1"),
                }[profile.telegram_user_id]
            if current_proposal == "X" and round_num == 1:
                return {
                    1: AgentSignal(user_id=1, stance="ACCEPT", reason="r2"),
                    2: AgentSignal(user_id=2, stance="ACCEPT", reason="r2"),
                    3: AgentSignal(user_id=3, stance="COUNTER", reason="r2", counter_proposal="Y"),
                }[profile.telegram_user_id]
            # Round 3: everyone finally converges on X, now that it survived
            # the holdout's round-2 counter instead of being abandoned for Y.
            return AgentSignal(user_id=profile.telegram_user_id, stance="ACCEPT", reason="r3")

        mock_get.side_effect = fake

        result = run_negotiation(
            NegotiationRequest(group_chat_id=0, goal_text="dinner", profiles=[USER_A, USER_B, USER_C])
        )

        self.assertTrue(result.converged)
        self.assertEqual(result.decision, "X")


class TestSelectNextProposal(unittest.TestCase):
    def test_keeps_current_proposal_when_majority_already_accepts(self):
        signals = [
            AgentSignal(user_id=1, stance="ACCEPT", reason="fine"),
            AgentSignal(user_id=2, stance="ACCEPT", reason="fine"),
            AgentSignal(user_id=3, stance="COUNTER", reason="nah", counter_proposal="Cafe Z"),
        ]
        self.assertEqual(_select_next_proposal("Cafe A", signals), "Cafe A")

    def test_switches_to_counter_when_no_majority(self):
        signals = [
            AgentSignal(user_id=1, stance="ACCEPT", reason="fine"),
            AgentSignal(user_id=2, stance="COUNTER", reason="nah", counter_proposal="Cafe Z"),
            AgentSignal(user_id=3, stance="COUNTER", reason="nah", counter_proposal="Cafe Y"),
        ]
        self.assertEqual(_select_next_proposal("Cafe A", signals), "Cafe Z")

    def test_first_round_takes_first_counter(self):
        signals = [
            AgentSignal(user_id=1, stance="COUNTER", reason="", counter_proposal="Cafe A"),
            AgentSignal(user_id=2, stance="REJECT", reason=""),
        ]
        self.assertEqual(_select_next_proposal(None, signals), "Cafe A")

    def test_no_counters_and_nothing_proposed_yet_stays_none(self):
        signals = [
            AgentSignal(user_id=1, stance="REJECT", reason=""),
            AgentSignal(user_id=2, stance="REJECT", reason=""),
        ]
        self.assertIsNone(_select_next_proposal(None, signals))

    def test_tied_vote_is_not_a_majority(self):
        # 1 of 2 accepting is exactly half, not a majority — should still
        # switch to the counter rather than getting stuck.
        signals = [
            AgentSignal(user_id=1, stance="ACCEPT", reason="fine"),
            AgentSignal(user_id=2, stance="COUNTER", reason="nah", counter_proposal="Cafe Z"),
        ]
        self.assertEqual(_select_next_proposal("Cafe A", signals), "Cafe Z")


class TestVerifyDecision(unittest.TestCase):
    """_verify_decision is the deterministic hard-constraint check — real
    haversine distance against real geocoded coordinates, no LLM call, and
    the negotiation loop cannot converge without passing it once candidates
    exist. These use real-world-scale coordinates (Bengaluru), not toy
    numbers, so the distances are meaningful."""

    def setUp(self):
        self.hsr_venue = VenueResult(name="Cafe X", address="HSR", rating=0, price_level=0, lat=12.90, lon=77.60)
        self.hsr_and_indiranagar = {"HSR": (12.90, 77.60), "Indiranagar": (12.97, 77.64)}  # ~8km apart in reality

    def test_passes_with_no_candidates(self):
        # Ungrounded / free-text mode — nothing real to check against.
        self.assertTrue(_verify_decision("anything", [], [USER_A], {}))

    def test_passes_when_decision_does_not_match_a_real_candidate(self):
        self.assertTrue(_verify_decision("some free text", [self.hsr_venue], [USER_A], {}))

    def test_passes_when_venue_is_within_range_of_everyone(self):
        profiles = [
            UserProfile(telegram_user_id=1, budget_min=100, budget_max=500, preferences=[], area="HSR"),
            UserProfile(telegram_user_id=2, budget_min=100, budget_max=500, preferences=[], area="Indiranagar"),
        ]
        self.assertTrue(_verify_decision("Cafe X", [self.hsr_venue], profiles, self.hsr_and_indiranagar))

    def test_fails_when_venue_is_genuinely_far_from_someone(self):
        # ~0.25 degrees latitude north of HSR — roughly 28km, well past the
        # 15km cutoff. This is exactly the case the check exists to catch.
        far_venue = VenueResult(name="Far Cafe", address="far away", rating=0, price_level=0, lat=13.15, lon=77.60)
        profiles = [UserProfile(telegram_user_id=1, budget_min=100, budget_max=500, preferences=[], area="HSR")]
        self.assertFalse(_verify_decision("Far Cafe", [far_venue], profiles, {"HSR": (12.90, 77.60)}))

    def test_skips_a_profile_whose_area_never_geocoded(self):
        # Can't verify is not the same as violates -- a geocoding miss for
        # one person's area must not block the whole negotiation.
        profiles = [UserProfile(telegram_user_id=1, budget_min=100, budget_max=500, preferences=[], area="Nowhere")]
        self.assertTrue(_verify_decision("Cafe X", [self.hsr_venue], profiles, {}))


class TestVenueGroundedNegotiation(unittest.TestCase):
    """Integration tests: _fetch_venue_context is mocked as a unit (it's pure
    network I/O — already covered by tests/test_places.py), everything
    downstream of it — schema-constrained proposals, majority-preservation,
    and the deterministic override — runs for real."""

    @patch("choir.engine.orchestrator._fetch_venue_context")
    @patch("choir.engine.orchestrator.get_agent_response")
    def test_converges_on_a_real_venue_and_attaches_decided_venue(self, mock_get, mock_fetch):
        venue = VenueResult(name="Real Cafe", address="HSR", rating=0, price_level=0, lat=12.90, lon=77.60)
        mock_fetch.return_value = ([venue], {"HSR": (12.90, 77.60)})
        mock_get.side_effect = scripted({
            (1, None): AgentSignal(user_id=1, stance="COUNTER", reason="fits", counter_proposal="Real Cafe"),
            (2, None): AgentSignal(user_id=2, stance="ACCEPT", reason="fine"),
            (1, "Real Cafe"): AgentSignal(user_id=1, stance="ACCEPT", reason="great"),
            (2, "Real Cafe"): AgentSignal(user_id=2, stance="ACCEPT", reason="great"),
        })

        result = run_negotiation(NegotiationRequest(group_chat_id=0, goal_text="lunch", profiles=[USER_A, USER_B]))

        self.assertTrue(result.converged)
        self.assertEqual(result.decision, "Real Cafe")
        self.assertIsNotNone(result.decided_venue)
        self.assertEqual(result.decided_venue.name, "Real Cafe")

    @patch("choir.engine.orchestrator._fetch_venue_context")
    @patch("choir.engine.orchestrator.get_agent_response")
    def test_deterministic_check_overrides_a_unanimous_but_too_far_decision(self, mock_get, mock_fetch):
        far_venue = VenueResult(name="Far Cafe", address="far away", rating=0, price_level=0, lat=13.15, lon=77.60)
        mock_fetch.return_value = ([far_venue], {"HSR": (12.90, 77.60)})
        mock_get.side_effect = scripted({
            (1, None): AgentSignal(user_id=1, stance="COUNTER", reason="ok", counter_proposal="Far Cafe"),
            (2, None): AgentSignal(user_id=2, stance="ACCEPT", reason="fine"),
            (1, "Far Cafe"): AgentSignal(user_id=1, stance="ACCEPT", reason="great"),
            (2, "Far Cafe"): AgentSignal(user_id=2, stance="ACCEPT", reason="great"),
        })

        result = run_negotiation(NegotiationRequest(group_chat_id=0, goal_text="lunch", profiles=[USER_A, USER_B]))

        # Both agents ACCEPTed unanimously, but the venue is ~28km from HSR —
        # the deterministic check must override the LLM's "unanimous" claim
        # rather than trust it, and report this honestly as non-convergence.
        self.assertFalse(result.converged)
        self.assertIsNone(result.decision)
        self.assertIsNone(result.decided_venue)


class TestRoundBudget(unittest.TestCase):
    def test_small_groups_get_the_floor(self):
        # Unchanged behavior for the common case — every existing test above
        # that hardcodes "4 rounds" for a 2-person negotiation depends on this.
        self.assertEqual(_round_budget(1), 4)
        self.assertEqual(_round_budget(2), 4)

    def test_larger_groups_get_more_rounds(self):
        self.assertEqual(_round_budget(3), 5)
        self.assertEqual(_round_budget(5), 7)

    def test_very_large_groups_are_capped(self):
        self.assertEqual(_round_budget(20), 8)


if __name__ == "__main__":
    unittest.main()
