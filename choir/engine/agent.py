"""Person A's negotiation engine — single-agent reasoning.

See plan.md Phase 1.1. This module knows nothing about Telegram or SQLite —
it's a pure function: (one person's profile, the group's goal, the current
proposal) -> that person's reaction. The orchestrator (orchestrator.py) is
the only thing that calls this, once per person per round.
"""
import json
import logging

import anthropic
from anthropic import Anthropic

from choir.schemas import AgentSignal, UserProfile, VenueResult

logger = logging.getLogger(__name__)

client = Anthropic()

MODEL = "claude-haiku-4-5"
# MODEL = "claude-sonnet-5"


def _build_response_schema(candidate_names: list[str]) -> dict:
    """When real venue candidates exist, counter_proposal is constrained to
    an exact candidate name (or null) via `enum` — the model is structurally
    unable to invent a venue that isn't real, instead of just being asked
    nicely not to. Without candidates (LocationIQ unconfigured/unavailable),
    falls back to free text so the negotiation still runs, just ungrounded,
    exactly as it did before venue integration existed.

    This is why we don't hand-parse "ACCEPT: <reason>" strings the way the
    original plan sketch did — a malformed or slightly-off-format model
    reply was the single most likely demo-day crash, and structured outputs
    make that class of bug impossible instead of just rare.
    """
    if candidate_names:
        counter_proposal_schema = {"enum": [*candidate_names, None]}
    else:
        # null unless stance is COUNTER — anyOf (not a nullable string type
        # list) because that's the form the API's JSON Schema subset supports.
        counter_proposal_schema = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "stance": {"type": "string", "enum": ["ACCEPT", "REJECT", "COUNTER"]},
            "reason": {"type": "string"},
            "counter_proposal": counter_proposal_schema,
        },
        "required": ["stance", "reason", "counter_proposal"],
        "additionalProperties": False,
    }


def get_agent_response(
    profile: UserProfile,
    goal_text: str,
    current_proposal: str | None,
    round_num: int = 0,
    max_rounds: int = 1,
    candidates: list[VenueResult] | None = None,
) -> AgentSignal:
    """
    Represents ONE person's private negotiator reacting to the current state
    of the negotiation. Deliberately sees only `profile` — never any other
    person's budget, preferences, or constraints — and never reveals this
    person's exact numbers back to the group. It only ever hands back a
    stance: ACCEPT, REJECT, or COUNTER, plus a short human-readable reason.

    round_num/max_rounds make the agent aware it's running out of chances to
    reach agreement — without this, a live run showed agents happily
    re-countering with their own favorite option every single round with no
    incentive to compromise, so the negotiation ran the full round budget and
    never converged even on a perfectly reasonable 3-way conflict. Telling the
    agent explicitly what holding out costs (a real, quantified "no decision
    at all" outcome) is what makes it weigh that tradeoff instead of just
    restating its preference indefinitely.

    candidates are the real, nearby venues (from choir.venues.places) this
    person may counter-propose — see _build_response_schema. A COUNTER is
    never just invented text when candidates exist; it's always one of these.
    """
    candidates = candidates or []
    rounds_left = max_rounds - round_num
    is_final_round = rounds_left <= 1

    if candidates:
        candidate_lines = "\n".join(f"- {v.name} ({v.address})" for v in candidates)
        venues_block = (
            f"\nReal, nearby venues you may counter-propose (pick one of these "
            f"exact names — you cannot invent your own):\n{candidate_lines}\n"
        )
    else:
        venues_block = ""

    system_prompt = f"""You represent one person in a group negotiation over what the group should do.
You know only this person's private preferences below. You never see anyone
else's budget or constraints, and your "reason" is shown to the whole group,
so never restate exact numbers from the budget range below.

Budget range: {profile.budget_min}-{profile.budget_max}
Preferences: {", ".join(profile.preferences) or "none stated"}
Area: {profile.area}
Dietary notes: {profile.dietary_notes or "none"}
Temporary context: {profile.temporary_context or "none"}
Additional notes: {profile.notes or "none"}
Calendar (next 48h): {profile.calendar_busy_text or "not connected - no availability data"}
{venues_block}
This is round {round_num + 1} of {max_rounds}. If the group still hasn't all
agreed on the same thing by the end of round {max_rounds}, NOBODY gets a
decision — the plan falls through entirely, which is worse for this person
than a decent-but-imperfect option they actually agree to. Weigh that real
cost against how strongly you feel about your own preference: it is
reasonable to hold out in early rounds, but you should become meaningfully
more willing to accept a workable option as rounds run out.
{"This is the FINAL round — if the current proposal is even roughly acceptable, ACCEPT it rather than countering again, since countering now guarantees no decision at all." if is_final_round else ""}

Decide your stance on the current proposal:
- ACCEPT if it's a genuinely workable fit for this person's budget and
  preferences — it doesn't have to be their single favorite option, just
  acceptable. If there is no current proposal yet, you cannot ACCEPT —
  there's nothing to accept, so use COUNTER instead.
- REJECT only if it clearly violates a hard constraint (budget ceiling,
  area/travel distance) and you don't have a better alternative to offer
  right now.
- COUNTER with one of the exact candidate venue names above (if any are
  listed) — or, if none were provided, a concrete specific alternative
  described in one short phrase — when it would better fit this person.
Otherwise leave "counter_proposal" null.

Also factor in timing. If you have a schedule constraint, say so in your reason.
If proposing a COUNTER, include a suggested time alongside the place.

If the calendar line above shows a real conflict with the current proposal's
time, treat it like a hard constraint (REJECT or COUNTER with a different
time) — but never restate the other event's title or details in your reason,
same rule as never restating your exact budget numbers."""

    if current_proposal:
        user_message = f"The group wants to: {goal_text}\nCurrent proposal on the table: {current_proposal}"
    elif candidates:
        user_message = f"The group wants to: {goal_text}\nNo proposal yet — pick one of the venues listed above."
    else:
        user_message = f"The group wants to: {goal_text}\nNo proposal yet — suggest one that fits your constraints."

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            # This is a quick single-turn classification-style decision, not an
            # open-ended reasoning task, so we skip thinking for latency — a
            # negotiation round calls this once per person, several times over.
            # No output_config.effort here: Haiku 4.5 doesn't support it at all
            # (400s on any value), unlike Sonnet 5 / Opus-tier — structured
            # outputs alone (the "format" below) are enough for this model.
            thinking={"type": "disabled"},
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": _build_response_schema([v.name for v in candidates]),
                },
            },
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError:
        # Network/API failure at a system boundary — fail honestly into a
        # REJECT rather than crashing the whole negotiation for everyone else.
        logger.warning(
            "Anthropic API error for user %s, round %d", profile.telegram_user_id, round_num, exc_info=True
        )
        return AgentSignal(
            user_id=profile.telegram_user_id,
            stance="REJECT",
            reason="couldn't reach the negotiation service",
        )

    if response.stop_reason == "refusal":
        # HTTP 200, but the safety classifier declined — content may be empty
        # or partial. Not an APIError, so it needs its own branch here.
        logger.warning("Anthropic refused the request for user %s, round %d", profile.telegram_user_id, round_num)
        return AgentSignal(
            user_id=profile.telegram_user_id,
            stance="REJECT",
            reason="couldn't get a clear response this round",
        )

    try:
        # Three distinct ways a response can fail to be the JSON we asked
        # for, none of which are anthropic.APIError and none of which were
        # being caught before: no text block at all (StopIteration), text
        # that isn't valid JSON — e.g. truncated by stop_reason == "max_tokens"
        # (JSONDecodeError), or valid JSON missing an expected key (KeyError).
        # output_config.format makes this rare, not impossible — a live demo
        # is exactly where "rare" becomes "the one time it happens."
        text = next(block.text for block in response.content if block.type == "text")
        data = json.loads(text)
        stance = data["stance"]
        reason = data["reason"]
        counter_proposal = data["counter_proposal"]
    except (StopIteration, json.JSONDecodeError, KeyError, TypeError):
        logger.warning(
            "Malformed agent response for user %s, round %d", profile.telegram_user_id, round_num, exc_info=True
        )
        return AgentSignal(
            user_id=profile.telegram_user_id,
            stance="REJECT",
            reason="couldn't parse a clear response this round",
        )

    return AgentSignal(
        user_id=profile.telegram_user_id,
        stance=stance,
        reason=reason,
        counter_proposal=counter_proposal,
    )
