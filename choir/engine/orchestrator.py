from typing import Callable, Optional

from choir.schemas import NegotiationRequest, NegotiationResult, AgentSignal

OnRound = Callable[[int, list[AgentSignal]], None]


def run_negotiation(request: NegotiationRequest, on_round: Optional[OnRound] = None) -> NegotiationResult:
    """
    Person A's negotiation orchestrator — runs rounds, checks convergence,
    builds the final outcome. See plan.md Phase 1.2 for the spec.

    Signature is the contract the bot layer depends on: takes a
    NegotiationRequest and an optional on_round(round_num, signals) callback
    for live status updates, returns a NegotiationResult. Keep this shape
    stable when implementing — the bot layer is already wired against it.
    """
    raise NotImplementedError("negotiation engine (Person A) not built yet")
