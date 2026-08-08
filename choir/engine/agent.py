"""Person A's negotiation engine — single-agent reasoning.

See plan.md Phase 1.1. This module knows nothing about Telegram or SQLite —
it's a pure function: (one person's profile, the group's goal, the current
proposal) -> that person's reaction. The orchestrator (orchestrator.py) is
the only thing that calls this, once per person per round.
"""
import json

import anthropic
from anthropic import Anthropic

from choir.schemas import AgentSignal, UserProfile

client = Anthropic()

MODEL = "claude-haiku-4-5"
# MODEL = "claude-sonnet-5"

# Claude's reply must match this shape exactly. This is why we don't hand-parse
# "ACCEPT: <reason>" strings the way the original plan sketch did — a malformed
# or slightly-off-format model reply was the single most likely demo-day crash,
# and structured outputs make that class of bug impossible instead of just rare.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "stance": {"type": "string", "enum": ["ACCEPT", "REJECT", "COUNTER"]},
        "reason": {"type": "string"},
        # null unless stance is COUNTER — anyOf (not a nullable string type list)
        # because that's the form the API's JSON Schema subset supports.
        "counter_proposal": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["stance", "reason", "counter_proposal"],
    "additionalProperties": False,
}


def get_agent_response(profile: UserProfile, goal_text: str, current_proposal: str | None) -> AgentSignal:
    """
    Represents ONE person's private negotiator reacting to the current state
    of the negotiation. Deliberately sees only `profile` — never any other
    person's budget, preferences, or constraints — and never reveals this
    person's exact numbers back to the group. It only ever hands back a
    stance: ACCEPT, REJECT, or COUNTER, plus a short human-readable reason.
    """
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

Decide your stance on the current proposal:
- ACCEPT only if it genuinely fits this person's budget and preferences. If
  there is no current proposal yet, you cannot ACCEPT — there's nothing to
  accept, so use COUNTER instead.
- REJECT if it clearly violates a hard constraint (budget ceiling, area/travel
  distance) and you don't have a better alternative to offer right now.
- COUNTER when you can suggest a concrete, specific alternative that would
  better fit this person (e.g. a particular venue type, time, or area) —
  in that case "counter_proposal" must describe it in one short phrase.
Otherwise leave "counter_proposal" null.

Also factor in timing. If you have a schedule constraint, say so in your reason.
If proposing a COUNTER, include a suggested time alongside the place."""

    if current_proposal:
        user_message = f"The group wants to: {goal_text}\nCurrent proposal on the table: {current_proposal}"
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
                "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
            },
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError:
        # Network/API failure at a system boundary — fail honestly into a
        # REJECT rather than crashing the whole negotiation for everyone else.
        return AgentSignal(
            user_id=profile.telegram_user_id,
            stance="REJECT",
            reason="couldn't reach the negotiation service",
        )

    text = next(block.text for block in response.content if block.type == "text")
    data = json.loads(text)

    return AgentSignal(
        user_id=profile.telegram_user_id,
        stance=data["stance"],
        reason=data["reason"],
        counter_proposal=data["counter_proposal"],
    )
