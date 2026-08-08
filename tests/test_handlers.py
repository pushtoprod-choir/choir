"""Offline tests for the /choir command's message-flow orchestration — no
Telegram, no API keys. Mocks the DB layer and run_negotiation itself (already
live-verified separately in test_engine.py); this file is purely about the
occasion-capture and /choir update glue logic in choir/bot/handlers.py, which
had zero test coverage before this.
"""
import itertools
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import choir.bot.handlers as handlers
from choir.schemas import NegotiationResult, UserProfile, VenueResult

FAKE_PROFILE = UserProfile(telegram_user_id=1, budget_min=100, budget_max=500, preferences=[], area="HSR")

_update_id_counter = itertools.count(1)


def make_message(chat_id: int, text: str = "", user_id: int = 1, is_bot: bool = False):
    return SimpleNamespace(
        chat_id=chat_id,
        chat=SimpleNamespace(type="group"),
        from_user=SimpleNamespace(id=user_id, first_name="Test", is_bot=is_bot),
        text=text,
        reply_text=AsyncMock(),
    )


def make_update(message, update_id: int | None = None):
    # A fresh, unique id per call by default — the dedup guard's state is
    # module-level, so reusing an id across calls would make later ones look
    # like Telegram redeliveries and get silently (and confusingly) dropped.
    return SimpleNamespace(message=message, update_id=update_id or next(_update_id_counter))


def make_context(args: list[str]):
    return SimpleNamespace(args=args, bot=SimpleNamespace(username="choir_bot"))


class TestOccasionAndUpdateFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        handlers._active_negotiations.clear()
        handlers._pending_occasion.clear()
        handlers._last_decision.clear()
        handlers._processed_update_ids.clear()
        handlers._processed_update_id_order.clear()

        self.patches = [
            patch("choir.bot.handlers.get_seen_members", return_value=[1]),
            patch("choir.bot.handlers.get_profile", return_value=FAKE_PROFILE),
            patch("choir.bot.handlers.record_seen_member"),
            patch("choir.bot.handlers.get_member_name", return_value="Test"),
            patch("choir.bot.handlers.start_negotiation_log", return_value=1),
            patch("choir.bot.handlers.record_negotiation_round"),
            patch("choir.bot.handlers.finish_negotiation_log"),
            patch("choir.bot.handlers.get_negotiation_history", return_value=[]),
            patch("choir.bot.handlers.attach_calendar_availability"),
            patch("choir.bot.handlers.create_events_for_connected", return_value=None),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

        # is_planning_request hits the Anthropic API for real — mocked here
        # the same way run_negotiation already is, since this file is purely
        # about handlers.py's own glue logic. Kept as its own attribute
        # (rather than folded into self.patches) so individual tests can
        # flip its return_value to exercise the intent-gating path.
        is_planning_patch = patch("choir.bot.handlers.is_planning_request", return_value=True)
        self.mock_is_planning = is_planning_patch.start()
        self.addCleanup(is_planning_patch.stop)

        # get_missing_info_question hits the Anthropic API for real — mocked
        # to None (no question needed) by default so _gather_dynamic_context
        # is a no-op and doesn't need message.get_bot() in these tests. Kept
        # as its own attribute so the update-bypass test can assert it's
        # never called at all on that path.
        missing_info_patch = patch("choir.bot.handlers.get_missing_info_question", return_value=None)
        self.mock_get_missing_info = missing_info_patch.start()
        self.addCleanup(missing_info_patch.stop)


        run_patch = patch("choir.bot.handlers.run_negotiation")
        self.mock_run = run_patch.start()
        self.addCleanup(run_patch.stop)
        self.mock_run.return_value = NegotiationResult(
            converged=True, decision="Test Cafe", explanation="works", tradeoffs=["fine"], rounds=[],
        )

    async def test_choir_asks_for_occasion_before_negotiating(self):
        message = make_message(chat_id=100, text="/choir plan lunch")
        await handlers.handle_choir_command(make_update(message), make_context(["plan", "lunch"]))

        self.mock_run.assert_not_called()
        self.assertIn(100, handlers._pending_occasion)
        self.assertIn("occasion", message.reply_text.call_args[0][0].lower())

    async def test_occasion_reply_triggers_negotiation_with_context_included(self):
        handlers._pending_occasion[100] = "plan lunch"
        message = make_message(chat_id=100, text="my birthday!")
        await handlers.handle_occasion_reply(make_update(message), None)

        self.mock_run.assert_called_once()
        goal_text = self.mock_run.call_args[0][0].goal_text
        self.assertIn("plan lunch", goal_text)
        self.assertIn("birthday", goal_text)
        self.assertNotIn(100, handlers._pending_occasion)

    async def test_skip_reply_omits_occasion_from_goal_text(self):
        handlers._pending_occasion[100] = "plan lunch"
        message = make_message(chat_id=100, text="skip")
        await handlers.handle_occasion_reply(make_update(message), None)

        goal_text = self.mock_run.call_args[0][0].goal_text
        self.assertEqual(goal_text, "plan lunch")

    async def test_unrelated_message_is_ignored_when_nothing_pending(self):
        message = make_message(chat_id=999, text="hey everyone")
        await handlers.handle_occasion_reply(make_update(message), None)

        self.mock_run.assert_not_called()
        message.reply_text.assert_not_called()

    async def test_update_with_no_prior_decision_is_rejected(self):
        message = make_message(chat_id=200, text="/choir update running late")
        await handlers.handle_choir_command(make_update(message), make_context(["update", "running", "late"]))

        self.mock_run.assert_not_called()
        self.assertIn("no previous plan", message.reply_text.call_args[0][0].lower())

    async def test_update_with_no_reason_is_rejected(self):
        handlers._last_decision[200] = "Cafe Old"
        message = make_message(chat_id=200, text="/choir update")
        await handlers.handle_choir_command(make_update(message), make_context(["update"]))

        self.mock_run.assert_not_called()
        self.assertIn("tell me why", message.reply_text.call_args[0][0].lower())

    async def test_update_with_a_prior_decision_seeds_goal_text_and_skips_occasion(self):
        handlers._last_decision[200] = "Cafe Old"
        message = make_message(chat_id=200, text="/choir update someone's running late")
        await handlers.handle_choir_command(
            make_update(message), make_context(["update", "someone's", "running", "late"])
        )

        self.mock_run.assert_called_once()
        self.assertNotIn(200, handlers._pending_occasion)
        goal_text = self.mock_run.call_args[0][0].goal_text
        self.assertIn("Cafe Old", goal_text)
        self.assertIn("running late", goal_text)

    async def test_converged_negotiation_is_remembered_for_a_future_update(self):
        message = make_message(chat_id=100, text="/choir plan lunch")
        await handlers.handle_choir_command(make_update(message), make_context(["plan", "lunch"]))
        reply_message = make_message(chat_id=100, text="skip")
        await handlers.handle_occasion_reply(make_update(reply_message), None)

        self.assertEqual(handlers._last_decision[100], "Test Cafe")

    async def test_concurrency_guard_blocks_a_second_choir(self):
        handlers._active_negotiations.add(300)
        message = make_message(chat_id=300, text="/choir plan dinner")
        await handlers.handle_choir_command(make_update(message), make_context(["plan", "dinner"]))

        self.mock_run.assert_not_called()
        self.assertIn("already negotiating", message.reply_text.call_args[0][0].lower())

    async def test_second_choir_while_occasion_pending_is_rejected(self):
        handlers._pending_occasion[100] = "plan lunch"
        message = make_message(chat_id=100, text="/choir plan something else")
        await handlers.handle_choir_command(make_update(message), make_context(["plan", "something", "else"]))

        self.mock_run.assert_not_called()
        self.assertIn("still waiting", message.reply_text.call_args[0][0].lower())

    async def test_non_planning_message_is_gated_before_the_occasion_question(self):
        self.mock_is_planning.return_value = False
        message = make_message(chat_id=100, text="/choir what's up")
        await handlers.handle_choir_command(make_update(message), make_context(["what's", "up"]))

        self.mock_run.assert_not_called()
        self.assertNotIn(100, handlers._pending_occasion)
        self.assertIn("plan", message.reply_text.call_args[0][0].lower())

    async def test_update_bypasses_intent_gating_even_with_a_non_planning_sounding_reason(self):
        # A revision reason like "someone's running late" would plausibly
        # fail a naive planning-intent classifier despite being a completely
        # valid /choir update — the update path must never call it at all.
        self.mock_is_planning.return_value = False
        handlers._last_decision[200] = "Cafe Old"
        message = make_message(chat_id=200, text="/choir update someone's running late")
        await handlers.handle_choir_command(
            make_update(message), make_context(["update", "someone's", "running", "late"])
        )

        self.mock_is_planning.assert_not_called()
        self.mock_run.assert_called_once()

    async def test_update_bypasses_dynamic_questions(self):
        # An explicit revision command shouldn't trigger a fresh round of
        # clarifying questions — the reason for changing already provides
        # context, same rationale as skipping occasion-capture and
        # intent-gating on this path.
        self.mock_get_missing_info.return_value = "Would a real question ever be asked here?"
        handlers._last_decision[200] = "Cafe Old"
        message = make_message(chat_id=200, text="/choir update someone's running late")
        await handlers.handle_choir_command(
            make_update(message), make_context(["update", "someone's", "running", "late"])
        )

        self.mock_get_missing_info.assert_not_called()
        self.mock_run.assert_called_once()

    async def test_redelivered_update_is_processed_only_once(self):
        # Telegram long-polling can redeliver the same update (a documented
        # real behavior) — without this guard, a redelivered /choir would
        # trigger a second full negotiation for a trigger already handled.
        message = make_message(chat_id=100, text="/choir plan lunch")
        update = make_update(message, update_id=42)

        await handlers.handle_choir_command(update, make_context(["plan", "lunch"]))
        self.assertIn(100, handlers._pending_occasion)  # first delivery: processed normally

        handlers._pending_occasion.clear()  # simulate it having already moved on
        await handlers.handle_choir_command(update, make_context(["plan", "lunch"]))  # redelivery, same update_id
        self.assertNotIn(100, handlers._pending_occasion)  # second delivery: silently ignored


class TestBuildVenuesText(unittest.TestCase):
    def test_fits_everything_when_theres_room(self):
        venues = [VenueResult(name="Cafe A", address="HSR", rating=0, price_level=0)]
        text = handlers._build_venues_text(venues, max_chars=1000)
        self.assertIn("Cafe A", text)
        self.assertNotIn("trimmed for length", text)

    def test_drops_trailing_venues_to_fit_the_budget_instead_of_overflowing(self):
        venues = [
            VenueResult(name=f"Cafe {i}", address="Some Long Road Name, HSR Layout, Bengaluru", rating=0, price_level=0)
            for i in range(10)
        ]
        text = handlers._build_venues_text(venues, max_chars=300)

        self.assertLessEqual(len(text), 300 + len("\n\n_(+N more, trimmed for length)_") + 5)
        self.assertIn("trimmed for length", text)

    def test_zero_budget_returns_empty_string(self):
        venues = [VenueResult(name="Cafe A", address="HSR", rating=0, price_level=0)]
        self.assertEqual(handlers._build_venues_text(venues, max_chars=0), "")


class TestConvergedReplyAssembly(unittest.IsolatedAsyncioTestCase):
    """_run_negotiation's converged branch has two responsibilities that
    matter beyond the occasion/update glue already covered above: choosing
    the right venue-rendering path (a real decided_venue vs. a best-effort
    fallback search) and never letting a reply_text failure take down the
    whole command instead of degrading gracefully."""

    def setUp(self):
        handlers._active_negotiations.clear()
        handlers._pending_occasion.clear()
        handlers._last_decision.clear()
        handlers._processed_update_ids.clear()
        handlers._processed_update_id_order.clear()

        self.patches = [
            patch("choir.bot.handlers.get_seen_members", return_value=[1]),
            patch("choir.bot.handlers.get_profile", return_value=FAKE_PROFILE),
            patch("choir.bot.handlers.record_seen_member"),
            patch("choir.bot.handlers.get_member_name", return_value="Test"),
            patch("choir.bot.handlers.start_negotiation_log", return_value=1),
            patch("choir.bot.handlers.record_negotiation_round"),
            patch("choir.bot.handlers.finish_negotiation_log"),
            patch("choir.bot.handlers.attach_calendar_availability"),
            patch("choir.bot.handlers.get_missing_info_question", return_value=None),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_a_grounded_decision_uses_the_single_venue_path_not_a_fresh_search(self):
        venue = VenueResult(name="Cafe A", address="HSR", rating=0, price_level=0, lat=12.9, lon=77.6)
        run_patch = patch("choir.bot.handlers.run_negotiation", return_value=NegotiationResult(
            converged=True, decision="Cafe A", explanation="works", tradeoffs=[], rounds=[], decided_venue=venue,
        ))
        describe_patch = patch("choir.bot.handlers._describe_decided_venue", new_callable=AsyncMock, return_value="\n\ndescribed")
        fallback_patch = patch("choir.bot.handlers._fallback_venue_suggestions", new_callable=AsyncMock)
        calendar_patch = patch("choir.bot.handlers.create_events_for_connected", return_value=None)
        with run_patch, describe_patch as mock_describe, fallback_patch as mock_fallback, calendar_patch:
            message = make_message(chat_id=1, text="/choir plan lunch")
            await handlers._run_negotiation(message, "plan lunch")

        mock_describe.assert_awaited_once()
        mock_fallback.assert_not_called()
        self.assertIn("described", message.reply_text.call_args[0][0])

    async def test_an_ungrounded_decision_falls_back_to_a_length_budgeted_search(self):
        run_patch = patch("choir.bot.handlers.run_negotiation", return_value=NegotiationResult(
            converged=True, decision="Some free-text plan", explanation="works", tradeoffs=[], rounds=[],
        ))
        describe_patch = patch("choir.bot.handlers._describe_decided_venue", new_callable=AsyncMock)
        fallback_patch = patch(
            "choir.bot.handlers._fallback_venue_suggestions", new_callable=AsyncMock, return_value="\n\nfallback options"
        )
        calendar_patch = patch("choir.bot.handlers.create_events_for_connected", return_value=None)
        with run_patch, describe_patch as mock_describe, fallback_patch as mock_fallback, calendar_patch:
            message = make_message(chat_id=1, text="/choir plan lunch")
            await handlers._run_negotiation(message, "plan lunch")

        mock_describe.assert_not_called()
        mock_fallback.assert_awaited_once()
        # budget passed to the fallback must account for the reply text already built
        budget_arg = mock_fallback.call_args[0][2]
        self.assertLess(budget_arg, handlers.TELEGRAM_MESSAGE_LIMIT)
        self.assertIn("fallback options", message.reply_text.call_args[0][0])

    async def test_calendar_summary_line_is_appended_when_someone_is_connected(self):
        from choir.schemas import CalendarCreationSummary

        run_patch = patch("choir.bot.handlers.run_negotiation", return_value=NegotiationResult(
            converged=True, decision="Cafe Old", explanation="works", tradeoffs=[], rounds=[],
        ))
        fallback_patch = patch("choir.bot.handlers._fallback_venue_suggestions", new_callable=AsyncMock, return_value="")
        calendar_patch = patch(
            "choir.bot.handlers.create_events_for_connected",
            return_value=CalendarCreationSummary(connected=2, created=1, extraction_failed=False),
        )
        with run_patch, fallback_patch, calendar_patch:
            message = make_message(chat_id=1, text="/choir plan lunch")
            await handlers._run_negotiation(message, "plan lunch")

        self.assertIn("1/2 connected calendars", message.reply_text.call_args[0][0])

    async def test_reply_send_failure_retries_without_venues_instead_of_crashing(self):
        run_patch = patch("choir.bot.handlers.run_negotiation", return_value=NegotiationResult(
            converged=True, decision="Cafe Old", explanation="works", tradeoffs=[], rounds=[],
        ))
        fallback_patch = patch(
            "choir.bot.handlers._fallback_venue_suggestions", new_callable=AsyncMock, return_value="\n\nsome venues"
        )
        calendar_patch = patch("choir.bot.handlers.create_events_for_connected", return_value=None)
        with run_patch, fallback_patch, calendar_patch:
            message = make_message(chat_id=1, text="/choir plan lunch")
            # First send (decision + venues) fails as if it were over Telegram's
            # limit; the retry without venues must still go out, not raise.
            message.reply_text = AsyncMock(side_effect=[Exception("message too long"), None])
            await handlers._run_negotiation(message, "plan lunch")

        self.assertEqual(message.reply_text.call_count, 2)
        second_call_text = message.reply_text.call_args_list[1][0][0]
        self.assertNotIn("some venues", second_call_text)


if __name__ == "__main__":
    unittest.main()
