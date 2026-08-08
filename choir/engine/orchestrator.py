"""Person A's negotiation orchestrator — runs rounds, checks convergence,
builds the final outcome. See plan.md Phase 1.2.

Signature is the contract the bot layer depends on (choir/bot/handlers.py):
run_negotiation is synchronous (the bot drives it via asyncio.to_thread since
it makes blocking API calls) and takes an optional on_round(round_num, signals)
callback fired once per round for live status updates. Keep this shape stable.
"""
import logging
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from choir.engine.agent import get_agent_response
from choir.schemas import AgentSignal, NegotiationRequest, NegotiationResult, UserProfile, VenueResult
from choir.venues.places import build_venue_query, find_venues, geocode_areas, haversine_distance_km

logger = logging.getLogger(__name__)

MIN_ROUNDS = 4
MAX_ROUNDS_CAP = 8


def _round_budget(num_people: int) -> int:
    """More people plausibly need more back-and-forth to reconcile
    conflicting preferences — a fixed 4-round budget was the same for a
    2-person and an 8-person group regardless. Scales with group size,
    floored at MIN_ROUNDS (unchanged behavior for the common 2-3 person
    case) and capped at MAX_ROUNDS_CAP so a large group doesn't turn into an
    impractically long, expensive negotiation — every extra round costs a
    real API call for every person."""
    return min(MAX_ROUNDS_CAP, max(MIN_ROUNDS, num_people + 2))


# Generous same-city cutoff for the deterministic distance check in
# _verify_decision. Deliberately wide: the point is to catch a genuinely
# wrong-side-of-town proposal, not to block legitimate cross-neighborhood
# compromises agents negotiate their way to (e.g. HSR <-> Indiranagar in
# Bengaluru is ~8km and should pass; that gap is exactly the kind of thing
# agents are supposed to work out via preference trade-offs, not something
# a hard constraint should preempt).
MAX_VENUE_DISTANCE_KM = 15.0

OnRound = Callable[[int, list[AgentSignal]], None]


def run_negotiation(request: NegotiationRequest, on_round: Optional[OnRound] = None) -> NegotiationResult:
    started = time.monotonic()
    logger.info(
        "Negotiation started: chat=%s people=%d goal=%r",
        request.group_chat_id, len(request.profiles), request.goal_text,
    )

    candidates, area_coords = _fetch_venue_context(request)
    if candidates:
        logger.info(
            "Negotiating with %d real venue candidates: chat=%s", len(candidates), request.group_chat_id
        )

    max_rounds = _round_budget(len(request.profiles))

    current_proposal: str | None = None
    # Mirrors current_proposal's "on the table" pattern, applied to the
    # meeting time instead of the venue — plan_date is fixed up front (see
    # NegotiationRequest.plan_date), so this is the only other thing that
    # needs converging on. Unlike current_proposal, it's never None once
    # round 0 completes: every agent response always carries a proposed_time.
    current_time: str | None = None
    # Every round's signals, kept around so a non-convergent negotiation can
    # still surface the best options it saw instead of just giving up empty.
    history: list[list[AgentSignal]] = []
    # Previous round's signals, fed into the next round's agent calls so each
    # person can react to what everyone else actually said/wanted instead of
    # blindly restating their own favorite against one flattened proposal.
    previous_signals: list[AgentSignal] | None = None

    for round_num in range(max_rounds):
        round_started = time.monotonic()
        signals = _get_signals_for_round(
            request.profiles, request.goal_text, request.plan_date, current_proposal, current_time,
            round_num, max_rounds, candidates, previous_signals,
        )
        history.append(signals)
        previous_signals = signals
        logger.info(
            "Round %d/%d: chat=%s stances=%s (%.2fs)",
            round_num + 1, max_rounds, request.group_chat_id,
            [s.stance for s in signals], time.monotonic() - round_started,
        )

        if on_round:
            on_round(round_num, signals)

        # current_proposal is not None is required here: on round 1 nothing is
        # on the table yet, so an ACCEPT from everyone would mean "we all agree
        # on nothing" — a real failure mode if the model ever ignores the "no
        # proposal yet, suggest one" instruction in agent.py's prompt.
        if current_proposal is not None and current_time is not None and all(s.stance == "ACCEPT" for s in signals):
            if _verify_decision(current_proposal, candidates, request.profiles, area_coords):
                decided_venue = next((v for v in candidates if v.name == current_proposal), None)
                logger.info(
                    "Negotiation converged: chat=%s rounds=%d decision=%r time=%s total=%.2fs",
                    request.group_chat_id, round_num + 1, current_proposal, current_time, time.monotonic() - started,
                )
                return NegotiationResult(
                    converged=True,
                    decision=current_proposal,
                    explanation=_build_explanation(signals),
                    tradeoffs=_build_tradeoffs(signals),
                    rounds=history,
                    decided_venue=decided_venue,
                    decided_time=current_time,
                )
            # Everyone said ACCEPT, but the deterministic distance check
            # overrode it — don't report a false "unanimous," fall through
            # to the same honest non-convergence path as hitting the round budget.
            logger.warning(
                "Convergence overridden by hard-constraint check: chat=%s decision=%r",
                request.group_chat_id, current_proposal,
            )
            break

        next_proposal = _select_next_proposal(current_proposal, signals)
        if next_proposal is not None:
            current_proposal = next_proposal
        elif current_proposal is None:
            # Round 1 and nobody proposed or accepted anything — there's
            # nothing to iterate on, so stop instead of repeating empty rounds.
            break
        current_time = _select_next_time(current_time, signals)

    # Did not converge within the round budget (or failed the hard-constraint check)
    # — an honest "here are the top options" result, not a fake forced
    # decision nobody actually agreed to.
    logger.info(
        "Negotiation did not converge: chat=%s rounds_run=%d total=%.2fs",
        request.group_chat_id, len(history), time.monotonic() - started,
    )
    return NegotiationResult(
        converged=False,
        decision=None,
        explanation=None,
        top_options=_extract_top_options(history),
        rounds=history,
    )


def _get_signals_for_round(
    profiles: list[UserProfile],
    goal_text: str,
    plan_date: str,
    current_proposal: str | None,
    current_time: str | None,
    round_num: int,
    max_rounds: int,
    candidates: list[VenueResult],
    previous_signals: list[AgentSignal] | None = None,
) -> list[AgentSignal]:
    """Fires one get_agent_response call per profile concurrently instead of
    sequentially — each is an independent blocking HTTP call to Claude, so a
    thread pool turns N sequential round-trips into roughly one round-trip's
    worth of wall-clock time. Previously a 5-person group paid 5x the latency
    of a 1-person one, every single round. Order matches `profiles`, not
    completion order, since futures are submitted and awaited in that order.

    previous_signals is last round's full signal list (None on round 0) —
    each profile gets everyone else's prior signal (its own excluded) as
    other_signals, so this round's decisions can actually respond to the
    real conflict instead of one flattened current_proposal string."""
    with ThreadPoolExecutor(max_workers=max(len(profiles), 1)) as pool:
        futures = [
            pool.submit(
                get_agent_response, profile, goal_text, plan_date, current_proposal, current_time,
                round_num, max_rounds, candidates,
                [s for s in previous_signals if s.user_id != profile.telegram_user_id] if previous_signals else None,
            )
            for profile in profiles
        ]
        return [future.result() for future in futures]


def _fetch_venue_context(
    request: NegotiationRequest,
) -> tuple[list[VenueResult], dict[str, Optional[tuple[float, float]]]]:
    """Real venue candidates this negotiation may propose, plus each area's
    geocoded coordinates for the distance check in _verify_decision. Isolated
    into one function — mockable as a unit in tests — so the round loop
    doesn't need to know LocationIQ exists at all. Returns ([], {}) if
    LOCATION_IQ_API_KEY isn't configured or the lookup fails for any reason:
    the negotiation still runs, just without real venue grounding, exactly
    like before this feature existed."""
    api_key = os.environ.get("LOCATION_IQ_API_KEY")
    if not api_key:
        logger.warning("LOCATION_IQ_API_KEY not set; negotiating without real venue grounding")
        return [], {}
    try:
        areas = list({p.area for p in request.profiles})
        area_coords = geocode_areas(areas, api_key)
        query = build_venue_query(request.profiles, request.goal_text)
        candidates = find_venues(query, api_key, area_coords=area_coords)
        if not candidates:
            logger.warning(
                "No venue candidates found: chat=%s areas=%s area_coords=%s",
                request.group_chat_id, areas, area_coords,
            )
        return candidates, area_coords
    except Exception:
        logger.warning("Venue lookup failed; negotiating without real venue grounding", exc_info=True)
        return [], {}


def _verify_decision(
    decision: str | None,
    candidates: list[VenueResult],
    profiles: list[UserProfile],
    area_coords: dict[str, Optional[tuple[float, float]]],
) -> bool:
    """Deterministic hard-constraint check — no Claude call, and the LLM
    cannot talk its way past it. Confirms the venue every agent just
    accepted is actually within a reasonable distance of EVERY profile's
    stated area, not just close to whoever's counter-proposal happened to
    win. Uses real geocoded coordinates from LocationIQ — not a fabricated
    price/distance estimate.

    Passes trivially (returns True) when there's nothing real to check
    against: no candidates were available, or the decision doesn't match a
    real venue (free-text fallback mode). A profile whose area failed to
    geocode is skipped rather than failing the whole negotiation over an
    unrelated geocoding hiccup — can't verify is not the same as violates.
    """
    if not candidates:
        return True
    venue = next((v for v in candidates if v.name == decision), None)
    if venue is None or not (venue.lat and venue.lon):
        return True

    for profile in profiles:
        coord = area_coords.get(profile.area)
        if coord is None:
            continue
        distance = haversine_distance_km(coord, (venue.lat, venue.lon))
        if distance > MAX_VENUE_DISTANCE_KM:
            logger.warning(
                "Distance check failed: venue=%r is %.1fkm from user %s's area %r (max %.0fkm)",
                decision, distance, profile.telegram_user_id, profile.area, MAX_VENUE_DISTANCE_KM,
            )
            return False
    return True


def _select_next_proposal(current_proposal: str | None, signals: list[AgentSignal]) -> str | None:
    """Decides what's on the table for the next round. Two rules, in order:

    1. If a majority already ACCEPTs the current proposal, keep offering it
       instead of jumping to a lone holdout's counter — a live run showed the
       orchestrator abandoning a proposal 2 of 3 people had already agreed to
       the moment the third person countered, so nobody's agreement ever
       "stuck" long enough to survive to a unanimous round. Majority support
       surviving into the next round gives the holdout a real chance to come
       around under mounting round pressure (see agent.py's round-awareness)
       instead of the group perpetually chasing whoever spoke up last.
    2. Otherwise, table whichever counter-proposal has the most support this
       round (a plurality vote), not just whoever's counter happened to sort
       first in `profiles` order. A live 3-way geographic split showed the
       old "take counters[0]" rule silently dropping the other two people's
       counters every round with no memory, so the tabled proposal bounced
       around based on list order rather than actual group support.

    Falls back to current_proposal unchanged if neither rule applies (e.g.
    everyone REJECTed with no alternative offered) — including staying None
    if nothing has ever been proposed yet.
    """
    if current_proposal is not None:
        accept_count = sum(1 for s in signals if s.stance == "ACCEPT")
        if accept_count > len(signals) / 2:
            return current_proposal

    counters = [s for s in signals if s.stance == "COUNTER" and s.counter_proposal]
    if not counters:
        return current_proposal
    tally = Counter(c.counter_proposal for c in counters)
    top_count = max(tally.values())
    leaders = [name for name, count in tally.items() if count == top_count]
    return leaders[0] if len(leaders) == 1 else counters[0].counter_proposal


def _select_next_time(current_time: str | None, signals: list[AgentSignal]) -> str | None:
    """Same convergence-forming logic as _select_next_proposal, applied to
    the meeting time instead of the venue: majority agreement on the time
    already on the table keeps it tabled (so agreement survives instead of
    bouncing to whoever spoke last), otherwise the plurality of what everyone
    is currently proposing wins the next round's table time. Unlike
    counter_proposal, proposed_time is required on every signal — there's
    always at least one time to tally, so this only returns None if `signals`
    itself is empty, which never happens with a non-empty profile list."""
    times = [s.proposed_time for s in signals if s.proposed_time]
    if not times:
        return current_time

    if current_time is not None:
        agree_count = sum(1 for t in times if t == current_time)
        if agree_count > len(signals) / 2:
            return current_time

    tally = Counter(times)
    top_count = max(tally.values())
    leaders = [t for t, count in tally.items() if count == top_count]
    return leaders[0] if len(leaders) == 1 else times[0]


def _build_explanation(signals: list[AgentSignal]) -> str:
    """One short line for the group message, e.g. the ACCEPT reasons combined."""
    reasons = [s.reason for s in signals if s.reason]
    return "; ".join(reasons[:2])


def _build_tradeoffs(signals: list[AgentSignal]) -> list[str]:
    """Per-person reasons behind the final decision — the receipt the group
    sees under "Why:" in bot/handlers.py, and the clearest proof to a judge
    that the negotiation actually weighed real constraints against each other."""
    return [s.reason for s in signals if s.reason]


def _normalize_option(text: str) -> str:
    """Case/whitespace normalization for dedup purposes only — the original
    text is still what gets displayed. Real venue-grounded proposals (see
    _fetch_venue_context) are exact strings from a shared candidate list and
    dedup perfectly on their own; this only matters for the free-text
    fallback mode, where "Cafe X, HSR" and "cafe x,  hsr" are the same place
    but weren't being recognized as duplicates before."""
    return " ".join(text.strip().lower().split())


def _extract_top_options(history: list[list[AgentSignal]]) -> list[str]:
    """Unique counter-proposals seen across every round, in the order they
    first came up, capped at 2 so the group message stays short."""
    options: list[str] = []
    seen_normalized: set[str] = set()
    for round_signals in history:
        for s in round_signals:
            if not s.counter_proposal:
                continue
            key = _normalize_option(s.counter_proposal)
            if key in seen_normalized:
                continue
            seen_normalized.add(key)
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
            if s.proposed_time:
                line += f" (time: {s.proposed_time})"
            lines.append(line)
    return "\n".join(lines)
