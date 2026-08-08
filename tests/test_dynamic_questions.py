"""Offline tests for the dynamic-questions phase in choir/bot/handlers.py —
no Telegram, no API keys. Mocks choir.bot.handlers.get_missing_info_question
(already separately unit-tested in tests/test_agent.py) to exercise the
orchestration/state-machine logic: parallel dispatch, private DM + skip/
timeout handling, and the temporary_context hand-off into round 1.

MVP scope only: a single pre-round phase, no mid-round follow-ups, no
changes to orchestrator.py or the AgentSignal/stance contract.
"""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import choir.bot.handlers as handlers
from choir.schemas import UserProfile


def make_profile(user_id: int) -> UserProfile:
    return UserProfile(
        telegram_user_id=user_id, budget_min=100, budget_max=500, preferences=[], area="HSR"
    )


def make_bot():
    return SimpleNamespace(send_message=AsyncMock())


def make_message(chat_id: int, bot=None):
    return SimpleNamespace(chat_id=chat_id, get_bot=lambda: bot or make_bot())


def make_reply_message(user_id: int, text: str):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, is_bot=False),
        text=text,
        reply_text=AsyncMock(),
    )


def make_update(message):
    return SimpleNamespace(message=message)


class TestGatherDynamicContext(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        handlers._pending_clarifications.clear()
        self.addCleanup(handlers._pending_clarifications.clear)

    async def test_non_flagged_profile_gets_no_dm(self):
        profile = make_profile(1)
        bot = make_bot()
        message = make_message(chat_id=100, bot=bot)

        with patch("choir.bot.handlers.get_missing_info_question", return_value=None):
            await handlers._gather_dynamic_context(message, [profile], "usual lunch spot")

        bot.send_message.assert_not_called()
        self.assertIsNone(profile.temporary_context)

    async def test_flagged_profile_is_dmed_and_answer_lands_in_temporary_context(self):
        profile = make_profile(1)
        bot = make_bot()
        message = make_message(chat_id=100, bot=bot)

        async def reply_after_delay():
            await asyncio.sleep(0.05)
            await handlers.handle_clarification_reply(
                make_update(make_reply_message(1, "only after 7pm")), None
            )

        with patch(
            "choir.bot.handlers.get_missing_info_question",
            return_value="Any time window that works best tomorrow?",
        ):
            await asyncio.gather(
                handlers._gather_dynamic_context(message, [profile], "plan lunch tomorrow"),
                reply_after_delay(),
            )

        bot.send_message.assert_awaited_once()
        sent_text = bot.send_message.call_args.kwargs["text"]
        self.assertIn("Any time window", sent_text)
        self.assertEqual(bot.send_message.call_args.kwargs["chat_id"], 1)
        self.assertEqual(profile.temporary_context, "only after 7pm")

    async def test_skip_reply_leaves_temporary_context_none(self):
        profile = make_profile(1)
        bot = make_bot()
        message = make_message(chat_id=100, bot=bot)

        async def reply_after_delay():
            await asyncio.sleep(0.05)
            await handlers.handle_clarification_reply(make_update(make_reply_message(1, "skip")), None)

        with patch("choir.bot.handlers.get_missing_info_question", return_value="Free tomorrow?"):
            await asyncio.gather(
                handlers._gather_dynamic_context(message, [profile], "plan lunch tomorrow"),
                reply_after_delay(),
            )

        self.assertIsNone(profile.temporary_context)
        self.assertNotIn(1, handlers._pending_clarifications)

    async def test_timeout_proceeds_without_answer_and_cleans_up_pending_state(self):
        profile = make_profile(1)
        bot = make_bot()
        message = make_message(chat_id=100, bot=bot)

        with patch("choir.bot.handlers.get_missing_info_question", return_value="Free tomorrow?"), patch(
            "choir.bot.handlers._CLARIFICATION_TIMEOUT_SECONDS", 0.05
        ):
            await handlers._gather_dynamic_context(message, [profile], "plan lunch tomorrow")

        self.assertIsNone(profile.temporary_context)
        self.assertNotIn(1, handlers._pending_clarifications)

    async def test_failed_dm_send_is_treated_like_a_skip(self):
        profile = make_profile(1)
        bot = make_bot()
        bot.send_message = AsyncMock(side_effect=Exception("user blocked the bot"))
        message = make_message(chat_id=100, bot=bot)

        with patch("choir.bot.handlers.get_missing_info_question", return_value="Free tomorrow?"):
            await handlers._gather_dynamic_context(message, [profile], "plan lunch tomorrow")

        self.assertIsNone(profile.temporary_context)
        self.assertNotIn(1, handlers._pending_clarifications)

    async def test_multiple_flagged_profiles_are_all_dmed(self):
        profile1, profile2 = make_profile(1), make_profile(2)
        bot = make_bot()
        message = make_message(chat_id=100, bot=bot)

        def fake_question(profile, goal_text):
            return f"Question for {profile.telegram_user_id}?"

        with patch("choir.bot.handlers.get_missing_info_question", side_effect=fake_question), patch(
            "choir.bot.handlers._CLARIFICATION_TIMEOUT_SECONDS", 0.05
        ):
            await handlers._gather_dynamic_context(message, [profile1, profile2], "plan lunch")

        self.assertEqual(bot.send_message.call_count, 2)

    async def test_question_generation_runs_in_parallel_not_sequentially(self):
        profile1, profile2 = make_profile(1), make_profile(2)
        bot = make_bot()
        message = make_message(chat_id=100, bot=bot)

        def slow_question(profile, goal_text):
            time.sleep(0.1)  # runs inside asyncio.to_thread -> a real thread
            return None

        with patch("choir.bot.handlers.get_missing_info_question", side_effect=slow_question):
            started = time.monotonic()
            await handlers._gather_dynamic_context(message, [profile1, profile2], "plan lunch")
            elapsed = time.monotonic() - started

        # Sequential would take ~0.2s; concurrent (asyncio.gather + to_thread) ~0.1s.
        self.assertLess(elapsed, 0.18)

    async def test_same_user_pending_in_two_concurrent_negotiations_fails_open(self):
        # Same person, two different groups triggering /choir around the same
        # time — the second negotiation must not clobber the first's pending
        # state, double-DM the person, or crash.
        profile_a, profile_b = make_profile(1), make_profile(1)
        bot = make_bot()
        message_a, message_b = make_message(chat_id=100, bot=bot), make_message(chat_id=200, bot=bot)

        with patch("choir.bot.handlers.get_missing_info_question", return_value="Some question?"), patch(
            "choir.bot.handlers._CLARIFICATION_TIMEOUT_SECONDS", 0.05
        ):
            await asyncio.gather(
                handlers._gather_dynamic_context(message_a, [profile_a], "plan lunch"),
                handlers._gather_dynamic_context(message_b, [profile_b], "plan dinner"),
            )

        self.assertEqual(bot.send_message.call_count, 1)
        self.assertNotIn(1, handlers._pending_clarifications)

    async def test_unrelated_dm_reply_with_nothing_pending_is_ignored(self):
        reply = make_reply_message(999, "hello there")
        await handlers.handle_clarification_reply(make_update(reply), None)
        reply.reply_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
