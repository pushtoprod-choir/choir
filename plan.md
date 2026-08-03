# Choir — detailed, phase-by-phase build plan

This expands the high-level plan into concrete, buildable steps. Read this alongside `choir-build-plan.md` and `README.md`.

Team mapping used throughout:
- **Person A** — Negotiation engine
- **Person B** — Telegram bot layer + onboarding
- **Person C** — Profile store + venue service

---

## Phase 0 — Shared contracts (do this together, first, before splitting up)

Nobody writes real logic until this file exists and all three of you agree on it. This is the single highest-leverage 20 minutes of the whole project.

Create `choir/schemas.py`:

```python
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class UserProfile:
    telegram_user_id: int
    budget_min: int
    budget_max: int
    preferences: list[str]       # e.g. ["cafe_person", "vegetarian", "quiet_places"]
    area: str                    # e.g. "HSR Layout"
    dietary_notes: Optional[str] = None
    temporary_context: Optional[str] = None   # e.g. "saving money this month" — cleared after each negotiation

@dataclass
class NegotiationRequest:
    group_chat_id: int
    goal_text: str                # raw text after the /choir command, e.g. "plan lunch for us"
    profiles: list[UserProfile]

@dataclass
class AgentSignal:
    user_id: int
    stance: str                   # "ACCEPT" | "REJECT" | "COUNTER"
    reason: str                   # short, human-readable
    counter_proposal: Optional[str] = None

@dataclass
class NegotiationResult:
    converged: bool
    decision: Optional[str]        # e.g. "Cafe Coffee Day, HSR, 7:30pm"
    explanation: Optional[str]     # e.g. "Chose the cafe over the bar — Rahul's saving money this week"
    top_options: list[str] = field(default_factory=list)  # populated only if converged == False

@dataclass
class VenueQuery:
    purpose: str                   # "casual_lunch" | "drinks" | "work_meeting"
    areas: list[str]               # every involved person's stated area
    budget_max: int

@dataclass
class VenueResult:
    name: str
    address: str
    rating: float
    price_level: int
```

**Why dataclasses and not dicts**: typos in dict keys (`profile["buget"]`) fail silently at runtime and are exactly the kind of bug that eats an hour on demo day. Dataclasses fail loudly and immediately if a field is missing or misnamed, which is worth the two extra minutes of typing.

Commit this file before anyone builds anything else. From here, all three phases below can be built in parallel.

---

## Phase 1 — Negotiation engine (Person A)

**Goal for this phase**: a fully working, fully tested negotiation engine that never touches Telegram. You should be able to run it from a plain Python script with fake profiles and get a real result.

### 1.1 — Single-agent function

Create `choir/engine/agent.py`:

```python
from anthropic import Anthropic
from choir.schemas import UserProfile, AgentSignal

client = Anthropic()

def get_agent_response(profile: UserProfile, goal_text: str, current_proposal: str | None) -> AgentSignal:
    """
    Represents ONE person's agent reacting to the current state of the negotiation.
    Never sees other people's profiles — only the current proposal on the table.
    """
    system_prompt = f"""
    You represent a person with these private preferences. You never reveal
    exact numbers to anyone — only ACCEPT, REJECT, or COUNTER with a short reason.

    Budget range: {profile.budget_min} to {profile.budget_max}
    Preferences: {", ".join(profile.preferences)}
    Area: {profile.area}
    Temporary note: {profile.temporary_context or "none"}

    Respond with exactly one of:
    ACCEPT: <short reason>
    REJECT: <short reason>
    COUNTER: <your suggested alternative> | <short reason>
    """

    user_message = f"The group wants to: {goal_text}\n"
    if current_proposal:
        user_message += f"Current proposal on the table: {current_proposal}"
    else:
        user_message += "No proposal yet — suggest one that fits your constraints."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    return _parse_agent_response(profile.telegram_user_id, response.content[0].text)


def _parse_agent_response(user_id: int, raw_text: str) -> AgentSignal:
    # Keep this parser simple and defensive — it's the single most likely place
    # for a demo-day crash if the model's output format drifts slightly.
    if raw_text.startswith("ACCEPT"):
        return AgentSignal(user_id=user_id, stance="ACCEPT", reason=raw_text.split(":", 1)[1].strip())
    if raw_text.startswith("REJECT"):
        return AgentSignal(user_id=user_id, stance="REJECT", reason=raw_text.split(":", 1)[1].strip())
    if raw_text.startswith("COUNTER"):
        body = raw_text.split(":", 1)[1].strip()
        proposal, reason = body.split("|", 1)
        return AgentSignal(user_id=user_id, stance="COUNTER", reason=reason.strip(), counter_proposal=proposal.strip())
    # Fallback if the model didn't follow the format — treat as a reject with a generic reason
    return AgentSignal(user_id=user_id, stance="REJECT", reason="couldn't parse a clear response")
```

**Test this alone first.** Write a throwaway script that calls `get_agent_response` with one fake profile and prints the result. Don't move on until this is reliable.

### 1.2 — Orchestrator

Create `choir/engine/orchestrator.py`:

```python
from choir.schemas import NegotiationRequest, NegotiationResult
from choir.engine.agent import get_agent_response

MAX_ROUNDS = 4

def run_negotiation(request: NegotiationRequest) -> NegotiationResult:
    current_proposal = None
    history = []

    for round_num in range(MAX_ROUNDS):
        signals = [
            get_agent_response(profile, request.goal_text, current_proposal)
            for profile in request.profiles
        ]
        history.append(signals)

        if all(s.stance == "ACCEPT" for s in signals):
            return NegotiationResult(
                converged=True,
                decision=current_proposal,
                explanation=_build_explanation(signals),
            )

        # take the first counter-proposal as the next thing on the table
        counters = [s for s in signals if s.stance == "COUNTER"]
        if counters:
            current_proposal = counters[0].counter_proposal
        elif current_proposal is None:
            # nobody proposed anything yet on round 1 — shouldn't normally happen
            # if it does, treat it as a non-convergence case
            break

    # didn't converge within MAX_ROUNDS — honest fallback, not a fake decision
    return NegotiationResult(
        converged=False,
        decision=None,
        explanation=None,
        top_options=_extract_top_options(history),
    )


def _build_explanation(signals) -> str:
    reasons = [s.reason for s in signals if s.reason]
    return "; ".join(reasons[:2])  # keep it short for the group message


def _extract_top_options(history) -> list[str]:
    # simplest working version: pull unique counter-proposals seen across all rounds
    options = set()
    for round_signals in history:
        for s in round_signals:
            if s.counter_proposal:
                options.add(s.counter_proposal)
    return list(options)[:2]
```

**Test this with 3-4 fake profiles that have a genuine conflict** (one tight budget, one far away, one flexible). Run it end to end and read the actual conversation it produces across rounds — this is where you'll tune the system prompt for realistic negotiation behavior instead of every agent just agreeing immediately.

### 1.3 — What "done" looks like for Phase 1

You can run `python test_engine.py` with hardcoded fake profiles and get either a converged decision with a real explanation, or an honest "didn't converge, here are the top 2 options" result. No Telegram code involved at all.

---

## Phase 2 — Profile store (Person C, first half)

Create `choir/store/profiles.py`. For hackathon speed, SQLite is genuinely fine — don't reach for Postgres.

```python
import sqlite3
import json
from choir.schemas import UserProfile

DB_PATH = "choir.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            telegram_user_id INTEGER PRIMARY KEY,
            budget_min INTEGER,
            budget_max INTEGER,
            preferences TEXT,        -- stored as JSON list
            area TEXT,
            dietary_notes TEXT,
            temporary_context TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_profile(profile: UserProfile):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO profiles VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        profile.telegram_user_id, profile.budget_min, profile.budget_max,
        json.dumps(profile.preferences), profile.area,
        profile.dietary_notes, profile.temporary_context,
    ))
    conn.commit()
    conn.close()

def get_profile(telegram_user_id: int) -> UserProfile | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT * FROM profiles WHERE telegram_user_id = ?", (telegram_user_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return UserProfile(
        telegram_user_id=row[0], budget_min=row[1], budget_max=row[2],
        preferences=json.loads(row[3]), area=row[4],
        dietary_notes=row[5], temporary_context=row[6],
    )

def clear_temporary_context(telegram_user_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE profiles SET temporary_context = NULL WHERE telegram_user_id = ?", (telegram_user_id,))
    conn.commit()
    conn.close()
```

**Test**: save a fake profile, read it back, confirm the round trip works. This should take 15 minutes total, it's the simplest phase — once done, help Person A or B if either is behind schedule.

---

## Phase 3 — Telegram bot layer (Person B)

### 3.1 — Bot setup
1. Message `@BotFather` on Telegram, `/newbot`, get your token
2. Store it as `TELEGRAM_BOT_TOKEN` in `.env`
3. `pip install python-telegram-bot`

### 3.2 — Command detection and routing

Create `choir/bot/handlers.py`:

```python
from telegram import Update
from telegram.ext import ContextTypes
from choir.store.profiles import get_profile
from choir.schemas import NegotiationRequest
from choir.engine.orchestrator import run_negotiation

async def handle_choir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    goal_text = " ".join(context.args)  # everything after "/choir"

    if not goal_text:
        await message.reply_text("Tell me what you want to plan — e.g. /choir plan lunch for us")
        return

    # get every group member's profile
    member_ids = await _get_group_member_ids(update, context)
    profiles = []
    missing_users = []

    for user_id in member_ids:
        profile = get_profile(user_id)
        if profile is None:
            missing_users.append(user_id)
        else:
            profiles.append(profile)

    if missing_users:
        bot_username = context.bot.username
        await message.reply_text(
            f"Some of you haven't set up Choir yet. Tap this and hit Start: "
            f"t.me/{bot_username}"
        )
        return

    request = NegotiationRequest(
        group_chat_id=message.chat_id,
        goal_text=goal_text,
        profiles=profiles,
    )
    result = run_negotiation(request)

    if result.converged:
        await message.reply_text(f"{result.decision}\n\n{result.explanation}")
    else:
        options_text = "\n".join(f"- {opt}" for opt in result.top_options)
        await message.reply_text(f"Couldn't fully agree — here are the top options:\n{options_text}")


async def _get_group_member_ids(update, context) -> list[int]:
    # NOTE: Telegram's Bot API doesn't give you a clean "list all members" call
    # for privacy reasons. For the hackathon demo, the realistic approach is:
    # track user IDs as people send messages in the group (store them in a
    # simple "seen in this chat" table), rather than trying to enumerate
    # membership from the API. Build this small tracking piece as part of 3.2.
    raise NotImplementedError
```

**This is the one non-obvious technical gotcha in the whole project** — flagged in the code comment above on purpose. Telegram doesn't let a bot cleanly list every member of a group. The practical fix for a hackathon: add a message handler that runs on every group message (not just `/choir`) and records "this user_id was seen in this chat_id" to a small table. By the time someone runs `/choir`, you have a real list of who's active in that chat to work from. Build this in the first hour of Phase 3, it's small but everything else depends on it.

### 3.3 — Registering the command

In `main.py`:

```python
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from choir.bot.handlers import handle_choir_command, track_group_member

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("choir", handle_choir_command))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.ALL, track_group_member))
    app.run_polling()
```

### 3.4 — What "done" looks like for Phase 3

In a real Telegram group with test accounts, typing `/choir plan lunch` correctly identifies group members, and either calls the (possibly still-mocked) negotiation engine or tells people to onboard first.

---

## Phase 4 — Onboarding flow (Person B, second half, or Person C once profile store is done)

Use `python-telegram-bot`'s `ConversationHandler` for the multi-step DM flow with inline buttons.

```python
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler, CallbackQueryHandler

BUDGET, PREFERENCES, AREA = range(3)

async def start_onboarding(update, context):
    keyboard = [
        [InlineKeyboardButton("Under ₹500", callback_data="budget_0_500")],
        [InlineKeyboardButton("₹500–800", callback_data="budget_500_800")],
        [InlineKeyboardButton("₹800–1500", callback_data="budget_800_1500")],
    ]
    await update.message.reply_text(
        "Hi, I'm your Choir representative. Quick setup, about 30 seconds.\n\nUsual budget for an outing?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return BUDGET

async def handle_budget(update, context):
    query = update.callback_query
    await query.answer()
    _, low, high = query.data.split("_")
    context.user_data["budget_min"], context.user_data["budget_max"] = int(low), int(high)

    keyboard = [
        [InlineKeyboardButton("Cafe person", callback_data="pref_cafe")],
        [InlineKeyboardButton("Foodie", callback_data="pref_foodie")],
        [InlineKeyboardButton("Budget conscious", callback_data="pref_budget")],
        [InlineKeyboardButton("Done picking", callback_data="pref_done")],
    ]
    await query.edit_message_text("Pick a couple that fit you:", reply_markup=InlineKeyboardMarkup(keyboard))
    return PREFERENCES

# ... handle_preferences (accumulates selections, "Done picking" advances) ...
# ... handle_area (plain text reply: "Where are you usually around?") ...
# ... on final step, build a UserProfile and call save_profile() ...
```

**What "done" looks like**: a test account that's never used the bot can DM it, tap through budget → preferences → area in under 30 seconds, and immediately show up correctly in `get_profile()`.

---

## Phase 5 — Venue service (Person C, second half)

```python
import googlemaps
from choir.schemas import VenueQuery, VenueResult

gmaps = googlemaps.Client(key=GOOGLE_PLACES_API_KEY)

def find_venues(query: VenueQuery) -> list[VenueResult]:
    # Purpose -> search keyword mapping (simplest working version — refine later)
    keyword_map = {
        "casual_lunch": "restaurant",
        "drinks": "bar",
        "work_meeting": "cafe with wifi",
    }
    keyword = keyword_map.get(query.purpose, "restaurant")

    # For the hackathon: geocode each area, average the coordinates as a rough midpoint
    coords = [gmaps.geocode(area)[0]["geometry"]["location"] for area in query.areas]
    midpoint_lat = sum(c["lat"] for c in coords) / len(coords)
    midpoint_lng = sum(c["lng"] for c in coords) / len(coords)

    places = gmaps.places_nearby(
        location=(midpoint_lat, midpoint_lng),
        radius=2000,
        keyword=keyword,
    )

    return [
        VenueResult(
            name=p["name"],
            address=p.get("vicinity", ""),
            rating=p.get("rating", 0),
            price_level=p.get("price_level", 2),
        )
        for p in places["results"][:5]
    ]
```

**If time is short, skip this entirely and hardcode a small list of 4-5 real venues near your demo location** — the negotiation logic is what matters for the demo, not live venue data.

---

## Phase 6 — Integration and demo polish (all three, together)

1. Wire the real (non-mocked) profile store and negotiation engine into the bot layer
2. Pre-onboard your own 3-4 test accounts, don't do live onboarding on stage
3. Write and rehearse one specific scenario with a genuine conflict (e.g. one profile with a tight budget, one profile far from the others) so the negotiation visibly does something non-trivial
4. Record a screen capture of a working run as a fallback in case live Telegram or the API has issues during the actual demo window
5. Time the full demo, aim for under 90 seconds for the live negotiation itself

---

## Phase 7 — Deploy it properly, so your team can actually test with real groups this week

With 5 hours, long polling on a laptop was the right call. With 4+ days, don't keep the bot tied to someone's laptop being open, deploy it somewhere persistent so all three of you (and friends you rope in to test) can hit it in real Telegram groups at any time.

**Simplest option: Railway or Render, free/cheap tier, still using long polling.** You don't need to switch to webhooks just because you're deploying, long polling works fine on a persistent server, it just needs to stay running, which a laptop can't reliably do for four days. Steps:
1. Push the repo to GitHub
2. Connect the repo to Railway (or Render) as a background worker / long-running process, not a web service
3. Set your environment variables (`TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `GOOGLE_PLACES_API_KEY`) in the platform's dashboard, never commit them
4. Deploy, confirm the bot responds in a test group

**Why this matters beyond convenience**: real testing surfaces real bugs, someone's profile with an edge-case budget, a negotiation that spirals past your round cap, a Telegram rate limit you didn't know existed. You want those showing up on Tuesday, not during your live demo on the 8th.

## Phase 8 — Features worth building now that you're not compressed into 5 hours

These were explicitly deferred earlier as "roadmap, not built" because a 5-hour build couldn't responsibly claim them. With real days available, build them for real.

### 8.1 — The fuller memory model (long-term + temporary + confidence)
Extend `UserProfile` with a lightweight confidence score per preference, adjusted after each negotiation based on whether that preference was actually accepted or rejected in the outcome:

```python
@dataclass
class ScoredPreference:
    tag: str
    confidence: float  # 0.0 to 1.0, starts around 0.6 for onboarding-stated preferences

# after a negotiation resolves, nudge confidence up/down based on outcome
def update_confidence(profile, accepted_tags: list[str], rejected_tags: list[str]):
    for pref in profile.scored_preferences:
        if pref.tag in accepted_tags:
            pref.confidence = min(1.0, pref.confidence + 0.05)
        elif pref.tag in rejected_tags:
            pref.confidence = max(0.0, pref.confidence - 0.05)
```
Keep the adjustment step small and boring, this doesn't need to be sophisticated to feel smart in a demo, it needs to visibly shift over a few real negotiations.

### 8.2 — MCP calendar integration (the piece skipped before due to OAuth overhead)
Now that you have days, not hours, the OAuth consent flow is worth doing properly. Use a Google Calendar MCP connector so an agent's availability check is against real events, not a manually-typed answer. Build this as a genuinely optional path: if a user hasn't connected, the flow degrades gracefully to "when are you free?" exactly as before, don't make this a blocker for anyone who skips it.

### 8.3 — The negotiation receipt
After convergence, generate a short structured summary of what was traded off, not just the final decision:

```python
@dataclass
class NegotiationReceipt:
    decision: str
    tradeoffs: list[str]  # e.g. ["Rahul's budget kept it under ₹600", "Priya's 20-min travel limit ruled out 2 options"]
```
This is a small addition on top of what Phase 1 already produces, and it's the single best demo moment you have, it's the proof the negotiation was real, not decorative.

### 8.4 — A verification step before announcing "unanimous"
Before the orchestrator declares convergence, add a cheap sanity check: does the final proposal actually satisfy every profile's stated hard constraints (budget ceiling, area/travel limit)? This is a deterministic check, not another Claude call, simple comparison logic. It's a small addition that answers "what if the agents are wrong" before a judge asks it.

### 8.5 — Open-sourcing the negotiation protocol
With real time, actually structure `choir/engine/` as a standalone, importable package with its own README and a couple of usage examples, separate from the Telegram-specific code. This is what makes "we're open-sourcing the negotiation core" a true claim in your pitch instead of an aspiration.

---

## Suggested day-by-day schedule (4+ days before Aug 8)

| Day | Focus | Who |
|---|---|---|
| **Day 1** | Phase 0 (contracts, together) → Phase 1 (negotiation engine) and Phase 2 (profile store) start in parallel. End of day: engine works standalone with fake profiles. | A + C |
| **Day 1 (parallel)** | Bot skeleton: BotFather setup, `/choir` command detection, the group-member-tracking workaround. | B |
| **Day 2** | Finish Phase 3 (bot layer, wired to the real engine + profile store). Deploy to Railway/Render (Phase 7) so the bot is live and testable by everyone. | B, with A/C helping wire things up |
| **Day 2 (parallel)** | Phase 4, full onboarding flow with inline keyboards. Test it on 3-4 real accounts, including people outside the core team if you can rope in friends. | B or C |
| **Day 3** | Phase 5 (venue sourcing with real Google Places data, purpose detection). Start Phase 8 features: confidence-based memory (8.1), negotiation receipt (8.3). | C, with A on 8.3 since it touches the engine |
| **Day 3 (parallel)** | Verification/sanity check (8.4). MCP calendar integration if time allows (8.2) — treat as a stretch goal, not a blocker. | A |
| **Day 4** | Full integration testing with real groups and real scenarios, including deliberately awkward ones (2-person group, someone with no stated preferences, a genuine budget standoff). Fix what breaks. Package the engine as importable (8.5) if pursuing the open-source angle. | All three |
| **Day 4 (evening)** | Write and rehearse the demo script against what's actually built. Record a fallback video of a clean run. Update the pitch doc and README to reflect the real feature set. | All three |
| **Aug 8 (hackathon day)** | Light touch: final polish, live debugging with mentor input, UI/copy refinement, rehearsal. You're not building the core from scratch, so use the day for the things that make a working project feel finished. | All three |

This is a much healthier position than compressing everything into 5 hours: the negotiation engine, bot layer, and onboarding should all be genuinely working and tested by Day 2-3, which means the actual hackathon day becomes about polish, live iteration with mentor feedback, and rehearsal, not a race against a broken build.