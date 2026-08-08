# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Choir is a Telegram bot where every group member gets a private AI representative that negotiates group decisions (starting with outings) on their behalf, using a stored profile (budget, preferences, area, dietary needs) so nobody has to state real constraints out loud in the group. Built for Push to Prod (Anthropic & Elevation Capital), Bengaluru.

## Commands

### Setup
```bash
python3 -m venv choir-env
choir-env\Scripts\activate      # Windows; source choir-env/bin/activate on Unix
pip install -r requirements.txt
```
Copy `.env.example` to `.env` and fill in `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `LOCATION_IQ_API_KEY`.

### Run the bot
```bash
python main.py
```
Long-polling — no public server/webhook needed. Must be restarted after any code change to pick it up (it's a long-running process, not hot-reloaded).

### Tests
Two different kinds of "test" files exist side by side — know which one you're running:

- **`tests/` — offline, mocked, no API keys needed.** These are what `pytest` actually collects (root-level `test_engine.py`/`test_venues.py` define no `test_*`-prefixed functions, so pytest skips them even though the filenames match the discovery pattern).
  ```bash
  pytest -q                                          # full offline suite
  pytest tests/test_orchestrator.py -q                # one file
  pytest tests/test_orchestrator.py -k test_converges_after_a_counter_is_accepted   # one test
  # or: python -m unittest tests.test_orchestrator -v
  ```
- **Root-level `test_engine.py` / `test_venues.py` — live manual scripts, hit real APIs.** Run directly with `python test_engine.py` / `python test_venues.py`; they need real keys in `.env` and are the "does this actually work end-to-end" check, not part of CI-style runs.

## Architecture

### Request flow
`/choir <goal text>` in a group → `choir/bot/handlers.py:handle_choir_command`:
1. Every group member seen so far (`get_seen_members`, populated by `track_group_member` on every group message) must have a saved profile, or the command bails out and tells them to DM the bot.
2. A cheap intent-classification call (`choir/bot/intent.py`) gates out non-planning messages (e.g. "what's up") before the expensive negotiation runs — fails **open** (treats it as a real request) if the classifier call itself fails, so a flaky call never silently swallows a genuine ask.
3. `choir/engine/orchestrator.py:run_negotiation` drives up to `MAX_ROUNDS` rounds, calling `choir/engine/agent.py:get_agent_response` once per person per round. Each call is a synchronous Anthropic Messages API call with `output_config.format` (structured JSON output) — driven off the main event loop via `asyncio.to_thread`, with a `sync_on_round` shim bridging the sync orchestrator back to the async `on_round` callback that posts live per-round updates to the group.
4. On convergence or exhaustion, `choir/venues/places.py:find_venues` pulls real nearby venues from LocationIQ (geocode-then-nearby-search, midpoint of everyone's stated area), then `choir/venues/enrichment.py:enrich_venues` does one batched Claude+`web_search` call to add a rating/price/vibe note per venue — **best-effort, fails closed** (any error → falls back to the unenriched venue list silently, never blocks the reply).
5. `format_transcript` (in `orchestrator.py`) renders the full round-by-round history as a follow-up message; both this and the live round updates resolve Telegram display names through the same `choir.store.profiles.get_member_name` (injected into `format_transcript` via an optional `resolve_name` callback, so the engine module itself stays DB-free).

### The engine/bot boundary
`choir/engine/` (agent.py, orchestrator.py) has **zero Telegram/SQLite knowledge** — it's pure `(profiles, goal text) -> NegotiationResult`, and `run_negotiation`'s `on_round` callback signature is treated as a stable contract the bot layer depends on. `choir/schemas.py` is the load-bearing shared-contract file — every dataclass there (`UserProfile`, `AgentSignal`, `VenueResult`, etc.) is read by multiple modules across the engine/bot/store/venues boundary, so changes there ripple widely; prefer adding optional fields over changing existing ones. Module docstrings still reference the original hackathon phase split (`agent.py`/`orchestrator.py` = "Person A", `places.py` = "Person C") from `plan.md` — that plan is a historical build-order reference, not current state (the engine is fully implemented, not a stub).

### SQLite schema (`choir/store/profiles.py`)
`choir.db` is git-ignored and has **no migration framework** — `init_db()` is the only schema authority, using `CREATE TABLE IF NOT EXISTS` for new tables and a `try/except sqlite3.OperationalError` around `ALTER TABLE ... ADD COLUMN` for columns added after a table already existed (so it's safe to call against both a fresh DB and an already-populated one). When adding a `UserProfile` field, you need to touch four places in sync: the dataclass in `schemas.py`, the `ALTER TABLE` guard in `init_db()`, the explicit column list in `save_profile`, and the explicit `SELECT` column list + constructor call in `get_profile`.

`seen_members` (`chat_id`, `user_id`, `first_name`) is separate from `profiles` (keyed only on `telegram_user_id`) — it's how the bot knows who's present in which group (to require profiles for) and resolves display names. `first_name` updates are **broadcast across every chat_id row for that user_id** (not scoped to the chat_id in the call), since a person's name doesn't vary per group — this is what lets a name learned in one group (or during onboarding, which only has a DM chat_id) backfill stale `NULL` rows elsewhere.

### Onboarding state machine
`choir/bot/onboarding.py` defines a linear `ConversationHandler` flow (`BUDGET → PREFERENCES → AREA → DIETARY → ANYTHING_ELSE`) whose state constants and handler functions are wired into `main.py`'s `ConversationHandler(states={...})` — the two files must stay in sync when adding/removing a step. It only runs in DMs (`filters.ChatType.PRIVATE`) because Telegram bots can't message a user first; group activity (`track_group_member`) is what triggers the "you haven't set up Choir yet" prompt.

### Import-order gotcha
`choir/engine/agent.py`, `choir/venues/enrichment.py`, and `choir/bot/intent.py` all construct an `Anthropic()` client **at module import time**, so `load_dotenv()` must run before any `choir.*` import — `main.py` and the root-level manual test scripts do this explicitly as the first lines, with a comment explaining why. A standalone script or REPL session that imports `choir.*` before loading env vars will fail non-obviously (or, for the intent classifier and enrichment paths specifically, silently fail *open*/*closed* rather than crash — see their module docstrings).
