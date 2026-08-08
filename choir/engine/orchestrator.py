"""Person A's negotiation orchestrator — runs rounds, checks convergence,
builds the final outcome. See plan.md Phase 1.2.

Signature is the contract the bot layer depends on (choir/bot/handlers.py):
run_negotiation is synchronous (the bot drives it via asyncio.to_thread since
it makes blocking API calls) and takes an optional on_round(round_num, signals)
callback fired once per round for live status updates. Keep this shape stable.
"""
from typing import Callable, Optional

from choir.engine.agent import get_agent_response
from choir.schemas import AgentSignal, NegotiationRequest, NegotiationResult

MAX_ROUNDS = 4

OnRound = Callable[[int, list[AgentSignal]], None]


def run_negotiation(request: NegotiationRequest, on_round: Optional[OnRound] = None) -> NegotiationResult:
    current_proposal: str | None = None
    # Every round's signals, kept around so a non-convergent negotiation can
    # still surface the best options it saw instead of just giving up empty.
    history: list[list[AgentSignal]] = []

    for round_num in range(MAX_ROUNDS):
        signals = [
            get_agent_response(profile, request.goal_text, current_proposal)
            for profile in request.profiles
        ]
        history.append(signals)

        if on_round:
            on_round(round_num, signals)

        # current_proposal is not None is required here: on round 1 nothing is
        # on the table yet, so an ACCEPT from everyone would mean "we all agree
        # on nothing" — a real failure mode if the model ever ignores the "no
        # proposal yet, suggest one" instruction in agent.py's prompt.
        if current_proposal is not None and all(s.stance == "ACCEPT" for s in signals):
            return NegotiationResult(
                converged=True,
                decision=current_proposal,
                explanation=_build_explanation(signals),
                tradeoffs=_build_tradeoffs(signals),
                rounds=history,
            )

        # Take the first counter-proposal as the next thing on the table.
        # Simplest working version — doesn't try to merge or rank multiple
        # counters, it just keeps the negotiation moving round to round.
        counters = [s for s in signals if s.stance == "COUNTER"]
        if counters:
            current_proposal = counters[0].counter_proposal
        elif current_proposal is None:
            # Round 1 and nobody proposed or accepted anything — there's
            # nothing to iterate on, so stop instead of repeating empty rounds.
            break

    # Didn't converge within MAX_ROUNDS — an honest "here are the top options"
    # result, not a fake forced decision nobody actually agreed to.
    return NegotiationResult(
        converged=False,
        decision=None,
        explanation=None,
        top_options=_extract_top_options(history),
        rounds=history,
    )


def _build_explanation(signals: list[AgentSignal]) -> str:
    """One short line for the group message, e.g. the ACCEPT reasons combined."""
    reasons = [s.reason for s in signals if s.reason]
    return "; ".join(reasons[:2])


def _build_tradeoffs(signals: list[AgentSignal]) -> list[str]:
    """Per-person reasons behind the final decision — the receipt the group
    sees under "Why:" in bot/handlers.py, and the clearest proof to a judge
    that the negotiation actually weighed real constraints against each other."""
    return [s.reason for s in signals if s.reason]


def _extract_top_options(history: list[list[AgentSignal]]) -> list[str]:
    """Unique counter-proposals seen across every round, in the order they
    first came up, capped at 2 so the group message stays short."""
    options: list[str] = []
    for round_signals in history:
        for s in round_signals:
            if s.counter_proposal and s.counter_proposal not in options:
                options.append(s.counter_proposal)
    return options[:2]


def format_transcript(rounds: list[list[AgentSignal]], resolve_name: Optional[Callable[[int], str]] = None) -> str:
    """Turns NegotiationResult.rounds into a readable "show your work" block —
    the demo moment that proves the negotiation was real back-and-forth, not
    a single hidden API call. Pure formatting, no I/O by default, so it's
    testable with the same fake AgentSignal objects used everywhere else in
    this engine — resolve_name is an optional injected lookup (e.g. the
    bot layer's get_member_name) so the transcript can show names instead of
    raw user_ids without this module doing any DB access itself."""
    lines = []
    for round_num, signals in enumerate(rounds):
        lines.append(f"Round {round_num + 1}:")
        for s in signals:
            name = resolve_name(s.user_id) if resolve_name else s.user_id
            line = f"  {name}: {s.stance} — {s.reason}"
            if s.counter_proposal:
                line += f" (proposes: {s.counter_proposal})"
            lines.append(line)
    return "\n".join(lines)
