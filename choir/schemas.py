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
    extraction_failed: bool = False
