"""Manual test for the negotiation engine — no Telegram, no SQLite, no mocks.

Run: python test_engine.py

This is Phase 1's "done" bar from plan.md: three fake profiles with a genuine
conflict (tight budget vs. someone far away vs. someone flexible), fed
straight into run_negotiation(), reading whatever comes back. If every agent
agrees instantly with no back-and-forth, the system prompt in agent.py needs
tuning — that's the thing to watch for here, not just "did it crash."
"""
from dotenv import load_dotenv

from choir.engine.orchestrator import run_negotiation
from choir.schemas import NegotiationRequest, UserProfile

load_dotenv()  # picks up ANTHROPIC_API_KEY from .env, same as main.py

profiles = [
    UserProfile(
        telegram_user_id=1,
        budget_min=100,
        budget_max=300,
        preferences=["budget_conscious", "vegetarian"],
        area="HSR Layout",
        temporary_context="saving money this month",
    ),
    UserProfile(
        telegram_user_id=2,
        budget_min=500,
        budget_max=1200,
        preferences=["foodie", "cafe_person"],
        area="Indiranagar",
    ),
    UserProfile(
        telegram_user_id=3,
        budget_min=200,
        budget_max=800,
        preferences=["quiet_places"],
        area="HSR Layout",
    ),
]

request = NegotiationRequest(
    group_chat_id=0,
    goal_text="plan dinner for us tonight",
    profiles=profiles,
)


def on_round(round_num, signals):
    print(f"\n--- Round {round_num + 1} ---")
    for s in signals:
        line = f"  user {s.user_id}: {s.stance} — {s.reason}"
        if s.counter_proposal:
            line += f" (proposes: {s.counter_proposal})"
        print(line)


if __name__ == "__main__":
    result = run_negotiation(request, on_round=on_round)

    print("\n=== RESULT ===")
    if result.converged:
        print(f"Decision: {result.decision}")
        print(f"Explanation: {result.explanation}")
        print("Tradeoffs:")
        for t in result.tradeoffs:
            print(f"  - {t}")
    else:
        print("Didn't converge. Top options:")
        for opt in result.top_options:
            print(f"  - {opt}")
