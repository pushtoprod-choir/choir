# Choir

**AI representatives that negotiate for your group, so you don't have to.**

Built for [Push to Prod: Building at the Frontier](https://pushtoprod-india.devfolio.co/overview) - Anthropic & Elevation Capital, August 8, 2026.

---

## The problem

Every group decision today runs on the same broken pattern: someone asks where everyone wants to go, ten messages of "idk you guys pick" happen, someone's real budget never gets said out loud, and half the time the group just doesn't go. The planning part was never actually hard. The coordination is. People hide their real constraints because saying them out loud creates friction, so the group either lands on a compromise nobody loves, or no decision at all.

Every existing group-planning tool treats this as an organizing problem. It isn't. It's a negotiation problem, and nobody's actually negotiating.

## What Choir does

Every person in a Telegram group gets a persistent AI representative that knows their real preferences, budget, and constraints. When the group wants to decide something, they trigger Choir with a single command, and the agents negotiate privately on each person's behalf, proposing, countering, trading off, the way each person would negotiate for themselves. The group only ever sees the final outcome and a short, honest reason why:

> *"Chose the café over the bar — Rahul's saving money this week, and Priya couldn't travel far today."*

Nobody has to say their budget out loud. Nobody has to be the one who says no.

Group outings are the first demo. The underlying primitive, private agents negotiating toward a shared outcome without over-disclosing, generalizes to bill splitting, scheduling, and gift pooling using the same engine.

## How it works

1. **Trigger.** Someone in a Telegram group types `/choir <what they want to do>`.
2. **Profile lookup.** Choir pulls each group member's stored profile, long-term preferences (budget range, tastes, general area) plus anything temporary they've mentioned for this specific plan ("I'm saving money this month").
3. **Negotiation.** Each person's agent proposes and reacts on their behalf. Agents exchange minimal-disclosure signals (`ACCEPT` / `REJECT` / `COUNTER-PROPOSE` + a reason), never raw private data. The orchestrator runs this until every agent actually agrees, or, if there's a genuine conflict, narrows it to the best 2 options and hands it back to the group instead of faking a decision nobody wants.
4. **Venue sourcing.** For outings specifically, Choir first figures out the purpose of the meetup (casual lunch, drinks, a work catch-up all want different things from a venue), then pulls real options from LocationIQ around a fair midpoint of everyone's stated area.
5. **Outcome.** The group gets one message: the decision, plus why.

## Onboarding — "Meet Your Representative"

Nobody fills out a profile form for a bot they just met, so onboarding is just-in-time, not upfront. The first time someone's needed for a negotiation and has no profile, Choir prompts them to start a DM (a Telegram requirement — bots can't message users who haven't messaged them first). Setup is a short, tap-based flow: a budget range, a few preference tags, a general area, done in under a minute. Calendar connection is offered as an optional extra step, never a blocker.

## Architecture

```
choir/
├── engine/
│   ├── agent.py          # single-agent reasoning: profile + goal -> proposal/response
│   └── orchestrator.py   # runs negotiation rounds, checks convergence, builds the final outcome
├── bot/
│   ├── handlers.py        # /choir command detection, message routing
│   └── onboarding.py      # inline-keyboard onboarding flow
├── store/
│   └── profiles.py        # profile CRUD (long-term + temporary memory)
├── venues/
│   └── places.py           # purpose detection + Google Places lookup
├── main.py
├── .env.example
└── requirements.txt
```

## Tech stack

| Piece | Choice |
|---|---|
| Language | Python |
| Bot framework | [`python-telegram-bot`](https://github.com/python-telegram-bot/python-telegram-bot) |
| Reasoning | Claude, via the [Anthropic SDK](https://github.com/anthropics/anthropic-sdk-python) |
| Venue data | LocationIQ (OpenStreetMap-backed) |
| Storage | SQLite (hackathon scope) |
| Transport | Long polling (no public server required) |

## Setup

### 1. Prerequisites
- Python 3.10+
- A Telegram account
- An [Anthropic API key](https://console.anthropic.com)
- A [LocationIQ](https://locationiq.com) API key (free tier is enough)

### 2. Create the Telegram bot
1. Message [`@BotFather`](https://t.me/BotFather) on Telegram
2. `/newbot` → choose a display name → choose a username ending in `bot`
3. Copy the API token it gives you

### 3. Clone and install
```bash
git clone <repo-url>
cd choir
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Configure environment variables
Copy `.env.example` to `.env` and fill in:
```
TELEGRAM_BOT_TOKEN=
ANTHROPIC_API_KEY=
LOCATION_IQ_API_KEY=
```

### 5. Run it
```bash
python main.py
```

Add the bot to a Telegram group, and trigger it with `/choir <what you want to do>`.

## Design principles

- **Minimal disclosure.** Agents never hand each other raw private data. Only outcomes cross the boundary.
- **Honest convergence.** "Unanimous" means every agent actually agreed, not an averaged compromise. If the group genuinely can't converge, Choir says so and narrows to the best options instead of forcing an answer.
- **No live wallets, no live GPS.** Budget and location are stated preferences set once during onboarding, not real-time financial or location data.
- **Zero-friction adoption.** Choir lives inside a chat people already use. No new app, no login.

## Roadmap

- [ ] Phase 1 — Negotiation engine (standalone, tested with fake profiles)
- [ ] Phase 2 — Profile store
- [ ] Phase 3 — Telegram bot layer
- [ ] Phase 4 — Onboarding flow
- [ ] Phase 5 — Venue sourcing
- [ ] Phase 6 — Demo polish

Beyond the hackathon: bill splitting, scheduling, and gift pooling on the same negotiation core; open-sourcing the negotiation protocol itself as a reusable pattern for agent-to-agent coordination.

## Team

Built by [Pragathi, Nishant and Lakshya](https://github.com/pushtoprod-choir) for Push to Prod, Bengaluru, August 2026.