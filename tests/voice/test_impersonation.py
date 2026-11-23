"""One agent can't pass for another: the process tree backs the session a caller claims, `remind --to` reaches only
the caller's own chain, and an agent's reminder is tagged with who set it and never typed as a slash command."""

import importlib.util
import unittest
from unittest import mock

from tests import sandbox  # first: a throwaway HOME
import routing
from voice import agents, chains, reminders
from voice.agents import Process
from voice.desktop import Tab
from voice.state import SPEAKERS, Speaker

hf_spec = importlib.util.spec_from_file_location("handsfree_remind", sandbox.REPO / "handsfree/handsfree.py")
handsfree = importlib.util.module_from_spec(hf_spec)
hf_spec.loader.exec_module(handsfree)

BOSS = "claude-11111111-1111-4111-8111-111111111111"
CHILD = "claude-22222222-2222-4222-8222-222222222222"
OTHER = "claude-33333333-3333-4333-8333-333333333333"


class Vouched(unittest.TestCase):
    def test_a_child_claiming_its_bosses_session_is_refused(self):
        me = Process(4242, ["claude"])
        with mock.patch.object(agents, "nearest_agent", return_value=me), \
                mock.patch.object(agents, "ancestors", return_value=[Process(9, ["bash"]), me]), \
                mock.patch.object(agents, "in_cursor_sandbox", return_value=False), \
                mock.patch("voice.resume.claude_record", return_value=CHILD.removeprefix("claude-")):
            self.assertEqual(agents.vouched("claude", CHILD), "")
            self.assertIn("another claude session", agents.vouched("claude", BOSS))

    def test_another_kind_of_agent_or_none_at_all_is_refused(self):
        with mock.patch.object(agents, "in_cursor_sandbox", return_value=False):
            with mock.patch.object(agents, "nearest_agent", return_value=Process(1, ["codex"])):
                self.assertIn("run by codex", agents.vouched("claude", BOSS))
            with mock.patch.object(agents, "nearest_agent", return_value=None):
                self.assertIn("not run by an agent", agents.vouched("claude", BOSS))

    def test_a_folder_key_must_be_that_process_folder(self):
        me = mock.Mock(agent="codex", is_editor=False, folder=sandbox.REPO, pid=1)
        with mock.patch.object(agents, "nearest_agent", return_value=me), \
                mock.patch.object(agents, "in_cursor_sandbox", return_value=False):
            self.assertEqual(agents.vouched("codex", agents._key("codex", str(sandbox.REPO))), "")
            self.assertTrue(agents.vouched("codex", agents._key("codex", "/elsewhere")))

    def test_a_fresh_cursor_cli_is_known_by_its_pane(self):
        # Not dumpable: no environment, no open store.db, no --resume. A deployed one reports from its own pane
        for path in chains.CHAINS.glob("*.jsonl") if chains.CHAINS.is_dir() else []:
            path.unlink()
        boss, child = "cursor-44444444-4444-4444-8444-444444444444", "cursor-55555555-5555-4555-8555-555555555555"
        sibling = "cursor-66666666-6666-4666-8666-666666666666"
        chain = chains.open(boss)
        chains.append(chain, "launch", parent=boss, pane="%14", pid=392791, depth=2, agent="cursor", where="site",
                      parent_pane="%3", parent_pid=300)
        chains.append(chain, "launch", parent=boss, pane="%15", pid=392800, depth=2, agent="cursor", where="site",
                      parent_pane="%3", parent_pid=300)
        panes = {"%3": 300, "%14": 392791, "%15": 392800}

        def run_in(pid):
            me = Process(pid, ["/home/u/.local/bin/cursor-agent", "--force"])
            return mock.patch.object(agents, "nearest_agent", return_value=me), \
                mock.patch.object(agents, "ancestors", return_value=[me, Process(1000, ["tmux"])])

        with mock.patch.object(agents, "in_cursor_sandbox", return_value=False), \
                mock.patch("voice.desktop.tmux.pane_pids", return_value=panes):
            first, second = run_in(392791)
            with first, second:
                self.assertEqual(agents.vouched("cursor", child), "")  # nothing recorded for it yet
                self.assertIn("another cursor session", agents.vouched("cursor", boss))  # its boss's pane is %3
            chains.bind(child, "%14", 392791)  # its first report
            first, second = run_in(392800)
            with first, second:
                self.assertIn("another cursor session", agents.vouched("cursor", child))  # bound to %14
                self.assertIn("another cursor session", agents.vouched("cursor", boss))
                self.assertEqual(agents.vouched("cursor", sibling), "")
            first, second = run_in(300)
            with first, second:
                self.assertEqual(agents.vouched("cursor", boss), "")
                self.assertIn("another cursor session", agents.vouched("cursor", child))
            first, second = run_in(392791)
            with first, second:
                self.assertEqual(agents.vouched("cursor", child), "")
                self.assertIn("another cursor session", agents.vouched("cursor", sibling))  # %14 is the child's
            with mock.patch("voice.desktop.tmux.pane_pids", return_value={}), first, second:
                self.assertIn("another cursor session", agents.vouched("cursor", child))  # outside tmux: nothing

    def test_the_cli_refuses_an_unbacked_caller(self):
        with mock.patch.object(handsfree, "caller", return_value=Speaker(BOSS, "claude")), \
                mock.patch("voice.agents.vouched", return_value="nope"):
            self.assertIsNone(handsfree.vouched_caller())


class RemindTo(unittest.TestCase):
    def setUp(self):
        for path in chains.CHAINS.glob("*.jsonl") if chains.CHAINS.is_dir() else []:
            path.unlink()
        reminders.FILE.unlink(missing_ok=True)
        chain = chains.open(BOSS)
        chains.append(chain, "launch", parent=BOSS, pane="%7", pid=7, depth=2, agent="claude", where="job")
        chains.bind(CHILD, "%7", 7)
        patches = [mock.patch.object(handsfree.subprocess, "run", return_value=mock.Mock(returncode=0)),
                   mock.patch("voice.agents.run_by_agent", return_value=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def remind(self, me, to):
        target = Speaker(to, "claude", "elsewhere")
        with mock.patch.object(handsfree, "vouched_caller", return_value=me), \
                mock.patch.object(handsfree.routing, "find", return_value=target):
            return handsfree.remind(["--to", "claude in elsewhere", "in", "20m", "/compact"])

    def test_an_agent_reaches_its_boss_and_its_helpers_only(self):
        boss, child = Speaker(BOSS, "claude", "boss"), Speaker(CHILD, "claude", "job")
        self.assertEqual(0, self.remind(child, BOSS))
        self.assertEqual(0, self.remind(boss, CHILD))
        self.assertEqual(self.remind(child, OTHER), 1)
        made = reminders.listed()
        self.assertEqual(len(made), 2)
        self.assertEqual({r["by"] for r in made}, {"agent"})
        self.assertEqual({r["sender"] for r in made}, {"claude in job", "claude in boss"})

    def test_a_script_with_no_agent_may_remind_anyone(self):
        with mock.patch("voice.agents.run_by_agent", return_value=False):
            self.assertEqual(0, self.remind(None, OTHER))
        self.assertEqual(reminders.listed()[0]["sender"], "")

    def test_an_agent_the_tree_does_not_back_sets_nothing(self):
        self.assertEqual(self.remind(None, BOSS), 1)
        self.assertEqual(reminders.listed(), [])


class RemindByName(unittest.TestCase):
    """`remind --to NAME` finds the caller's own helper by its deploy name or pane, and nothing outside its chain."""

    def setUp(self):
        for path in chains.CHAINS.glob("*.jsonl") if chains.CHAINS.is_dir() else []:
            path.unlink()
        reminders.FILE.unlink(missing_ok=True)
        chain = chains.open(BOSS)
        chains.append(chain, "launch", parent=BOSS, pane="%15", pid=15, depth=2, agent="claude",
                      where="kite-designer", name="tests")
        chains.bind(CHILD, "%15", 15)
        chains.append(chain, "launch", parent=BOSS, pane="%16", pid=16, depth=2, agent="codex",
                      where="kite-designer", name="docs")
        patches = [mock.patch.object(handsfree.subprocess, "run", return_value=mock.Mock(returncode=0)),
                   mock.patch("voice.agents.run_by_agent", return_value=True),
                   mock.patch("voice.desktop.tmux.pane_pids", return_value=None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def remind(self, me, to):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with mock.patch.object(handsfree, "vouched_caller", return_value=me), \
                mock.patch.object(handsfree.routing, "find", return_value=None) as find, redirect_stdout(out):
            code = handsfree.remind(["--to", to, "in", "20m", "check the build"])
        return code, out.getvalue(), find

    def test_a_boss_reminds_its_helper_by_name_or_pane(self):
        boss = Speaker(BOSS, "claude", "boss")
        for to in ("tests", "%15"):
            code, _, find = self.remind(boss, to)
            self.assertEqual(code, 0)
            find.assert_not_called()
        self.assertEqual({r["session"] for r in reminders.listed()}, {CHILD})

    def test_a_helper_that_has_not_spoken_yet_is_not_guessed(self):
        code, said, _ = self.remind(Speaker(BOSS, "claude", "boss"), "docs")
        self.assertEqual(code, 1)
        self.assertIn("docs (tmux %16) hasn't spoken yet", said)
        self.assertEqual(reminders.listed(), [])

    def test_a_name_never_reaches_another_bosss_helper(self):
        code, said, find = self.remind(Speaker(OTHER, "claude", "elsewhere"), "tests")
        self.assertEqual(code, 1)
        find.assert_called_once()  # not one of its own: the usual agent lookup, which finds nobody
        self.assertEqual(reminders.listed(), [])


class AgentsReminder(unittest.TestCase):
    def typed(self, text, sender):
        speaker = Speaker(OTHER, "claude", "job", Tab("tmux", "%1"))
        terms = mock.Mock()
        terms.agent_running.return_value = True
        terms.type_line.return_value = True
        with mock.patch.object(routing, "place", return_value=""), \
                mock.patch.object(routing, "desktop", return_value=mock.Mock(terminals=terms)), \
                mock.patch.object(routing, "is_command", return_value=True):
            self.assertTrue(routing.deliver(text, routing.Heard.SCHEDULED, to=speaker, sender=sender))
        return terms.type_line.call_args.args[1]

    def test_never_a_slash_command_and_tagged_with_who_set_it(self):
        self.assertEqual(self.typed("/compact", "claude in job"),
                         "[henchline · scheduled · from claude in job] /compact")
        self.assertEqual(self.typed("/compact", ""), "[henchline · scheduled] /compact")
        self.assertEqual(self.typed("/compact", None), "/compact")  # the user's own, from the phone

    def test_the_reminder_record_says_whose_it_is(self):
        self.assertTrue(reminders.Reminder.of({"id": "1", "by": "phone"}).users)
        self.assertFalse(reminders.Reminder.of({"id": "1", "by": "agent", "sender": "x"}).users)
        self.assertFalse(reminders.Reminder.of({"id": "1", "by": "user"}).users)  # an older --to record


class RecordStaysPut(unittest.TestCase):
    """A process claiming another chat's id never repoints where that chat's replies go, refused or not."""

    def setUp(self):
        SPEAKERS.mkdir(parents=True, exist_ok=True)
        self.path = SPEAKERS / f"{BOSS}.json"
        Speaker(BOSS, "claude", "henchline", Tab("tmux", "%1")).save(self.path)
        self.addCleanup(lambda: self.path.unlink(missing_ok=True))

    def _call(self, verdict: str):
        desk = mock.Mock()
        desk.terminals.tab_of_caller.return_value = Tab("tmux", "%7")
        with mock.patch.object(handsfree, "identity", return_value=("claude", BOSS)), \
                mock.patch("voice.desktop.current", return_value=desk), \
                mock.patch("voice.agents.vouched", return_value=verdict), \
                mock.patch.object(Speaker, "pin", return_value=False):
            handsfree.caller()
        return Speaker.load(self.path).tab

    def test_an_unbacked_claim_leaves_the_tab(self):
        self.assertEqual(self._call("this command runs under another claude session"), Tab("tmux", "%1"))

    def test_the_chat_itself_moves_its_tab(self):
        self.assertEqual(self._call(""), Tab("tmux", "%7"))


if __name__ == "__main__":
    unittest.main()
