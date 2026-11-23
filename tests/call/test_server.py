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
        app.router.add_post("/crew/stop", server.crew_stop)
        return app

    def origin(self):
        return {"Origin": f"https://{self.server.host}:{self.server.port}"}

    async def test_a_stop_says_why_cursor_refused_from_the_bridges_own_answer(self):
        from voice.delivery import Delivery, Refusal
        from voice.state import Speaker
        chat = "12345678-1234-1234-1234-123456789abc"
        editor = Speaker(f"cursor-{chat}", "cursor", "job")
        notes = {Refusal.NOT_WORKING: "just finished", Refusal.NOT_A_CHAT_ID: "older Henchline bridge",
                 Refusal.OTHER_CHAT_SELECTED: "Another chat is selected", Refusal.NO_WINDOW: "Couldn't stop it"}
        for refusal, note in notes.items():
            with mock.patch.object(server, "recent_speakers", return_value=[editor]), \
                    mock.patch.object(server.cursor_bridge, "working", return_value=True), \
                    mock.patch.object(server.cursor_bridge, "ask_selected", return_value=Delivery.refused(refusal)):
                answer = await self.client.post("/crew/stop", json={"session": editor.session}, headers=self.origin())
            self.assertEqual(answer.status, 409, refusal)
            self.assertIn(note, (await answer.json())["note"])

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
            deliver.assert_called_once_with("hi", server.Heard.TEXTED, to=target, urgent=False, sender=None,
                                            ear=server.Ears.LAPTOP, said_at=None)
            switch.assert_not_called()


class Others(unittest.TestCase):
    """The chats heard or texted lately that aren't running: named, with why they are off the crew (2026-11-22)."""

    def setUp(self):
        import time
        self.now = time.time()
        for path in server.TEXTS.glob("*.json"):
            path.unlink()
        server.TEXTS.mkdir(parents=True, exist_ok=True)

    def text(self, tid, session, agent, ago):
        server.write_json(server.TEXTS / f"{tid}.json", {"id": tid, "session": session, "agent": agent,
                                                          "at": self.now - ago, "words": "hi"})

    def others(self, speakers, listed=(), over=(), launched=None):
        with mock.patch.object(server, "recent_speakers", return_value=speakers), \
                mock.patch.object(server, "chat_named", return_value=""), \
                mock.patch.object(server.chains, "launched_where", return_value=launched or {}):
            return {o["session"]: (o["name"], o["state"]) for o in server.others(set(listed), list(over))}

    def test_a_closed_chat_is_named_by_its_folder_and_an_ended_helper_says_so(self):
        from voice.state import Speaker
        closed = Speaker("claude-1", "claude", "garden-planner", at=self.now - 3600)
        helper = Speaker("claude-2", "claude", "kite-designer", at=self.now - 60)
        running = Speaker("claude-3", "claude", "henchline", at=self.now)
        old = Speaker("claude-4", "claude", "old", at=self.now - 5 * 86400)
        found = self.others([closed, helper, running, old], listed={"claude-3"}, over={"claude-2"})
        self.assertEqual(found, {"claude-1": ("Claude, in garden planner", "gone"),
                                 "claude-2": ("Claude, in kite designer", "ended")})

    def test_a_chat_known_only_by_its_texts_is_named_from_its_key(self):
        self.text("a" * 16, "codex-_home_me", "codex", 600)
        self.text("b" * 16, "reminders-x", "reminders", 600)  # a timer's: not an agent
        self.text("c" * 16, "claude-9", "claude", 4 * 86400)  # too old
        with mock.patch.object(server, "trusted_folders", return_value=[]), \
                mock.patch.object(server.Path, "home", return_value=server.Path("/home/me")):
            found = self.others([])
        self.assertEqual(found, {"codex-_home_me": ("Codex, in home", "gone")})

    def test_an_ended_helper_without_a_record_is_named_from_its_launch_or_its_transcript(self):
        kid = "claude-12345678-1234-4234-8234-123456789abc"
        found = self.others([], over=[kid], launched={kid: "pc_diagnostics"})
        self.assertEqual(found, {kid: ("Claude, in pc diagnostics", "ended")})
        with mock.patch("voice.resume.folder_of", return_value=server.Path("/srv/website")):
            found = self.others([], over=[kid])
        self.assertEqual(found, {kid: ("Claude, in website", "ended")})
        with mock.patch("voice.resume.folder_of", return_value=None):
            self.assertEqual(self.others([], over=[kid]), {kid: ("Claude", "ended")})  # truly unknown: no folder


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


class ConfirmSwitch(unittest.TestCase):
    """A voice switch the user made mid-call is said in the new voice, unless they're talking or a reply waits."""

    OFFER = {"tts": {"engine": server.phonevoice.ENGINE, "model": server.phonevoice.VERSION}, "chosen": True}

    def tearDown(self):
        server.phonevoice.call_over()

    def run_it(self, message, idle=True, queued=(), held=False, last=None):
        phase = server.Phase.IDLE if idle else server.Phase.RECORDING
        with mock.patch.object(server, "phase_now", lambda: (phase,)), \
             mock.patch.object(server.spool, "queued", lambda: list(queued)), \
             mock.patch.object(server.spool, "any_held", lambda: held), \
             mock.patch.object(server, "last_speaker", lambda: last), \
             mock.patch.object(server, "log"), \
             mock.patch.object(server.routing, "say") as say:
            server.phonevoice.offer(message)
            said = server.confirm_switch(message)
        return said, say

    def test_to_the_phone_queued_as_a_phone_clip(self):
        said, say = self.run_it(self.OFFER)
        self.assertEqual(said, server.phonevoice.SAID_PHONE)
        say.assert_called_once_with(server.phonevoice.SAID_PHONE, voice="")

    def test_to_the_laptop_in_the_last_agents_voice(self):
        server.phonevoice.offer(self.OFFER)
        last = server.Speaker("claude-x", "claude", "henchline")
        with mock.patch.object(server.voices, "for_agent", lambda agent: f"voice-of-{agent}"):
            said, say = self.run_it({"tts": "laptop"}, last=last)
        self.assertEqual(said, server.phonevoice.SAID_LAPTOP)
        say.assert_called_once_with(server.phonevoice.SAID_LAPTOP, voice="voice-of-claude")

    def test_skipped_while_the_user_talks_or_a_reply_waits(self):
        for kwargs in ({"idle": False}, {"queued": ["clip"]}, {"held": True}):
            said, say = self.run_it(self.OFFER, **kwargs)
            self.assertEqual(said, "")
            say.assert_not_called()

    def test_an_offer_or_fallback_says_nothing(self):
        for message in ({"tts": self.OFFER["tts"]}, {"tts": "fallback", "reason": "battery 9%"}):
            said, say = self.run_it(message)
            self.assertEqual(said, "")
            say.assert_not_called()

    def test_never_raises(self):
        with mock.patch.object(server.phonevoice, "confirmation", side_effect=OSError("disk")), \
             mock.patch.object(server, "log"):
            self.assertEqual(server.confirm_switch(self.OFFER), "")


class ClosedHow(unittest.TestCase):
    def test_says_who_closed_and_why(self):
        ws = mock.Mock(close_code=1006, exception=lambda: ConnectionResetError("reset by peer"))
        self.assertEqual(server.closed_how(ws, False), "close code 1006, error ConnectionResetError: reset by peer")
        ws = mock.Mock(close_code=1000, exception=lambda: None)
        self.assertEqual(server.closed_how(ws, True), "closed by the laptop")


if __name__ == "__main__":
    unittest.main()


class EarLogsWhereAndHowMuch(unittest.TestCase):
    """Audio lost after the phone's speaker loop: its route and volume, and each clip's loud total, are logged."""

    def test_route_once_and_a_clip_total_when_it_ends(self):
        ear = server.Ear()
        lines = []
        playing = [server.Playing("c1", "claude-x", "claude", "Hi there.")]
        with mock.patch.object(server, "log", lambda _f, line: lines.append(line)), \
             mock.patch.object(server.Playing, "now", classmethod(lambda cls: playing[0])):
            ear.report({"loud_ms": 900, "route": "speaker", "volume": 7, "volume_max": 15})
            ear.report({"loud_ms": 1100, "route": "speaker", "volume": 7, "volume_max": 15})
            playing[0] = None
            ear.report({"loud_ms": 0, "route": "bluetooth (call)", "volume": 3, "volume_max": 15})
        self.assertEqual(sum("plays to speaker, volume 7/15" in line for line in lines), 1)
        self.assertTrue(any("plays to bluetooth (call), volume 3/15" in line for line in lines))
        self.assertIn("call: clip c1: the phone played 2000 ms out loud", lines)


class SilentWhilePhoneSpeaks(unittest.TestCase):
    """The speaker monitor's idle silence isn't sent while the phone speaks talkback itself (2026-11-22 stall)."""

    def test_zero_chunks_skipped_only_while_the_phone_renders(self):
        from call import server
        quiet, sound = bytes(640), b"\x00\x01" * 320
        self.assertTrue(server.silent_while_phone_speaks(quiet, True))
        self.assertFalse(server.silent_while_phone_speaks(sound, True))
        self.assertFalse(server.silent_while_phone_speaks(quiet, False))
