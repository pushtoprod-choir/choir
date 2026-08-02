from choir.schemas import UserProfile, AgentSignal


def get_agent_response(profile: UserProfile, goal_text: str, current_proposal: str | None) -> AgentSignal:
    """
    Person A's negotiation engine. Represents ONE person's agent reacting to
    the current state of the negotiation — see plan.md Phase 1.1 for the spec.
    """
    raise NotImplementedError("negotiation engine (Person A) not built yet")
