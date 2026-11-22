"""A computer with no sound server still runs the call server (chats and texts only)."""

import unittest
from unittest import mock

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)
from call import server


class NoSoundServer(unittest.TestCase):
    def test_pactl_missing_answers_nothing_and_a_call_sets_up_no_sinks(self):
        with mock.patch.object(server.subprocess, "run", side_effect=FileNotFoundError("pactl")):
            self.assertEqual(server.pactl("list", "short", "modules"), "")
            with server.Devices() as devices:
                self.assertEqual(devices.modules, ["", ""])  # nothing to unload afterwards


if __name__ == "__main__":
    unittest.main()
