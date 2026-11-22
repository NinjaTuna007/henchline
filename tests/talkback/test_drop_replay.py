"""A call's line that drops mid-clip (a lift): what it cut is said again from the interrupted sentence when the phone
is back, texted once if the call is over, and nothing changes when the line holds."""

import importlib.util
import json
import time
import unittest
from pathlib import Path

from tests import sandbox

from voice import state
from voice.spool import Clip

spec = importlib.util.spec_from_file_location("talkback_worker", Path(sandbox.REPO) / "talkback/scripts/worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class Decision(unittest.TestCase):
    def test_drop_then_reconnect_replays(self):
        dropped = {"since": 100.0, "until": time.time() + 30}
        self.assertEqual(worker.on_dropped_line(False, dropped, True), "wait")  # while the phone may come back
        self.assertEqual(worker.on_dropped_line(False, dropped, False), "wait")  # a new clip waits too: not aloud
        self.assertEqual(worker.on_dropped_line(True, None, True), "play")  # the line is back: said on the call

    def test_drop_then_end_texts(self):
        self.assertEqual(worker.on_dropped_line(False, None, True), "text")  # the window passed: texted once

    def test_no_drop_changes_nothing(self):
        self.assertEqual(worker.on_dropped_line(True, None, False), "play")
        self.assertEqual(worker.on_dropped_line(False, None, False), "play")  # no call: at the laptop as ever


class HeldByDrop(unittest.TestCase):
    def test_clips_queued_after_the_line_went_quiet_are_texted(self):
        self.assertTrue(worker.held_by_drop(queued=105.0, since=100.0))
        self.assertTrue(worker.held_by_drop(queued=100.0, since=100.0))

    def test_older_clips_and_unknown_times_are_left_alone(self):
        self.assertFalse(worker.held_by_drop(queued=95.0, since=100.0))
        self.assertFalse(worker.held_by_drop(queued=105.0, since=None))


class Rewind(unittest.TestCase):
    def test_what_played_after_the_line_went_quiet_is_said_again(self):
        started = [10.0, 12.0, 14.0, 16.0]  # four pieces; the drop was noticed during the last
        self.assertEqual(worker.heard_up_to(started, 3, since=12.9), 1)  # quiet during piece 1: from piece 1

    def test_the_lag_counts_a_piece_just_begun_as_unheard(self):
        self.assertEqual(worker.heard_up_to([10.0, 12.0], 1, since=12.2), 0)  # 0.2 s into piece 1: not heard yet

    def test_without_a_time_it_resumes_where_it_was_cut(self):
        self.assertEqual(worker.heard_up_to([10.0, 12.0, 14.0], 2, since=None), 2)

    def test_never_later_than_where_it_was_cut(self):
        self.assertEqual(worker.heard_up_to([10.0, 12.0], 1, since=99.0), 1)

    def test_quiet_before_the_first_piece_says_it_all(self):
        self.assertEqual(worker.heard_up_to([10.0, 12.0], 1, since=5.0), 0)

    def test_the_rewound_piece_restarts_its_sentence(self):
        plan = worker.pieces(["Done.", "Here is the second sentence.", "And a third one."], "")
        at = worker.heard_up_to([0.0, 1.0, 2.0], 2, since=1.6)  # quiet during the second sentence
        self.assertEqual(plan.left(at, ["Done.", "Here is the second sentence.", "And a third one."]),
                         ["Here is the second sentence.", "And a third one."])


class Markers(unittest.TestCase):
    def setUp(self):
        state.STATE.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        state.CALL_DROPPED.unlink(missing_ok=True)
        state.CALL_SILENT.unlink(missing_ok=True)

    def test_a_dropped_line_counts_only_inside_its_window(self):
        state.CALL_DROPPED.write_text(json.dumps({"since": 50.0, "until": time.time() + 10}))
        self.assertIsNotNone(state.line_dropped())
        state.CALL_DROPPED.write_text(json.dumps({"since": 50.0, "until": time.time() - 1}))
        self.assertIsNone(state.line_dropped())
        state.CALL_DROPPED.write_text("not json")
        self.assertIsNone(state.line_dropped())

    def test_when_the_line_went_quiet(self):
        self.assertIsNone(state.silent_since())
        state.CALL_SILENT.write_text("123.5")
        self.assertEqual(state.silent_since(), 123.5)
        state.CALL_DROPPED.write_text(json.dumps({"since": 120.0, "until": time.time() + 10}))
        self.assertEqual(state.silent_since(), 120.0)  # the drop's own time first

    def test_an_old_empty_silence_marker_gives_no_time(self):
        state.CALL_SILENT.touch()  # written by an older server: no time in it
        self.assertIsNone(state.silent_since())

    def test_the_replay_flag_survives_the_spool(self):
        path = Path(state.STATE) / "replay-test.json"
        Clip("claude", "claude-x", ["One.", "Two."], for_call=True, replay=True).save(path)
        self.assertTrue(Clip.load(path).replay)
        path.unlink()


class Server(unittest.TestCase):
    def test_only_a_broken_connection_counts_as_a_drop(self):
        server_spec = importlib.util.find_spec("call.server")
        self.assertIsNotNone(server_spec)
        from call import server
        self.assertTrue(server.dropped_line(1006, here=False))  # the phone's TCP vanished (a lift)
        self.assertTrue(server.dropped_line(None, here=False))
        self.assertFalse(server.dropped_line(1000, here=False))  # the user hung up
        self.assertFalse(server.dropped_line(1001, here=False))
        self.assertFalse(server.dropped_line(1006, here=True))  # the laptop closed it


if __name__ == "__main__":
    unittest.main()
