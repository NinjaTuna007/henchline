"""call/server.py's call chat lines as the host's ledger takes them: what each line carries for it."""

import importlib.util
import time
import unittest

from tests import sandbox  # first: a throwaway HOME

try:
    import aiohttp  # noqa: F401
except ImportError:
    raise unittest.SkipTest("aiohttp missing: run with ~/.local/share/henchline/venv/bin/python3")

from voice.state import PLAYING, SENT, Sent, write_json

spec = importlib.util.spec_from_file_location("call_server_lines", sandbox.REPO / "call/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class StatusLines(unittest.TestCase):
    def setUp(self):
        SENT.unlink(missing_ok=True)
        PLAYING.unlink(missing_ok=True)
        self.addCleanup(SENT.unlink, missing_ok=True)
        self.addCleanup(PLAYING.unlink, missing_ok=True)

    def chats(self, status):
        return [e["chat"] for e in status.events() if "chat" in e]

    def test_a_phone_text_is_not_repeated_as_said_on_the_call(self):
        status = server.Status()
        time.sleep(0.01)
        Sent("claude", server.Heard.TEXTED.value, session="claude-1", words="hi there").save()
        self.assertEqual([c for c in self.chats(status) if c["from"] == "you"], [])  # the v1 message is the record

    def test_words_heard_without_their_text_are_a_placeholder(self):
        status = server.Status()
        time.sleep(0.01)
        Sent("claude", server.Heard.CALL.value, session="claude-1").save()  # no words kept (not on a call)
        said = [c for c in self.chats(status) if c["from"] == "you"]
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0]["placeholder"])

    def test_talkback_names_its_clip_so_a_resumed_call_keeps_it_once(self):
        write_json(PLAYING, {"clip": "0042-claude.json", "session": "claude-1", "agent": "claude", "text": "Done."})
        first, resumed = self.chats(server.Status()), self.chats(server.Status())  # a new Status per connection
        self.assertEqual([c["clip"] for c in first], ["0042-claude.json"])
        self.assertEqual([c["clip"] for c in resumed], ["0042-claude.json"])  # sent again: the clip tells the ledger


if __name__ == "__main__":
    unittest.main()
