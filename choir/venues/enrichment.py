"""Best-effort venue enrichment via a single batched Claude + web_search call.

Purely additive on top of choir/venues/places.py's LocationIQ results, which
already work fine on their own. This must never crash, block, or otherwise
affect what the bot sends to the group — any failure here (timeout, API
error, bad response shape) falls back to the original unenriched venue list.
"""
import json
import logging

from anthropic import Anthropic

from choir.schemas import VenueResult

logger = logging.getLogger(__name__)

client = Anthropic()

MODEL = "not-a-real-model"
# MODEL = "claude-haiku-4-5"
# MODEL = "claude-sonnet-5"

ENRICH_TIMEOUT_SECONDS = 8


def enrich_venues(venues: list[VenueResult], purpose: str) -> list[VenueResult]:
    if not venues:
        return venues

    venue_lines = "\n".join(f"{i}. {v.name} — {v.address}" for i, v in enumerate(venues))
    prompt = (
        f"Look up each of these {purpose.replace('_', ' ')} venues and write one short "
        "phrase per venue covering rating, price, and vibe if you can find it.\n\n"
        f"{venue_lines}\n\n"
        "Respond with ONLY a JSON array, one object per venue in the same order, each "
        'shaped like {"note": "<short phrase>"} — use an empty string for a venue if you '
        "can't find anything useful. No other text."
    )

    try:
        response = client.with_options(timeout=ENRICH_TIMEOUT_SECONDS, max_retries=0).messages.create(
            model=MODEL,
            max_tokens=1024,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )

        text = next(block.text for block in response.content if block.type == "text")
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]

        notes = json.loads(text)
        if not isinstance(notes, list) or len(notes) != len(venues):
            raise ValueError(f"expected {len(venues)} notes, got {notes!r}")

        return [
            VenueResult(
                name=v.name,
                address=v.address,
                rating=v.rating,
                price_level=v.price_level,
                note=(note.get("note") or None) if isinstance(note, dict) else None,
            )
            for v, note in zip(venues, notes)
        ]
    except Exception:
        logger.exception("Venue enrichment failed; falling back to unenriched venues")
        return venues
