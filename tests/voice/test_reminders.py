"""voice/reminders.py: when a reminder is due, and keeping them."""

import time
import unittest
from unittest import mock
from datetime import datetime

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)
from voice import reminders


class When(unittest.TestCase):
    NOW = datetime(2026, 11, 17, 9, 40)

    def at(self, text):
        return datetime.fromtimestamp(reminders.when(text, self.NOW))

    def test_a_bare_time_is_the_next_one(self):
        self.assertEqual(self.at("10:15"), datetime(2026, 11, 17, 10, 15))
        self.assertEqual(self.at("08:30"), datetime(2026, 11, 18, 8, 30))  # already past today

    def test_tomorrow_a_date_and_in(self):
        self.assertEqual(self.at("tomorrow 08:30"), datetime(2026, 11, 18, 8, 30))
        self.assertEqual(self.at("2026-11-20 07:05"), datetime(2026, 11, 20, 7, 5))
        self.assertEqual(self.at("in 20m"), datetime(2026, 11, 17, 10, 0))
        self.assertEqual(self.at("in 2h"), datetime(2026, 11, 17, 11, 40))

    def test_a_named_day_already_past_is_refused(self):
        with self.assertRaises(ValueError):
            reminders.when("today 08:00", self.NOW)
        with self.assertRaises(ValueError):
            reminders.when("2026-11-16 10:00", self.NOW)

    def test_nonsense_is_refused(self):
        with self.assertRaises(ValueError):
            reminders.when("soonish")


class Keeping(unittest.TestCase):
    def setUp(self):
        reminders.FILE.unlink(missing_ok=True)

    def test_due_settle_cancel(self):
        early = reminders.add(100.0, "claude-a", "wake up", keep_awake=True)
        later = reminders.add(10 ** 10, "claude-a", "later")
        self.assertEqual([r.id for r in reminders.due(200.0)], [early["id"]])
        self.assertEqual(reminders.keep_awake_until(), 400.0)
        self.assertFalse(reminders.settle(early["id"], reminders.ReminderState.DELIVERED))  # not claimed: left
        self.assertEqual(reminders.claim(early["id"]).words, "wake up")
        self.assertEqual(reminders.keep_awake_until(), 400.0)  # on its way: still keeps the laptop awake
        self.assertTrue(reminders.settle(early["id"], reminders.ReminderState.DELIVERED))
        self.assertEqual(reminders.due(200.0), [])
        self.assertTrue(reminders.cancel(later["id"]))
        self.assertFalse(reminders.cancel(later["id"]))  # only a pending one
        self.assertEqual(reminders.keep_awake_until(), 0.0)

    def test_a_reminder_cancelled_before_its_claim_is_never_typed(self):
        item = reminders.add(time.time() - 10, "claude-a", "wake up")
        self.assertEqual(len(reminders.due()), 1)  # due, and then the user cancels it
        self.assertTrue(reminders.cancel(item["id"]))
        self.assertIsNone(reminders.claim(item["id"]))
        self.assertFalse(reminders.settle(item["id"], reminders.ReminderState.DELIVERED))
        self.assertEqual(reminders.listed()[0]["state"], "cancelled")

    def test_a_claimed_reminder_cannot_be_cancelled_and_a_failed_try_is_pending_again(self):
        item = reminders.add(time.time() - 10, "claude-a", "wake up")
        self.assertIsNotNone(reminders.claim(item["id"]))
        self.assertIsNone(reminders.claim(item["id"]))  # never typed twice
        self.assertFalse(reminders.cancel(item["id"]))
        self.assertTrue(reminders.later(item["id"]))
        self.assertEqual(reminders.listed()[0]["state"], "pending")
        self.assertEqual(reminders.due(), [])  # backing off
        self.assertFalse(reminders.later(item["id"]))  # only a claim is put back

    def test_a_claim_left_by_a_dead_server_is_due_again(self):
        item = reminders.add(time.time() - 10, "claude-a", "wake up")
        reminders.claim(item["id"])
        self.assertEqual(reminders.due(time.time()), [])
        self.assertEqual([r.id for r in reminders.due(time.time() + reminders.SEND_FOR + 1)], [item["id"]])

    def test_the_server_delivers_only_a_reminder_still_pending(self):
        from call import server
        item = reminders.add(time.time() - 10, "claude-a", "wake up")
        reminders.cancel(item["id"])
        with mock.patch.object(server, "to_agent", return_value=("Claude", "")) as typed:
            server.deliver_reminder(item["id"])
        typed.assert_not_called()
        other = reminders.add(time.time() - 10, "claude-a", "stand up")
        with mock.patch.object(server, "to_agent", return_value=("Claude", "")) as typed:
            server.deliver_reminder(other["id"])
        typed.assert_called_once()
        self.assertEqual(next(r for r in reminders.listed() if r["id"] == other["id"])["state"], "delivered")


if __name__ == "__main__":
    unittest.main()
