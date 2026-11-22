"""voice/desktop/tmux.py: a new agent starts without another agent's inherited marks."""

import os
import unittest
from unittest import mock

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)
from voice.desktop import tmux


class CleanEnv(unittest.TestCase):
    def test_marks_from_tmux_and_from_us_are_unset(self):
        shown = "CURSOR_WORKSPACE_LABEL=robotproj\nHOME=/home/me\n-CURSOR_AGENT\nAGENT_TRANSCRIPTS=/x\n"
        with mock.patch.object(tmux, "out", return_value=shown), \
             mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "t", "PATH": "/usr/bin"}, clear=True):
            words = tmux.clean_env()
        self.assertTrue(words[0].endswith("env"))
        self.assertEqual(sorted(words[1:]), ["-uAGENT_TRANSCRIPTS", "-uCODEX_THREAD_ID", "-uCURSOR_AGENT",
                                             "-uCURSOR_WORKSPACE_LABEL"])

    def test_only_an_off_color_setting_is_unset(self):
        shown = "HOME=/home/me\nNO_COLOR=1\nFORCE_COLOR=0\n"
        with mock.patch.object(tmux, "out", return_value=shown), \
             mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            words = tmux.clean_env()
        self.assertEqual(sorted(words[1:]), ["-uFORCE_COLOR", "-uNO_COLOR"])

    def test_force_color_left_on_is_kept(self):
        with mock.patch.object(tmux, "out", return_value="HOME=/home/me\nFORCE_COLOR=1\nNO_COLOR=\n"), \
             mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            self.assertEqual(sorted(tmux.clean_env()[1:]), ["-uNO_COLOR"])
        with mock.patch.object(tmux, "out", return_value="HOME=/home/me\nFORCE_COLOR=\n"), \
             mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            self.assertEqual(tmux.clean_env(), [])
        with mock.patch.object(tmux, "out", return_value="HOME=/home/me\nFORCE_COLOR=false\n"), \
             mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            self.assertEqual(sorted(tmux.clean_env()[1:]), ["-uFORCE_COLOR"])


class Panes(unittest.TestCase):
    """Whether a pane is there comes from the list of panes: tmux 3.4 answers `display-message -t %99` for a pane
    that isn't there with nothing, and success."""

    def test_a_pane_missing_from_a_running_server_is_gone(self):
        with mock.patch.object(tmux, "pane_pids", return_value={"%0": 10, "%1": 11}):
            self.assertFalse(tmux.pane_there(tmux.Tab("tmux", "%15")))
            self.assertTrue(tmux.pane_there(tmux.Tab("tmux", "%1")))
            self.assertEqual(tmux.pane_pid(tmux.Tab("tmux", "%1")), 11)

    def test_no_server_means_gone_and_no_answer_means_unknown(self):
        with mock.patch.object(tmux, "pane_pids", return_value={}):
            self.assertFalse(tmux.pane_there(tmux.Tab("tmux", "%1")))
        with mock.patch.object(tmux, "pane_pids", return_value=None):
            self.assertIsNone(tmux.pane_there(tmux.Tab("tmux", "%1")))
            self.assertEqual(tmux.pane_pid(tmux.Tab("tmux", "%1")), 0)

    def test_server_start(self):
        with mock.patch.object(tmux, "out", return_value="1795357333"):
            self.assertEqual(tmux.server_started(), 1795357333.0)
        with mock.patch.object(tmux, "out", return_value=""), mock.patch.object(tmux, "pane_pids", return_value={}):
            self.assertEqual(tmux.server_started(), 0.0)
        with mock.patch.object(tmux, "out", return_value=""), mock.patch.object(tmux, "pane_pids", return_value=None):
            self.assertIsNone(tmux.server_started())


if __name__ == "__main__":
    unittest.main()
