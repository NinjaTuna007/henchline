"""`handsfree text` when the phone can't be reached: with Henchline set up on the phone the text is kept and the app
fetches it later, so it says "queued" and succeeds; with no way to the phone at all it fails and keeps nothing
(2026-11-26: a caller that retried on failure piled seven copies onto the phone)."""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import sandbox  # first: a throwaway HOME

hf_spec = importlib.util.spec_from_file_location("handsfree_text_queued", sandbox.REPO / "handsfree/handsfree.py")
handsfree = importlib.util.module_from_spec(hf_spec)
hf_spec.loader.exec_module(handsfree)


class TextWhenThePhoneIsOutOfReach(unittest.TestCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        self.texts = folder / "texts"
        self.texts.mkdir()
        self.ring = folder / "ring-endpoint"
        self.topic = folder / "ntfy-topic"
        self.pushed: list[bytes] = []
        self.push = "unreachable"
        self.server_up = True
        self.started: list[tuple[str, str]] = []
        self.ntfy_works = False
        self.rung: list[str] = []
        patches = [
            mock.patch.object(handsfree, "TEXTS", self.texts),
            mock.patch.object(handsfree, "CALL_RING", self.ring),
            mock.patch.object(handsfree, "RING_TOPIC", self.topic),
            mock.patch.object(handsfree, "HANDSFREE_LOG", folder / "handsfree.log"),
            mock.patch.object(handsfree, "in_call", lambda: False),
            mock.patch.object(handsfree, "push_outcome",
                              lambda body, headers=None, to="": self.pushed.append(body) or self.push),
            mock.patch.object(handsfree, "call_server_up", lambda: self.server_up),
            mock.patch.object(handsfree, "start_detached", lambda command, tid: self.started.append((command, tid))),
            mock.patch.object(handsfree, "ntfy", lambda *a, **k: self.ntfy_works),
            mock.patch.object(handsfree, "ring", lambda words, **k: self.rung.append(words) or 2),
            mock.patch.object(handsfree.time, "sleep", lambda s: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def send(self, words: str = "Class soon", **kw) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = handsfree.text(words, sent_by="Class reminders", **kw)
        return code, out.getvalue()

    def records(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in self.texts.glob("*.json")]

    def test_a_push_that_fails_with_henchline_set_up_is_queued_and_succeeds(self):
        self.ring.write_text("https://push.example/up-test")
        code, said = self.send()
        self.assertEqual(code, 0)
        self.assertIn("queued (id", said)
        self.assertIn("pushed again every minute", said)
        self.assertNotIn("couldn't reach", said)
        [record] = self.records()  # kept, for the app's /texts/pending
        self.assertEqual((record["status"], record["words"]), ("sent", "Class soon"))
        self.assertIn(record["id"], said)
        self.assertEqual(self.started, [("text-retry", record["id"])])  # the worker that pushes it again
        log = (self.texts.parent / "handsfree.log").read_text()
        self.assertIn("queued", log)
        self.assertNotIn("Class soon", log)  # never the words

    def test_an_http_refusal_is_a_failure_not_queued(self):
        self.ring.write_text("https://push.example/up-test")
        self.push = "refused"
        code, said = self.send()
        self.assertEqual(code, 1)
        self.assertNotIn("queued", said)
        self.assertEqual((self.records(), self.started), ([], []))

    def test_with_the_call_server_down_nothing_is_queued(self):
        self.ring.write_text("https://push.example/up-test")
        self.server_up = False
        code, said = self.send()
        self.assertEqual((code, self.records()), (1, []))
        self.assertIn("call server is down", said)

    def test_no_way_to_the_phone_fails_and_keeps_no_record(self):
        code, said = self.send()  # no push address, no ntfy topic
        self.assertEqual(code, 1)
        self.assertIn("couldn't reach the phone", said)
        self.assertEqual(self.records(), [])

    def test_ntfy_still_comes_before_the_queue(self):
        self.ring.write_text("https://push.example/up-test")
        self.topic.write_text("topic")
        self.ntfy_works = True
        code, said = self.send()
        self.assertEqual(code, 0)
        self.assertIn("sent as an ntfy notification", said)
        self.assertNotIn("queued", said)

    def test_a_push_that_works_is_sent_not_queued(self):
        self.ring.write_text("https://push.example/up-test")
        self.push = "sent"
        code, said = self.send()
        self.assertEqual(code, 0)
        self.assertIn("sent to the phone", said)
        self.assertNotIn("queued", said)
        self.assertEqual([c for c, _ in self.started], ["text-backup"])

    def test_until_read_on_a_queued_text_still_rings_when_unread(self):
        self.ring.write_text("https://push.example/up-test")
        clock = iter(range(0, 10_000, 30))
        with mock.patch.object(handsfree.time, "time", lambda: float(next(clock))):
            code, said = self.send("Read me", until_read=1)
        self.assertIn("queued", said)
        self.assertIn("not read in 1 min: ringing", said)
        self.assertEqual((code, self.rung), (2, ["Read me"]))

    def test_until_replied_on_a_queued_text_returns_once_answered(self):
        self.ring.write_text("https://push.example/up-test")
        with mock.patch.object(handsfree, "text_replied", lambda tid: True):
            code, said = self.send("Answer me", until_read=1, until="replied")
        self.assertIn("queued", said)
        self.assertEqual((code, self.rung), (0, []))


class RetryWorker(TextWhenThePhoneIsOutOfReach):
    """`handsfree text-retry ID`: a queued text pushed again until it lands, it is confirmed, or time is up."""

    def queued(self) -> str:
        self.ring.write_text("https://push.example/up-test")
        self.send("Class soon")
        [record] = self.records()
        return record["id"]

    def retry(self, tid: str, retry_for: float = 300) -> str:
        clock = iter(range(0, 100_000, 60))
        with mock.patch.object(handsfree.time, "time", lambda: float(next(clock))), \
             contextlib.redirect_stdout(io.StringIO()):
            return handsfree._retry_text(tid, every=60, retry_for=retry_for)

    def test_stops_when_a_push_goes_through(self):
        tid = self.queued()
        self.pushed.clear()
        self.push = "sent"
        self.assertEqual(self.retry(tid), "pushed")
        self.assertEqual(len(self.pushed), 1)

    def test_stops_when_the_phone_confirms_it(self):
        tid = self.queued()
        handsfree.update_text(tid, status="read")
        self.pushed.clear()
        self.assertEqual(self.retry(tid), "confirmed")
        self.assertEqual(self.pushed, [])

    def test_gives_up_at_the_cap_and_says_so(self):
        tid = self.queued()
        self.pushed.clear()
        self.assertEqual(self.retry(tid, retry_for=300), "gave up")
        self.assertTrue(2 <= len(self.pushed) <= 7)
        log = (self.texts.parent / "handsfree.log").read_text()
        self.assertIn("still not pushed", log)
        self.assertNotIn("Class soon", log)


if __name__ == "__main__":
    unittest.main()
