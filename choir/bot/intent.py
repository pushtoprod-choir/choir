"""Cheap intent gate for /choir — decides whether the text after the command
is an actual outing-planning request before spinning up the full negotiation
loop, so "/choir what's up" doesn't get treated as a real ask to plan lunch.

Best-effort in the opposite direction from choir/venues/enrichment.py: if this
classification call fails for any reason, we fail OPEN (treat it as a genuine
planning request) rather than silently swallowing someone's real ask just
because a quick classifier hiccuped.
"""
import json
import logging

from anthropic import Anthropic

logger = logging.getLogger(__name__)

client = Anthropic()

MODEL = "claude-haiku-4-5"

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_planning_request": {"type": "boolean"},
    },
    "required": ["is_planning_request"],
    "additionalProperties": False,
}


def is_planning_request(goal_text: str) -> bool:
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=64,
            system=(
                "Decide whether the following message is a genuine request to plan a group "
                "outing (e.g. lunch, dinner, drinks, an activity, a hangout) — as opposed to "
                "casual chit-chat, a greeting, or anything else that isn't actually asking to "
                "plan something."
            ),
            thinking={"type": "disabled"},
            output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
            messages=[{"role": "user", "content": goal_text}],
        )
        text = next(block.text for block in response.content if block.type == "text")
        return json.loads(text)["is_planning_request"]
    except Exception:
        logger.exception("Intent classification failed; defaulting to treating it as a planning request")
        return True
