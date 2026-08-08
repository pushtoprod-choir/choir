"""Best-effort venue enrichment via a single batched Claude + web_search call.

Purely additive on top of choir/venues/places.py's LocationIQ results, which
already work fine on their own. This must never crash, block, or otherwise
affect what the bot sends to the group — any failure here (timeout, API
error, bad response shape) falls back to the original unenriched venue list.
"""
import json
import logging
import re

from anthropic import Anthropic

from choir.schemas import VenueResult

logger = logging.getLogger(__name__)

client = Anthropic()

MODEL = "claude-haiku-4-5"
# MODEL = "claude-sonnet-5"

# 8s was the original value but empirically times out with web_search across
# more than one venue — a single-venue lookup alone took ~10s in testing.
ENRICH_TIMEOUT_SECONDS = 20

# web_search grounding sometimes wraps cited claims in <cite>...</cite> even
# though we never enabled the citations feature — strip it so raw markup
# doesn't leak into the note shown in the group chat. output_config.format
# below guarantees the response's JSON *shape*, not what's inside the string
# values, so this is still needed on top of it.
_CITE_PAIR_RE = re.compile(r"<cite[^>]*>(.*?)</cite>", re.DOTALL)
_CITE_STRAY_RE = re.compile(r"</?cite[^>]*>")


def _strip_citations(text: str) -> str:
    text = _CITE_PAIR_RE.sub(r"\1", text)
    return _CITE_STRAY_RE.sub("", text).strip()

# output_config.format guarantees the response is exactly this shape — this
# replaces manually stripping ```json fences and hoping the model didn't add
# commentary around the array, which was the same brittle-parsing pattern
# agent.py deliberately avoids elsewhere in this codebase.
NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "required": ["note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["notes"],
    "additionalProperties": False,
}


def enrich_venues(venues: list[VenueResult], purpose: str) -> list[VenueResult]:
    if not venues:
        return venues

    venue_lines = "\n".join(f"{i}. {v.name} — {v.address}" for i, v in enumerate(venues))
    prompt = (
        f"Look up each of these {purpose.replace('_', ' ')} venues and write one short "
        "phrase per venue covering rating, price, and vibe if you can find it. Use an "
        "empty string for a venue if you can't find anything useful. The note itself must "
        "be just the phrase — do not repeat the venue's number or name in it.\n\n"
        f"{venue_lines}"
    )

    try:
        response = client.with_options(timeout=ENRICH_TIMEOUT_SECONDS, max_retries=0).messages.create(
            model=MODEL,
            max_tokens=1024,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            output_config={"format": {"type": "json_schema", "schema": NOTE_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )

        text = next(block.text for block in response.content if block.type == "text")
        notes = json.loads(text)["notes"]
        if len(notes) != len(venues):
            raise ValueError(f"expected {len(venues)} notes, got {len(notes)}")

        result = []
        for v, note in zip(venues, notes):
            note_text = note.get("note") or None
            if note_text:
                note_text = _strip_citations(note_text) or None
            result.append(VenueResult(
                name=v.name,
                address=v.address,
                rating=v.rating,
                price_level=v.price_level,
                lat=v.lat,
                lon=v.lon,
                note=note_text,
            ))
        return result
    except Exception:
        logger.exception("Venue enrichment failed; falling back to unenriched venues")
        return venues
