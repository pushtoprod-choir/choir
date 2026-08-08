"""Extracts a concrete calendar date from the /choir plan description — the
one piece of the final decision that's fixed by the user up front rather than
negotiated. Same cheap structured-Claude-call pattern as choir/bot/intent.py,
but fails CLOSED (returns None) on any error: unlike intent classification,
where guessing wrong just means one flaky /choir gets treated as real, a
silently-wrong date here would mean a calendar event on the wrong day, which
is worse than making the user retype the command.
"""
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from anthropic import Anthropic

logger = logging.getLogger(__name__)

client = Anthropic()

MODEL = "claude-haiku-4-5"

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "has_date": {"type": "boolean"},
        "date": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["has_date", "date"],
    "additionalProperties": False,
}


def _local_tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("CHOIR_TIMEZONE", "Asia/Kolkata"))


def extract_plan_date(description: str) -> str | None:
    """Returns an ISO "YYYY-MM-DD" date if `description` names one — either
    explicitly ("15th August", "August 15") or relatively ("tomorrow", "this
    Saturday", "next Friday") — resolved against the current local date.
    Returns None if no date is mentioned at all, if a past date was named
    (planning a trip for a day that's already gone is never what's meant),
    or if the classification call itself fails for any reason — the caller
    treats every None the same way: ask the user to include a date."""
    now = datetime.now(_local_tz())
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=128,
            system=(
                f"Current date: {now.strftime('%A, %Y-%m-%d')} ({_local_tz().key}).\n"
                "Decide whether the following outing-planning request names a specific "
                "date — explicitly (e.g. \"15th August\", \"August 15\") or relatively "
                "(e.g. \"tomorrow\", \"this Saturday\", \"next Friday\"). If it does, "
                "resolve it to an absolute date and set has_date=true and date to that "
                "date in YYYY-MM-DD format. If no date is mentioned at all, set "
                "has_date=false and date to null."
            ),
            thinking={"type": "disabled"},
            output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
            messages=[{"role": "user", "content": description}],
        )
        text = next(block.text for block in response.content if block.type == "text")
        data = json.loads(text)
        if not data["has_date"] or not data["date"]:
            return None
        parsed = datetime.strptime(data["date"], "%Y-%m-%d").date()
        if parsed < now.date():
            logger.warning("Extracted date %s is in the past; treating as no date given", parsed)
            return None
        return parsed.isoformat()
    except Exception:
        logger.exception("Date extraction failed; treating the plan as dateless")
        return None
