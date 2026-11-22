"""call/server.py: who may post to it, and that a text or a share always names its agent."""

import importlib.util
import unittest
from unittest import mock

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)

try:
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase
except ImportError:  # the call's own packages are in the project's venv: run the suite with its python
    raise unittest.SkipTest("aiohttp missing: run with ~/.local/share/henchline/venv/bin/python3")

spec = importlib.util.spec_from_file_location("call_server", sandbox.REPO / "call/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class Routes(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        app.router.add_post("/text", server.texted)
        app.router.add_post("/share/tell", server.share_tell)
        app.router.add_get("/chat/minions", server.chat_minions)
        app.router.add_post("/chat/minions/view", server.chat_minion_view)
        app.router.add_post("/chat/minions/decide", server.chat_minion_decide)
        return app

    def origin(self):
        return {"Origin": f"https://{self.server.host}:{self.server.port}"}

    async def test_another_sites_page_is_refused(self):
        answer = await self.client.post("/text", json={"text": "hi", "session": "claude-1"},
                                        headers={"Origin": "https://evil.example"})
        self.assertEqual(answer.status, 403)

    async def test_a_text_without_an_agent_is_refused(self):
        with mock.patch.object(server, "to_agent") as to_agent:
            answer = await self.client.post("/text", json={"text": "hi", "session": ""}, headers=self.origin())
            self.assertEqual(answer.status, 400)
            to_agent.assert_not_called()

    async def test_a_text_goes_to_the_agent_named_without_switching(self):
        with mock.patch.object(server, "to_agent", return_value=("Claude", "")) as to_agent:
            answer = await self.client.post("/text", json={"text": " hi  there ", "session": "claude-1"},
                                            headers=self.origin())
            self.assertEqual(await answer.json(), {"to": "Claude", "queued": False})
            to_agent.assert_called_once_with("hi there", "claude-1", server.Heard.TEXTED, False, False)

    async def test_a_share_off_a_call_without_an_agent_is_refused(self):
        with mock.patch.object(server, "in_call", return_value=False), mock.patch.object(server, "to_agent") as to_agent:
            answer = await self.client.post("/share/tell", json={"text": "a link", "session": ""}, headers=self.origin())
            self.assertEqual(answer.status, 400)
            to_agent.assert_not_called()

    async def test_files_that_never_arrived_are_refused(self):
        answer = await self.client.post("/share/tell", json={"files": ["nope.bin"], "session": "claude-1"},
                                         headers=self.origin())
        self.assertEqual(answer.status, 400)

    async def test_delegated_job_is_scoped_to_the_named_chat(self):
        shown = {"boss": "boss", "used": 1, "slots": 8, "minions": []}
        with mock.patch.object(server.chains, "view", return_value=shown) as view:
            answer = await self.client.get("/chat/minions?session=boss", headers=self.origin())
        self.assertEqual(await answer.json(), {"chain": shown, "subagents": []})  # not a Claude chat: none
        view.assert_called_once_with("boss")

        with mock.patch.object(server.chains, "note_phone_view") as note, \
                mock.patch.object(server.chains, "view", return_value=shown):
            answer = await self.client.post("/chat/minions/view", json={"session": "child"}, headers=self.origin())
        self.assertEqual(await answer.json(), {"chain": shown, "subagents": []})
        note.assert_called_once_with("child")

    async def test_a_claude_chats_own_helpers_ride_along_and_never_break_the_strip(self):
        helper = {"id": "a1", "description": "Measuring", "kind": "subagent", "state": "running", "started": 1, "last": 2}
        with mock.patch.object(server.chains, "view", return_value={}), \
                mock.patch.object(server.subagents, "helpers", return_value=[helper]) as helpers:
            answer = await self.client.get("/chat/minions?session=claude-x", headers=self.origin())
        self.assertEqual(await answer.json(), {"chain": None, "subagents": [helper]})
        helpers.assert_called_once_with("claude-x")
        with mock.patch.object(server.chains, "view", return_value={}), \
                mock.patch.object(server.subagents, "helpers", side_effect=OSError("odd")):
            answer = await self.client.get("/chat/minions?session=claude-x", headers=self.origin())
        self.assertEqual(await answer.json(), {"chain": None, "subagents": []})

    async def test_end_or_keep_requires_a_waiting_request(self):
        with mock.patch.object(server.chains, "decide_end", return_value=True) as decide:
            answer = await self.client.post("/chat/minions/decide", json={"session": "child", "decision": "keep"},
                                            headers=self.origin())
        self.assertEqual(await answer.json(), {"ok": True})
        decide.assert_called_once_with("child", "keep")

        with mock.patch.object(server.chains, "decide_end", return_value=True), \
                mock.patch.object(server.minions, "end_session", return_value=0) as ended:
            answer = await self.client.post("/chat/minions/decide", json={"session": "child", "decision": "end"},
                                            headers=self.origin())
        self.assertEqual(await answer.json(), {"ok": True})
        ended.assert_called_once_with("child", force=True)


class ToAgent(unittest.TestCase):
    def test_an_unreachable_agent_gets_nothing(self):
        with mock.patch.object(server, "recent_speakers", return_value=[]), \
                mock.patch.object(server.routing, "deliver") as deliver:
            got, why = server.to_agent("hi", "claude-1", server.Heard.TEXTED, False)
            self.assertEqual(got, "")
            self.assertIn("isn't reachable", why)
            deliver.assert_not_called()

    def test_typed_to_exactly_that_agent(self):
        target = mock.Mock(session="claude-1", spoken=lambda: "Claude")
        with mock.patch.object(server, "recent_speakers", return_value=[target]), \
                mock.patch.object(server.routing, "reachable", return_value=True), \
                mock.patch.object(server.routing, "place", return_value=""), \
                mock.patch.object(server.routing, "switch_to") as switch, \
                mock.patch.object(server.routing, "deliver", return_value=True) as deliver:
            self.assertEqual(server.to_agent("hi", "claude-1", server.Heard.TEXTED, False), ("Claude", ""))
            deliver.assert_called_once_with("hi", server.Heard.TEXTED, to=target, urgent=False)
            switch.assert_not_called()


class Owns(unittest.TestCase):
    def test_a_missing_pid_does_not_own_the_live_pane(self):
        server.write_json(server.SPAWNED, {"panes": [{"pane": "%1", "pid": 9}]}, private=True)
        with mock.patch.object(server.tmux, "pane_pids", return_value={"%1": 9}):
            self.assertFalse(server.owns("%1", 0))
            self.assertFalse(server.owns("%1", 7))
            self.assertTrue(server.owns("%1", 9))

    def test_an_unanswered_tmux_does_not_own_a_saved_pid(self):
        server.write_json(server.SPAWNED, {"panes": [{"pane": "%1", "pid": 9}]}, private=True)
        with mock.patch.object(server.tmux, "pane_pids", return_value=None):
            self.assertFalse(server.owns("%1", 9))
        self.assertEqual(server.read_json(server.SPAWNED).get("panes"), [{"pane": "%1", "pid": 9}])

    def test_a_replaced_process_is_not_owned(self):
        server.write_json(server.SPAWNED, {"panes": [{"pane": "%1", "pid": 9}]}, private=True)
        with mock.patch.object(server.tmux, "pane_pids", return_value={"%1": 12}):
            self.assertFalse(server.owns("%1", 9))
            self.assertFalse(server.owns("%1", 12))


class SilentMicTest(unittest.TestCase):
    """The phone's mic arriving as exact zeros (a silenced capture): told once per spell, after MIC_SILENT_AFTER."""

    ZERO, SOUND = bytes(640), b"\x00\x01" * 320

    def test_told_once_after_the_threshold(self):
        silent = server.SilentMic()
        self.assertFalse(silent.feed(self.ZERO, 100.0))
        self.assertFalse(silent.feed(self.ZERO, 100.0 + server.MIC_SILENT_AFTER - 0.1))
        self.assertTrue(silent.feed(self.ZERO, 100.0 + server.MIC_SILENT_AFTER))
        self.assertFalse(silent.feed(self.ZERO, 200.0))  # not again in the same spell

    def test_real_sound_ends_the_spell(self):
        silent = server.SilentMic()
        silent.feed(self.ZERO, 0.0)
        self.assertTrue(silent.feed(self.ZERO, 5.0))
        self.assertFalse(silent.feed(self.SOUND, 6.0))
        self.assertFalse(silent.feed(self.ZERO, 7.0))
        self.assertTrue(silent.feed(self.ZERO, 7.0 + server.MIC_SILENT_AFTER))  # a new spell is told again

    def test_quiet_sound_is_not_silence(self):
        silent = server.SilentMic()
        for t in range(10):
            self.assertFalse(silent.feed(self.SOUND, float(t)))

    def test_the_phones_note_tells_once(self):
        silent = server.SilentMic()
        self.assertTrue(silent.phone_says({"note": "mic silent"}))
        self.assertFalse(silent.phone_says({"note": "mic silent", "restarted": True}))
        self.assertFalse(silent.feed(self.ZERO, 1e9))  # already told in this spell

    def test_from_page_says_it_once(self):
        import asyncio
        silent = server.SilentMic()
        with mock.patch.object(server, "tell_mic_silent") as tell:
            async def go():
                await server.from_page({"note": "mic silent"}, server.Ear(), silent)
                await server.from_page({"note": "mic silent", "restarted": True}, server.Ear(), silent)
                await asyncio.sleep(0.05)
            asyncio.run(go())
        tell.assert_called_once_with()


class ClosedHow(unittest.TestCase):
    def test_says_who_closed_and_why(self):
        ws = mock.Mock(close_code=1006, exception=lambda: ConnectionResetError("reset by peer"))
        self.assertEqual(server.closed_how(ws, False), "close code 1006, error ConnectionResetError: reset by peer")
        ws = mock.Mock(close_code=1000, exception=lambda: None)
        self.assertEqual(server.closed_how(ws, True), "closed by the laptop")


if __name__ == "__main__":
    unittest.main()


class SilentWhilePhoneSpeaks(unittest.TestCase):
    """The speaker monitor's idle silence isn't sent while the phone speaks talkback itself (2026-11-22 stall)."""

    def test_zero_chunks_skipped_only_while_the_phone_renders(self):
        from call import server
        quiet, sound = bytes(640), b"\x00\x01" * 320
        self.assertTrue(server.silent_while_phone_speaks(quiet, True))
        self.assertFalse(server.silent_while_phone_speaks(sound, True))
        self.assertFalse(server.silent_while_phone_speaks(quiet, False))
