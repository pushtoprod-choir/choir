"""Offline tests for choir/bot/handlers.py's _send_ride_suggestions — no
network calls. Mocks geocode_areas (LocationIQ) and the bot's send_message;
choir/actions/links.py's own math is tested separately in test_links.py.
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
        env_patch = patch.dict("os.environ", {"LOCATION_IQ_API_KEY": "test-key"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

        name_patch = patch("choir.bot.handlers.get_member_name", return_value="Test")
        name_patch.start()
        self.addCleanup(name_patch.stop)

    async def test_no_location_iq_key_is_a_silent_no_op(self):
        with patch.dict("os.environ", {}, clear=True):
            bot = make_bot()
            await handlers._send_ride_suggestions(CHAT_ID, [NEAR_PROFILE], VENUE, bot)
        bot.send_message.assert_not_called()

    async def test_venue_with_no_coordinates_is_a_silent_no_op(self):
        bot = make_bot()
        no_coords_venue = VenueResult(name="Cafe Y", address="Somewhere", rating=0, price_level=0)
        with patch("choir.bot.handlers.geocode_areas") as mock_geocode:
            await handlers._send_ride_suggestions(CHAT_ID, [NEAR_PROFILE], no_coords_venue, bot)
        mock_geocode.assert_not_called()
        bot.send_message.assert_not_called()

    async def test_close_profile_gets_no_ride_dm(self):
        bot = make_bot()
        with patch("choir.bot.handlers.geocode_areas", return_value={"Near Area": (12.9700, 77.7502)}):
            await handlers._send_ride_suggestions(CHAT_ID, [NEAR_PROFILE], VENUE, bot)
        bot.send_message.assert_not_called()

    async def test_far_profile_gets_a_ride_dm(self):
        bot = make_bot()
        with patch("choir.bot.handlers.geocode_areas", return_value={"Far Area": (12.8000, 77.5000)}):
            await handlers._send_ride_suggestions(CHAT_ID, [FAR_PROFILE], VENUE, bot)

        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.call_args.kwargs["chat_id"], 2)
        self.assertIn("km away", bot.send_message.call_args.kwargs["text"])
        self.assertIn("m.uber.com", bot.send_message.call_args.kwargs["text"])

    async def test_ungeocodable_area_is_skipped_not_crashed(self):
        bot = make_bot()
        with patch("choir.bot.handlers.geocode_areas", return_value={"Far Area": None}):
            await handlers._send_ride_suggestions(CHAT_ID, [FAR_PROFILE], VENUE, bot)
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
        with patch(
            "choir.bot.handlers.geocode_areas",
            return_value={"Far A": (12.8000, 77.5000), "Far B": (13.2000, 78.0000)},
        ):
            await handlers._send_ride_suggestions(CHAT_ID, two_far_profiles, VENUE, bot)
        self.assertEqual(bot.send_message.call_count, 2)

    async def test_nearby_participants_both_get_a_carpool_nudge(self):
        bot = make_bot()
        two_far_but_close_to_each_other = [
            UserProfile(telegram_user_id=20, budget_min=100, budget_max=500, preferences=[], area="Far A"),
            UserProfile(telegram_user_id=21, budget_min=100, budget_max=500, preferences=[], area="Far B"),
        ]
        with patch(
            "choir.bot.handlers.geocode_areas",
            return_value={"Far A": (12.8000, 77.5000), "Far B": (12.8010, 77.5010)},
        ):
            await handlers._send_ride_suggestions(CHAT_ID, two_far_but_close_to_each_other, VENUE, bot)

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
        with patch(
            "choir.bot.handlers.geocode_areas",
            return_value={"Far A": (12.8000, 77.5000), "Far B": (13.0000, 77.9000)},
        ):
            await handlers._send_ride_suggestions(CHAT_ID, two_far_apart, VENUE, bot)

        carpool_texts = [
            c.kwargs["text"] for c in bot.send_message.call_args_list if "sharing a ride" in c.kwargs["text"]
        ]
        self.assertEqual(carpool_texts, [])


if __name__ == "__main__":
    unittest.main()
