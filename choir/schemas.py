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
    notes: Optional[str] = None   # free-text "anything else we should know" answer from onboarding
    calendar_busy_text: Optional[str] = None  # populated at runtime by choir.calendar.client, never persisted —
                                               # None means "not connected", not "free"
    preference_confidence: dict = field(default_factory=dict)  # tag -> 0.0-1.0, persisted; a tag missing from
                                               # this dict (e.g. never negotiated over yet) implies the default
                                               # starting confidence (see choir.store.profiles.DEFAULT_CONFIDENCE),
                                               # not zero — callers must not assume every stated preference has
                                               # an entry here


@dataclass
class NegotiationRequest:
    group_chat_id: int
    goal_text: str                # raw text after the /choir command, e.g. "plan lunch for us"
    profiles: list[UserProfile]
    plan_date: str                 # "YYYY-MM-DD", extracted from the /choir plan text up front — fixed,
                                    # never negotiated; only the time is left for the agents to decide


@dataclass
class AgentSignal:
    user_id: int
    stance: str                   # "ACCEPT" | "REJECT" | "COUNTER"
    reason: str                   # short, human-readable
    counter_proposal: Optional[str] = None
    proposed_time: Optional[str] = None   # "HH:MM" 24-hour — the time this person is proposing/confirming for
                                           # plan_date; None only on an error-path fallback signal (see agent.py)


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
    lat: float = 0.0
    lon: float = 0.0
    note: Optional[str] = None   # best-effort rating/price/vibe note from enrichment; None if unavailable


@dataclass
class NegotiationResult:
    converged: bool
    decision: Optional[str]        # e.g. "Cafe Coffee Day, HSR, 7:30pm"
    explanation: Optional[str]     # e.g. "Chose the cafe over the bar — Rahul's saving money this week"
    top_options: list[str] = field(default_factory=list)  # populated only if converged == False
    tradeoffs: list[str] = field(default_factory=list)    # per-person reasons, populated on convergence
    rounds: list[list[AgentSignal]] = field(default_factory=list)  # every round's signals, always populated —
                                                                    # lets the bot show the actual back-and-forth,
                                                                    # not just the final outcome
    decided_venue: Optional[VenueResult] = None  # the REAL venue `decision` resolved to, when the negotiation had
                                                  # real candidates to choose from — None if venues were unavailable
                                                  # (LocationIQ down/unconfigured) and the decision is still free text
    decided_time: Optional[str] = None  # "HH:MM" 24-hour, the time the group actually converged on — always set
                                         # together with `decision` on convergence, always None otherwise
    area_coords: dict = field(default_factory=dict)  # area name -> (lat, lon) or None, from the SAME geocoding
                                         # pass _fetch_venue_context already did for venue-grounding/the distance
                                         # check — reused by choir.bot.handlers._send_ride_suggestions so it never
                                         # has to re-geocode (and re-risk a rate-limited/failed lookup) moments
                                         # after this same data was already fetched once


@dataclass
class CalendarTokens:
    telegram_user_id: int
    access_token: str
    refresh_token: str
    token_expiry: int   # unix timestamp (seconds)


@dataclass
class EventDetails:
    title: str
    start_iso: str              # ISO 8601, timezone-aware
    duration_minutes: int
    location_text: str


@dataclass
class CalendarCreationSummary:
    connected: int
    created: int
    build_failed: bool = False   # True only if plan_date/decided_time couldn't be combined into a real
                                  # datetime — shouldn't happen given the negotiation contract, but guarded
                                  # rather than trusted blindly (see choir.calendar.scheduling.build_event_details)
    event_ids: dict = field(default_factory=dict)  # telegram_user_id -> Google Calendar event id, for the ones
                                                     # actually created — lets the caller persist them so a later
                                                     # /choir update can delete exactly these events instead of
                                                     # stacking a duplicate on top
