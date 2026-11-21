"""The canceller gets a fresh start after an xrun on its path, and isn't judged while the laptop is overloaded."""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import sandbox  # noqa: F401
import listener
import sensing

TOP = """S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME 
C  167      0      0    ---     ---   ---   ---     0                  talkback_ec_capture
C  199      0      0    ---     ---   ---   ---     0                  talkback_ec_source
S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME 
R  233    256  48000  86.4us   0.3us  0.02  0.00   {mic}    S32LE 2 48000 alsa_input.mic
R  168      0      0   9.2us  20.3us  0.00  0.00  {spk}    S32LE 2 48000  + alsa_output.speaker
R  {cid}    480  48000  16.4us   6.4us  0.00  0.00    {cap}     F32P 1 48000  + talkback_ec_capture
R  199    480  48000  31.7us   3.4us  0.01  0.00    {src}     F32P 1 48000  + talkback_ec_source
R  300    480  48000  31.7us   3.4us  0.01  0.00    {other}     F32P 1 48000  + Plasma PA
"""
WATCHED = {"alsa_input.mic", "alsa_output.speaker", "talkback_ec_capture", "talkback_ec_source"}


def top(mic=14, spk=145, cap=0, src=3, other=0, cid=167):
    return TOP.format(mic=mic, spk=spk, cap=cap, src=src, other=other, cid=cid)


class Parse(unittest.TestCase):
    def test_counts_come_from_the_last_block(self):
        counts = sensing.pw_errors(top())
        self.assertEqual(counts[(199, "talkback_ec_source")], 3)
        self.assertEqual(counts[(168, "alsa_output.speaker")], 145)
        self.assertEqual(counts[(167, "talkback_ec_capture")], 0)

    def test_no_second_block_no_counts(self):
        self.assertEqual(sensing.pw_errors(top().split("S   ID", 2)[0]), {})


class Watch(unittest.TestCase):
    def watcher(self, texts):
        texts = iter(texts)
        return sensing.Xruns(threading.Event(), poll=lambda: next(texts), watched=lambda: WATCHED)

    def test_first_sight_only_sets_the_count(self):
        xruns = self.watcher([top()])
        xruns.poll()
        self.assertEqual(xruns.take(), {})

    def test_new_xruns_on_watched_nodes(self):
        xruns = self.watcher([top(), top(src=8, spk=146, other=50)])
        xruns.poll(), xruns.poll()
        self.assertEqual(xruns.take(), {"talkback_ec_source": 5, "alsa_output.speaker": 1})
        self.assertEqual(xruns.take(), {})  # taken once

    def test_a_restarted_node_is_a_new_start(self):
        xruns = self.watcher([top(cap=0), top(cap=40, cid=500)])  # new id: the canceller restarted
        xruns.poll(), xruns.poll()
        self.assertEqual(xruns.take(), {})
        self.assertNotIn((167, "talkback_ec_capture"), xruns.seen)

    def test_counts_right_after_a_restart_are_not_news(self):
        xruns = self.watcher([top(), top(src=9), top(src=10)])
        xruns.poll()
        xruns.settle()
        xruns.poll()
        self.assertEqual(xruns.take(), {})
        xruns.quiet_until = 0.0
        xruns.poll()
        self.assertEqual(xruns.take(), {"talkback_ec_source": 1})

    def test_the_default_output_only_while_it_is_the_speakers(self):
        for speakers, expected in ((True, True), (False, False)):
            xruns = sensing.Xruns(threading.Event(), poll=lambda: "", on_speakers=lambda: speakers)
            with mock.patch.object(sensing.subprocess, "run",
                                   return_value=mock.Mock(stdout="alsa_output.speaker\n")) as ran, \
                    mock.patch.object(sensing, "canceller_mic", return_value="alsa_input.mic"):
                nodes = xruns.nodes()
            self.assertEqual("alsa_output.speaker" in nodes, expected)
            self.assertEqual(ran.called, expected)
            self.assertTrue({"talkback_ec_capture", "talkback_ec_source", "alsa_input.mic"} <= nodes)

    def test_a_failed_poll_changes_nothing(self):
        def broken():
            raise OSError("no pw-top")
        xruns = sensing.Xruns(threading.Event(), poll=broken, watched=lambda: WATCHED)
        xruns.poll()
        self.assertEqual(xruns.take(), {})


class Busy(unittest.TestCase):
    def test_over_the_share_of_cores(self):
        loads = [3.0, 13.0, 13.0, 5.0]
        load = sensing.Load(cores=22, loadavg=lambda: (loads.pop(0), 0, 0))
        with mock.patch.object(sensing, "log") as said:
            self.assertFalse(load.high())
            load.at = 0.0
            self.assertTrue(load.high())
            load.at = 0.0
            self.assertTrue(load.high())
            load.at = 0.0
            self.assertFalse(load.high())
        self.assertEqual(said.call_count, 2)  # once busy, once back

    def test_read_at_most_once_a_second(self):
        calls = []
        load = sensing.Load(cores=22, loadavg=lambda: calls.append(1) or (1.0, 0, 0))
        load.high(), load.high(), load.high()
        self.assertEqual(len(calls), 1)


class BusyNotice(unittest.TestCase):
    def test_said_once_a_busy_spell_and_never_on_a_call(self):
        me = SimpleNamespace(busy_told=False)
        with mock.patch.object(listener.routing, "say") as said:
            for _ in range(5):
                listener.Listener.tell_busy(me, True, True, False)
            self.assertEqual(said.call_count, 1)
            listener.Listener.tell_busy(me, True, False, False)  # the spell ended
            listener.Listener.tell_busy(me, True, True, False)
            self.assertEqual(said.call_count, 2)
            me.busy_told = False
            listener.Listener.tell_busy(me, True, False, True)  # on a call: the guard doesn't apply
            listener.Listener.tell_busy(me, False, True, False)  # no clip playing
            self.assertEqual(said.call_count, 2)


class Listening(unittest.TestCase):
    def me(self, **kw):
        base = dict(calling=False, xrun_due=False, xrun_rest=listener.XRUN_REST, last_xrun=0.0,
                    restart_due=False, xruns=SimpleNamespace(take=lambda: {"talkback_ec_source": 2},
                                                              settle=lambda: None),
                    load=SimpleNamespace(high=lambda: False),
                    echo_watch=SimpleNamespace(restarted=time.monotonic() - 100),
                    echo=SimpleNamespace(clear=lambda: None),
                    stream=SimpleNamespace(terminate=lambda: None), open_stream=lambda: None, canceller_since=0.0)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_an_xrun_asks_for_a_restart(self):
        me = self.me()
        with mock.patch.object(listener, "log") as said:
            listener.Listener.watch_xruns(me)
        self.assertTrue(me.xrun_due)
        self.assertIn("canceller xrun: talkback_ec_source +2, restart when quiet", said.call_args[0][1])

    def test_not_on_a_call(self):
        me = self.me(calling=True)
        listener.Listener.watch_xruns(me)
        self.assertFalse(me.xrun_due)

    def test_ready_after_the_rest_and_not_overloaded(self):
        self.assertTrue(listener.Listener.xrun_ready(self.me(xrun_due=True)))
        young = self.me(xrun_due=True, echo_watch=SimpleNamespace(restarted=time.monotonic() - 5))
        self.assertFalse(listener.Listener.xrun_ready(young))
        busy = self.me(xrun_due=True, load=SimpleNamespace(high=lambda: True))
        self.assertFalse(listener.Listener.xrun_ready(busy))
        self.assertFalse(listener.Listener.xrun_ready(self.me()))

    def test_ready_sooner_than_the_drift_watch(self):
        # A canceller two minutes old may not restart for drift (five minutes) but may for an xrun
        me = self.me(xrun_due=True, echo_watch=SimpleNamespace(restarted=time.monotonic() - 120,
                                                               may_restart=lambda: False))
        self.assertTrue(listener.Listener.xrun_ready(me))

    def test_xrun_restarts_back_off_and_stay_at_the_cap_while_xruns_go_on(self):
        me = self.me()
        clock = [10000.0]
        with mock.patch.object(listener.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(listener, "start_canceller", return_value=1.0), mock.patch.object(listener, "log"):
            rests = []
            for _ in range(12):  # xruns every 20 s that never stop; a restart each time one may happen
                waited = 0.0
                while waited < me.xrun_rest:
                    listener.Listener.watch_xruns(me)
                    clock[0] += 20
                    waited += 20
                listener.Listener.restart_canceller(me)
                rests.append(me.xrun_rest)
            self.assertEqual(rests[:4], [60.0, 120.0, 240.0, 300.0])
            self.assertTrue(all(rest == listener.XRUN_REST_MAX for rest in rests[3:]))
            clock[0] += listener.XRUN_REST_MAX + 1  # a quiet spell: the next xrun starts short again
            listener.Listener.watch_xruns(me)
            self.assertEqual(me.xrun_rest, listener.XRUN_REST)

    def test_a_restart_settles_the_watcher(self):
        settled = []
        me = self.me(xruns=SimpleNamespace(take=lambda: {}, settle=lambda: settled.append(1)))
        with mock.patch.object(listener, "start_canceller", return_value=1.0), mock.patch.object(listener, "log"):
            listener.Listener.restart_canceller(me)
        self.assertEqual(settled, [1])


if __name__ == "__main__":
    unittest.main()
