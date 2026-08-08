"""Offline tests for choir/bot/handlers.py's _send_ride_suggestions — no
network calls. area_coords is passed in directly (reusing what the
negotiation itself already geocoded — see NegotiationResult.area_coords),
not re-geocoded here, so these tests just supply that dict straight up
instead of mocking geocode_areas/LocationIQ. choir/actions/links.py's own
math is tested separately in test_links.py.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import choir.bot.handlers as handlers
from choir.schemas import UserProfile, VenueResult

CHAT_ID = 100
VENUE = VenueResult(name="Cafe X", address="Somewhere", rating=0, price_level=0, lat=12.9698, lon=77.7500)

NEAR_PROFILE = UserProfile(telegram_user_id=1, budget_min=100, budget_max=500, preferences=[], area="Near Area")
FAR_PROFILE = UserProfile(telegram_user_id=2, budget_min=100, budget_max=500, preferences=[], area="Far Area")


def make_bot():
    return SimpleNamespace(send_message=AsyncMock())


class TestSendRideSuggestions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        name_patch = patch("choir.bot.handlers.get_member_name", return_value="Test")
        name_patch.start()
        self.addCleanup(name_patch.stop)

    async def test_venue_with_no_coordinates_is_a_silent_no_op(self):
        bot = make_bot()
        no_coords_venue = VenueResult(name="Cafe Y", address="Somewhere", rating=0, price_level=0)
        await handlers._send_ride_suggestions(
            CHAT_ID, [NEAR_PROFILE], no_coords_venue, bot, {"Near Area": (12.9700, 77.7502)}
        )
        bot.send_message.assert_not_called()

    async def test_empty_area_coords_is_a_silent_no_op(self):
        bot = make_bot()
        await handlers._send_ride_suggestions(CHAT_ID, [NEAR_PROFILE], VENUE, bot, {})
        bot.send_message.assert_not_called()

    async def test_close_profile_gets_no_ride_dm(self):
        bot = make_bot()
        await handlers._send_ride_suggestions(
            CHAT_ID, [NEAR_PROFILE], VENUE, bot, {"Near Area": (12.9700, 77.7502)}
        )
        bot.send_message.assert_not_called()

    async def test_far_profile_gets_a_ride_dm(self):
        bot = make_bot()
        await handlers._send_ride_suggestions(
            CHAT_ID, [FAR_PROFILE], VENUE, bot, {"Far Area": (12.8000, 77.5000)}
        )

        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.call_args.kwargs["chat_id"], 2)
        self.assertIn("km away", bot.send_message.call_args.kwargs["text"])
        self.assertIn("m.uber.com", bot.send_message.call_args.kwargs["text"])

    async def test_ungeocodable_area_is_skipped_not_crashed(self):
        bot = make_bot()
        await handlers._send_ride_suggestions(CHAT_ID, [FAR_PROFILE], VENUE, bot, {"Far Area": None})
        bot.send_message.assert_not_called()

    async def test_an_area_missing_entirely_from_area_coords_is_skipped_not_crashed(self):
        # area_coords only has entries for areas the negotiation successfully
        # geocoded — a profile whose area isn't a key at all (not even None)
        # must be skipped the same safe way as an explicit None.
        bot = make_bot()
        await handlers._send_ride_suggestions(CHAT_ID, [FAR_PROFILE], VENUE, bot, {})
        bot.send_message.assert_not_called()

    async def test_failed_dm_does_not_prevent_the_other_persons_dm(self):
        bot = make_bot()
        bot.send_message = AsyncMock(side_effect=[Exception("blocked"), None])
        # Deliberately far apart from EACH OTHER too, so only the two
        # ride-suggestion DMs fire — isolates this test from the carpool path.
        two_far_profiles = [
            UserProfile(telegram_user_id=10, budget_min=100, budget_max=500, preferences=[], area="Far A"),
            UserProfile(telegram_user_id=11, budget_min=100, budget_max=500, preferences=[], area="Far B"),
        ]
        await handlers._send_ride_suggestions(
            CHAT_ID, two_far_profiles, VENUE, bot,
            {"Far A": (12.8000, 77.5000), "Far B": (13.2000, 78.0000)},
        )
        self.assertEqual(bot.send_message.call_count, 2)

    async def test_nearby_participants_both_get_a_carpool_nudge(self):
        bot = make_bot()
        two_far_but_close_to_each_other = [
            UserProfile(telegram_user_id=20, budget_min=100, budget_max=500, preferences=[], area="Far A"),
            UserProfile(telegram_user_id=21, budget_min=100, budget_max=500, preferences=[], area="Far B"),
        ]
        await handlers._send_ride_suggestions(
            CHAT_ID, two_far_but_close_to_each_other, VENUE, bot,
            {"Far A": (12.8000, 77.5000), "Far B": (12.8010, 77.5010)},
        )

        carpool_texts = [
            c.kwargs["text"] for c in bot.send_message.call_args_list if "sharing a ride" in c.kwargs["text"]
        ]
        self.assertEqual(len(carpool_texts), 2)  # one to each person in the pair

    async def test_distant_participants_get_no_carpool_nudge(self):
        bot = make_bot()
        two_far_apart = [
            UserProfile(telegram_user_id=30, budget_min=100, budget_max=500, preferences=[], area="Far A"),
            UserProfile(telegram_user_id=31, budget_min=100, budget_max=500, preferences=[], area="Far B"),
        ]
        await handlers._send_ride_suggestions(
            CHAT_ID, two_far_apart, VENUE, bot,
            {"Far A": (12.8000, 77.5000), "Far B": (13.0000, 77.9000)},
        )

        carpool_texts = [
            c.kwargs["text"] for c in bot.send_message.call_args_list if "sharing a ride" in c.kwargs["text"]
        ]
        self.assertEqual(carpool_texts, [])


if __name__ == "__main__":
    unittest.main()
