"""An agent in Cursor's terminal panel: found from a fake /proc, typed into through fake bridges on real sockets."""

import contextlib
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import sandbox  # noqa: F401
from voice import resume
from voice.agents import Process
from voice.desktop import Tab, Terminals, cursor_bridge, cursor_term
from voice.desktop.cursor_term import CursorTerminals
from voice.desktop.tmux import WithTmux

MAIN, HOST, SHELL, CLAUDE, OTHER_HOST = 100, 200, 300, 400, 250
TTY = 34817  # /dev/pts/1


class FakeProc:
    """A /proc with only the processes a test makes."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="proc-"))

    def add(self, pid, ppid, words, tty=0, tpgid=-1, start=1000, env=(), exe="", children=(), cpu=0, pgrp=0,
            stdin=None):
        here = self.root / str(pid)
        (here / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
        (here / "fd").mkdir(exist_ok=True)
        (here / "fd" / "0").unlink(missing_ok=True)
        if stdin or tty:
            (here / "fd" / "0").symlink_to(stdin or f"/dev/pts/{tty - 34816}")
        fields = ["S", ppid, pgrp or pid, pid, tty, tpgid, 0, 0, 0, 0, 0, cpu, 0, 0, 0, 20, 0, 1, 0, start]
        (here / "stat").write_text(f"{pid} ({Path(words[0]).name[:15]}) " + " ".join(map(str, fields)) + " 0 0\n")
        (here / "cmdline").write_bytes(b"\0".join(w.encode() for w in words) + b"\0")
        (here / "environ").write_bytes(b"\0".join(e.encode() for e in env) + b"\0")
        (here / "task" / str(pid) / "children").write_text(" ".join(map(str, children)))
        if exe:
            (here / "exe").unlink(missing_ok=True)
            (here / "exe").symlink_to(exe)

    def cursor(self, front=CLAUDE, shell_env=("TERM_PROGRAM=vscode",), agent_words=("claude",), agent_tty=TTY):
        """Cursor's main process, its pty host, a terminal's bash, and in it (in front) an agent."""
        self.add(MAIN, 1, ["/usr/share/cursor/cursor"], exe="/usr/share/cursor/cursor", children=[HOST])
        self.add(HOST, MAIN, ["/proc/self/exe", "--type=utility", "--utility-sub-type=node.mojom.NodeService"],
                 exe="/usr/share/cursor/cursor", children=[SHELL])
        self.add(SHELL, HOST, ["/usr/bin/bash", "--init-file", "shellIntegration-bash.sh"], tty=TTY, tpgid=front,
                 start=5000, env=["TERM_PROGRAM=vscode", "KONSOLE_DBUS_SERVICE=:1.83",
                                  "KONSOLE_DBUS_SESSION=/Sessions/1", *shell_env], children=[CLAUDE])
        self.add(CLAUDE, SHELL, list(agent_words), tty=agent_tty, tpgid=front, start=6000)


class ProcCase(unittest.TestCase):
    def setUp(self) -> None:
        self.proc = FakeProc()
        patcher = mock.patch.object(cursor_term, "PROC", self.proc.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.proc.root, True)
        cursor_term._claims = (0.0, {})
        cursor_term._why.clear()

    @property
    def tab(self) -> Tab:
        return Tab(cursor_term.OWNER, f"{SHELL}@5000")


class Address(ProcCase):
    def test_an_address_is_the_shell_and_its_start_time(self):
        self.assertEqual(cursor_term.parse(Tab("cursor-term", "300@5000")), (300, 5000))
        self.assertEqual(cursor_term.parse(Tab("cursor-term", "300")), (0, 0))
        self.assertEqual(cursor_term.parse(Tab("tmux", "300@5000")), (0, 0))
        self.assertEqual(cursor_term.parse(Tab("cursor-term", "-3@5000")), (0, 0))

    def test_the_agent_in_front_and_its_shell(self):
        self.proc.cursor()
        self.assertEqual(cursor_term.shell_of(CLAUDE), SHELL)
        self.assertEqual(cursor_term.tab_of_shell(SHELL), self.tab)
        self.assertEqual(cursor_term.front_of(self.tab), (CLAUDE, CLAUDE))
        self.assertEqual(cursor_term.tab_for(CLAUDE), self.tab)
        self.assertEqual(cursor_term.shells(), [SHELL])

    def test_a_reused_pid_is_not_that_terminal(self):
        self.proc.cursor()
        self.assertEqual(cursor_term.agent_pid(Tab(cursor_term.OWNER, f"{SHELL}@4999")), 0)

    def test_a_shell_in_front_is_no_agent_even_with_one_behind_it(self):
        self.proc.cursor(front=SHELL)  # `claude &`: bash is in front, and typed words would run as commands
        self.assertEqual(cursor_term.front_of(self.tab), (0, 0))
        self.assertEqual(cursor_term.tab_for(CLAUDE), Tab())

    def test_the_agent_a_wrapper_started(self):
        self.proc.cursor(front=CLAUDE, agent_words=("/usr/bin/npx", "something"))
        self.proc.add(CLAUDE, SHELL, ["/usr/bin/npx", "something"], tty=TTY, tpgid=CLAUDE, start=6000, children=[401])
        self.proc.add(401, CLAUDE, ["node", "/opt/bin/codex"], tty=TTY, tpgid=CLAUDE, start=6001, pgrp=CLAUDE)
        self.assertEqual(cursor_term.front_of(self.tab), (CLAUDE, 401))
        self.assertEqual(cursor_term.tab_for(401), self.tab)

    def test_an_agent_in_another_process_group_is_not_the_wrappers(self):
        self.proc.add(CLAUDE, SHELL, ["/usr/bin/npx", "something"], tty=TTY, tpgid=CLAUDE, start=6000, children=[401])
        self.proc.cursor(front=CLAUDE, agent_words=("/usr/bin/npx", "something"))
        self.proc.add(CLAUDE, SHELL, ["/usr/bin/npx", "something"], tty=TTY, tpgid=CLAUDE, start=6000, children=[401])
        self.proc.add(401, CLAUDE, ["claude"], tty=TTY, tpgid=CLAUDE, start=6001)  # its own group: a job
        self.assertEqual(cursor_term.front_of(self.tab), (0, 0))

    def test_another_program_reading_the_terminal_in_front(self):
        self.proc.cursor()
        self.assertFalse(cursor_term.reading(self.tab, CLAUDE, CLAUDE))
        self.proc.add(450, CLAUDE, ["vim", "/tmp/prompt"], tty=TTY, tpgid=CLAUDE, start=6500, pgrp=CLAUDE)
        self.assertTrue(cursor_term.reading(self.tab, CLAUDE, CLAUDE))  # Ctrl+G: $EDITOR on the agent's terminal
        self.proc.add(450, CLAUDE, ["mcp-server"], start=6500, pgrp=CLAUDE, stdin="pipe:[123]")
        self.assertFalse(cursor_term.reading(self.tab, CLAUDE, CLAUDE))

    def test_the_cursor_editor_in_front_is_no_agent(self):
        self.proc.cursor(agent_words=("/usr/bin/cursor", "."))
        self.assertEqual(cursor_term.agent_pid(self.tab), 0)

    def test_claudes_background_daemon_is_not_this_terminal(self):
        self.proc.cursor()
        self.proc.add(CLAUDE, SHELL, ["claude", "bg-pty-host"], tty=TTY, tpgid=CLAUDE, start=6000, children=[402])
        self.proc.add(402, CLAUDE, ["claude"], tty=TTY, tpgid=CLAUDE, start=6002)
        self.assertEqual(cursor_term.shell_of(402), 0)

    def test_another_terminal_started_from_a_cursor_terminal_is_not_it(self):
        self.proc.cursor(agent_words=("konsole",))  # its own tabs are on other ttys
        self.proc.add(410, CLAUDE, ["/usr/bin/bash"], tty=34818, tpgid=411, start=7000, children=[411])
        self.proc.add(411, 410, ["claude"], tty=34818, tpgid=411, start=7001)
        self.assertEqual(cursor_term.shell_of(411), 0)

    def test_no_terminal_without_cursors_pty_host(self):
        self.proc.cursor()
        self.proc.add(HOST, MAIN, ["/usr/bin/konsole"], exe="/usr/bin/konsole", children=[SHELL])
        self.assertEqual(cursor_term.shell_of(CLAUDE), 0)
        self.assertEqual(cursor_term.shells(), [])


class Caller(ProcCase):
    """2026-11-23: an agent in Cursor's terminal inherits Konsole's variables; they must never name its tab."""

    def inner(self) -> mock.Mock:
        inner = mock.Mock(spec=Terminals)
        inner.tab_of_caller.return_value = Tab(":1.83", "/Sessions/1")
        return inner

    def test_the_cursor_terminal_wins_over_inherited_konsole_variables(self):
        self.proc.cursor()
        inner = self.inner()
        with mock.patch("voice.agents.nearest_agent", return_value=Process(CLAUDE, ["claude"])):
            self.assertEqual(CursorTerminals(inner).tab_of_caller(), self.tab)
        inner.tab_of_caller.assert_not_called()

    def test_in_a_cursor_terminal_but_not_its_agent_no_tab_at_all(self):
        self.proc.cursor()
        self.proc.add(CLAUDE + 50, CLAUDE, ["claude"], tty=TTY, start=6100)  # a Claude started by the one in front
        inner = self.inner()
        with mock.patch("voice.agents.nearest_agent", return_value=Process(CLAUDE + 50, ["claude"])):
            self.assertEqual(CursorTerminals(inner).tab_of_caller(), Tab())
        inner.tab_of_caller.assert_not_called()

    def test_outside_cursor_the_desktops_own_terminal(self):
        inner = self.inner()
        with mock.patch("voice.agents.nearest_agent", return_value=Process(999, ["claude"])):
            self.assertEqual(CursorTerminals(inner).tab_of_caller(), Tab(":1.83", "/Sessions/1"))

    def test_tmux_inside_a_cursor_terminal_is_the_pane(self):
        self.proc.cursor()
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%7"}):
            self.assertEqual(WithTmux(CursorTerminals(self.inner())).tab_of_caller(), Tab("tmux", "%7"))

    def test_other_tabs_go_to_the_desktop(self):
        inner = self.inner()
        terminals = CursorTerminals(inner)
        terminals.type_line(Tab(":1.83", "/Sessions/1"), "hi", again=True)
        inner.type_line.assert_called_once_with(Tab(":1.83", "/Sessions/1"), "hi", again=True, taken=None)


class FindRunning(ProcCase):
    def test_a_chat_in_a_cursor_terminal_is_found_there(self):
        me = os.getpid()
        agent = mock.Mock(agent="claude", is_editor=False)
        cursor_tab = Tab(cursor_term.OWNER, "300@5000")
        with mock.patch.object(resume, "_is_chat", side_effect=lambda a, c, pid, e, cmd: pid == me), \
                mock.patch.object(resume, "tab_of", return_value=Tab(":1.83", "/Sessions/1")), \
                mock.patch.object(resume, "runs_in", return_value=False), \
                mock.patch.object(resume, "_tmux_pane", return_value=Tab()), \
                mock.patch.object(cursor_term, "tab_for", return_value=cursor_tab) as found, \
                mock.patch("voice.agents.process", return_value=agent):
            self.assertEqual(resume.find_running("claude", "7f867b40-b715-4c23-92d6-66484e6c3381"), [(me, cursor_tab)])
        found.assert_called_once_with(me)


class Bridge:
    """A Cursor window's bridge: a note and a socket in the bridge folder, answering with `answer(request)`."""

    def __init__(self, folder: Path, name: str, answer, version="0.2.0", pid=None, suffix=".sock") -> None:
        self.asked: list[dict] = []
        self.note = folder / f"{name}.json"
        window = {"pid": pid or os.getpid(), "workspace": "ws", "folders": []}
        if version:
            window["version"] = version
        if suffix == ".sock":
            self.note.write_text(json.dumps(window))
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(self.note.with_suffix(suffix)))
        self.server.listen(8)
        self.answer = answer
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    part = conn.recv(65536)
                    if not part:
                        break
                    data += part
                asked = json.loads(data)
                self.asked.append(asked)
                conn.sendall(json.dumps(self.answer(asked)).encode() + b"\n")

    def close(self) -> None:
        self.server.close()


def lists(*pids):
    def answer(asked):
        if asked.get("terminals"):
            return {"ok": True, "terminals": [{"pid": p, "name": "bash"} for p in pids]}
        return {"ok": True}
    return answer


class Typing(ProcCase):
    def setUp(self) -> None:
        super().setUp()
        self.folder = Path(tempfile.mkdtemp(prefix="hb-", dir="/tmp"))  # a socket path must stay short
        self.addCleanup(shutil.rmtree, self.folder, True)
        patcher = mock.patch.object(cursor_bridge, "DIR", self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.proc.cursor()
        # The bridges' notes name this test's pid: here, an extension host of the same Cursor
        self.proc.add(os.getpid(), MAIN, ["/proc/self/exe", "--type=utility"], exe="/usr/share/cursor/cursor")

    def bridge(self, name, answer, **kw) -> Bridge:
        made = Bridge(self.folder, name, answer, **kw)
        self.addCleanup(made.close)
        return made

    def test_one_window_lists_the_terminal_and_types_into_it(self):
        self.bridge("1", lists(999))
        mine = self.bridge("2", lists(SHELL, 998))
        self.assertTrue(cursor_term.type_line(self.tab, "line one\n\nline \x1b[201~three\r\n"))
        typed = mine.asked[-1]["type"]
        self.assertEqual(typed["text"], "line one\n\nline [201~three\n")  # control characters stripped here too
        self.assertEqual((typed["shell"], typed["shell_born"], typed["front"], typed["front_born"], typed["agent"],
                          typed["agent_born"]), (SHELL, 5000, CLAUDE, 6000, CLAUDE, 6000))
        self.assertTrue(typed["enter"])

    def test_no_window_lists_it(self):
        self.bridge("1", lists(999))
        self.assertFalse(cursor_term.type_line(self.tab, "hi"))
        self.assertIn("no Cursor window lists that terminal", cursor_term.why_not(self.tab))

    def test_two_windows_list_it(self):
        self.bridge("1", lists(SHELL))
        self.bridge("2", lists(SHELL))
        self.assertFalse(cursor_term.type_line(self.tab, "hi"))
        self.assertIn("two Cursor windows", cursor_term.why_not(self.tab))

    def test_the_bridges_refusal_is_the_reason(self):
        self.bridge("1", lambda asked: {"ok": True, "terminals": [{"pid": SHELL}]} if asked.get("terminals")
                    else {"ok": False, "why": "another program is in front"})
        self.assertFalse(cursor_term.type_line(self.tab, "hi"))
        self.assertEqual(cursor_term.why_not(self.tab), "another program is in front")

    def test_nothing_is_asked_with_a_shell_in_front(self):
        self.proc.cursor(front=SHELL)
        mine = self.bridge("1", lists(SHELL))
        self.assertFalse(cursor_term.type_line(self.tab, "rm -rf ~"))
        self.assertEqual(mine.asked, [])
        self.assertFalse(cursor_term.interrupt(self.tab))

    def test_escape_takes_the_same_check(self):
        mine = self.bridge("1", lists(SHELL))
        self.assertTrue(cursor_term.interrupt(self.tab))
        self.assertEqual(mine.asked[-1], {"escape": {"shell": SHELL, "shell_born": 5000, "front": CLAUDE,
                                                     "front_born": 6000, "agent": CLAUDE, "agent_born": 6000}})

    def test_no_answer_after_sending_may_have_been_typed(self):
        self.bridge("1", lambda asked: {"ok": True, "terminals": [{"pid": SHELL}]} if asked.get("terminals") else {})
        self.assertTrue(cursor_term.type_line(self.tab, "hi"))  # not refused: sending again could type it twice
        self.assertIn("“ws” got the words but didn't say whether it typed them", cursor_term.why_not(self.tab))

    def test_an_editor_the_agent_opened_is_not_typed_into(self):
        mine = self.bridge("1", lists(SHELL))
        self.proc.add(450, CLAUDE, ["vim"], tty=TTY, tpgid=CLAUDE, start=6500, pgrp=CLAUDE)
        self.assertFalse(cursor_term.type_line(self.tab, "hi"))
        self.assertIn("reading that terminal", cursor_term.why_not(self.tab))
        self.assertEqual(mine.asked, [])

    def test_steer_presses_enter_through_the_bridge(self):
        mine = self.bridge("1", lists(SHELL))
        with mock.patch("voice.desktop.tmux.steer_after", side_effect=lambda press, **kw: press()):
            self.assertTrue(cursor_term.type_line(self.tab, "hi", again=True, taken=lambda: False))
        self.assertIn("enter", mine.asked[-1])

    def old_window(self, reload=None, listed=(SHELL,), name="1"):
        """A window with the older bridge (it can't list terminals) and the reload helper (which can)."""
        self.bridge(name, lambda asked: {"ok": False, "why": "not a chat id"}, version="")

        def helper(asked):
            if asked.get("terminals"):
                return {"ok": True, "terminals": [{"pid": p} for p in listed]}
            return reload(asked) if reload else {"ok": True}
        return self.bridge(name, helper, version="", suffix=".reload")

    def test_an_older_bridge_is_reloaded_in_the_background_and_answered_at_once(self):
        self.old_window()
        with mock.patch.object(cursor_term, "installed_version", return_value=(0, 2, 0)), \
                mock.patch.object(cursor_term, "_start_reload") as started:
            self.assertFalse(cursor_term.type_line(self.tab, "hi"))
            self.assertIn("say it again in a moment", cursor_term.why_not(self.tab))
            started.assert_called_once_with(self.tab)
            self.assertFalse(cursor_term.type_line(self.tab, "hi"))  # while it checks: not started twice
            started.assert_called_once()

    def test_the_background_reload_runs_apart(self):
        with mock.patch("subprocess.Popen") as popen:
            cursor_term._start_reload(self.tab)
        argv, kw = popen.call_args
        self.assertEqual(argv[0][1:], ["-m", "voice.desktop.cursor_term", "reload", self.tab.address])
        self.assertTrue(kw["start_new_session"])

    def test_the_background_reload_of_a_working_window(self):
        helper = self.old_window()
        with mock.patch.object(cursor_term, "window_busy", return_value="a Cursor chat in it is working"):
            self.assertEqual(cursor_term.reload_now(self.tab.address), 1)
        self.assertEqual([a for a in helper.asked if "reload" in a], [])
        with mock.patch.object(cursor_term, "installed_version", return_value=(0, 2, 0)), \
                mock.patch.object(cursor_term, "_start_reload") as started:
            self.assertFalse(cursor_term.type_line(self.tab, "hi"))
        self.assertIn("won't reload it while a Cursor chat in it is working", cursor_term.why_not(self.tab))
        started.assert_not_called()

    def test_the_background_reload_of_an_idle_window_then_typing(self):
        def reload(asked):  # the window reloads: its old note goes, a new bridge comes up
            (self.folder / "1.json").unlink()
            self.new = Bridge(self.folder, "2", lists(SHELL))
            return {"ok": True}
        helper = self.old_window(reload)
        with mock.patch.object(cursor_term, "window_busy", return_value=""):
            self.assertEqual(cursor_term.reload_now(self.tab.address), 0)
        self.addCleanup(self.new.close)
        self.assertIn({"reload": True}, helper.asked)
        self.assertEqual(cursor_term._reloads()[str(MAIN)]["state"], "reloaded")
        self.assertTrue(cursor_term.type_line(self.tab, "hi"), cursor_term.why_not(self.tab))
        self.assertEqual(self.new.asked[-1]["type"]["text"], "hi")

    def test_a_reload_just_done_by_another_is_not_done_again(self):
        helper = self.old_window()
        cursor_term._set_reload(MAIN, "reloaded")
        with mock.patch.object(cursor_term, "window_busy", return_value="") as checked:
            self.assertEqual(cursor_term.reload_now(self.tab.address), 0)
        checked.assert_not_called()
        self.assertEqual([a for a in helper.asked if "reload" in a], [])

    def test_a_reload_that_ends_early_tells_nobody_it_is_reloading(self):
        self.bridge("1", lists(999))  # the terminal isn't there any more: nothing to reload
        cursor_term._set_reload(MAIN, "checking")
        self.assertEqual(cursor_term.reload_now(self.tab.address), 0)
        self.assertNotIn(str(MAIN), cursor_term._reloads())
        cursor_term._set_reload(MAIN, "busy", "a chat is working")  # an outcome stays
        cursor_term.reload_now(self.tab.address)
        self.assertEqual(cursor_term._reloads()[str(MAIN)]["state"], "busy")
        self.assertEqual(sorted(p.name for p in self.folder.iterdir() if "reloads" in p.name),
                         ["reloads", "reloads.lock"])  # written whole: no temp file left behind

    def test_a_reload_that_finds_another_at_it_leaves_its_record(self):
        cursor_term._set_reload(MAIN, "checking")

        @contextlib.contextmanager
        def taken(wait=5.0):
            yield False
        with mock.patch.object(cursor_term, "_reload_lock", taken):
            self.assertEqual(cursor_term.reload_now(self.tab.address), 1)
        self.assertEqual(cursor_term._reloads()[str(MAIN)]["state"], "checking")  # the running check owns it

    def test_each_delivery_reads_back_its_own_note(self):
        """Two deliveries to one terminal at once: one refused, one typed; neither gets the other's note."""
        self.bridge("1", lists(SHELL))
        refuse = threading.Event()
        typed_first = threading.Event()
        notes = {}
        real = cursor_term.reach

        def reach(tab, reload=True):
            if threading.current_thread().name == "refused":
                typed_first.wait(5)
                return None, "another program is in front"
            return real(tab, reload)

        def deliver(name):
            ok = cursor_term.type_line(self.tab, "hi")
            if name == "typed":
                typed_first.set()
                refuse.wait(5)  # read back only after the other thread wrote its refusal
            else:
                refuse.set()
            notes[name] = (ok, cursor_term.why_not(self.tab))
        with mock.patch.object(cursor_term, "reach", reach):
            threads = [threading.Thread(target=deliver, args=(n,), name=n) for n in ("typed", "refused")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(notes["typed"], (True, ""))
        self.assertEqual(notes["refused"], (False, "another program is in front"))

    def test_a_reload_that_cant_start_is_a_refusal(self):
        self.old_window()
        with mock.patch.object(cursor_term, "installed_version", return_value=(0, 2, 0)), \
                mock.patch.object(cursor_term, "_start_reload", side_effect=OSError(24, "Too many open files")):
            self.assertFalse(cursor_term.type_line(self.tab, "hi"))
        self.assertIn("couldn't start (Too many open files)", cursor_term.why_not(self.tab))
        self.assertNotIn(str(MAIN), cursor_term._reloads())

    def test_an_older_window_that_doesnt_hold_the_terminal_is_not_reloaded(self):
        self.old_window(listed=(999,))
        with mock.patch.object(cursor_term, "installed_version", return_value=(0, 2, 0)), \
                mock.patch.object(cursor_term, "_start_reload") as started:
            self.assertFalse(cursor_term.type_line(self.tab, "hi"))
        started.assert_not_called()
        self.assertIn("older Henchline bridge without the reload helper", cursor_term.why_not(self.tab))

    def test_no_reload_when_cursor_has_only_the_old_bridge_installed(self):
        self.old_window()
        with mock.patch.object(cursor_term, "installed_version", return_value=(0, 1, 15)), \
                mock.patch.object(cursor_term, "_start_reload") as started:
            self.assertFalse(cursor_term.type_line(self.tab, "hi"))
        self.assertIn("./install.sh agents", cursor_term.why_not(self.tab))
        started.assert_not_called()

    def test_the_installed_version_is_the_one_cursor_loads(self):
        folder = Path.home() / ".cursor/extensions"
        folder.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, folder, True)
        entry = {"identifier": {"id": "henchline.henchline-cursor-bridge"}}
        (folder / "extensions.json").write_text(json.dumps([
            {**entry, "version": "0.1.15", "relativeLocation": "henchline.henchline-cursor-bridge-0.1.15"},
            {**entry, "version": "0.2.0", "relativeLocation": "henchline.henchline-cursor-bridge-0.2.0"},
            {"identifier": {"id": "other.x"}, "version": "9.0.0", "relativeLocation": "other.x-9.0.0"}]))
        self.assertEqual(cursor_term.installed_version(), (0, 2, 0))
        (folder / ".obsolete").write_text('{"henchline.henchline-cursor-bridge-0.2.0": true}')
        self.assertEqual(cursor_term.installed_version(), (0, 1, 15))

    def test_a_window_of_another_cursor_is_not_asked(self):
        self.proc.add(os.getpid(), 77, ["/proc/self/exe", "--type=utility"], exe="/usr/share/cursor/cursor")
        mine = self.bridge("1", lists(SHELL))
        self.assertFalse(cursor_term.type_line(self.tab, "hi"))
        self.assertEqual(mine.asked, [])

    def test_agent_tabs_lists_only_reachable_terminals(self):
        self.bridge("1", lists(SHELL))
        self.assertEqual(cursor_term.agent_tabs(), [(self.tab, CLAUDE)])
        cursor_term._claims = (0.0, {})
        (self.folder / "1.json").unlink()
        self.assertEqual(cursor_term.agent_tabs(), [])
        self.old_window(name="3")  # an older bridge whose helper lists it: reachable once reloaded
        cursor_term._claims = (0.0, {})
        self.assertEqual(cursor_term.agent_tabs(), [(self.tab, CLAUDE)])


class Busy(ProcCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = Path.home()

    def chat(self, folder: str, chat: str, age: float) -> None:
        where = self.home / ".cursor/projects" / cursor_bridge.project_of(folder) / "agent-transcripts" / chat
        where.mkdir(parents=True, exist_ok=True)
        log = where / f"{chat}.jsonl"
        log.write_text("{}\n")
        then = time.time() - age
        os.utime(log, (then, then))
        os.utime(where, (then, then))

    def test_a_chat_written_a_moment_ago_is_working(self):
        self.chat("/w/a", "11111111-1111-1111-1111-111111111111", 5)
        with mock.patch("voice.desktop.cursor_cdp.generating", return_value=False):
            self.assertIn("working", cursor_term.window_busy({"pid": 1, "workspace": "a", "folders": ["/w/a"]}))

    def test_the_page_saying_a_chat_generates_is_working(self):
        with mock.patch("voice.desktop.cursor_cdp.generating", return_value=True):
            self.assertIn("working", cursor_term.window_busy({"pid": 1, "workspace": "a", "folders": ["/w/b"]}))

    def test_without_the_page_or_its_chats_it_is_unsure(self):
        self.chat("/w/a", "11111111-1111-1111-1111-111111111111", 7 * 3600)
        with mock.patch("voice.desktop.cursor_cdp.generating", return_value=None):
            self.assertTrue(cursor_term.window_busy({"pid": 1, "workspace": "a", "folders": ["/w/a"]}))
        with mock.patch("voice.desktop.cursor_cdp.generating", return_value=False):
            self.assertTrue(cursor_term.window_busy({"pid": 1, "workspace": "a", "folders": ["/w/none"]}))

    def test_a_quiet_window_with_idle_terminals_may_reload(self):
        self.chat("/w/c", "22222222-2222-2222-2222-222222222222", 7 * 3600)
        self.proc.cursor()
        with mock.patch("voice.desktop.cursor_cdp.generating", return_value=False), \
                mock.patch.object(cursor_term, "agent_busy", return_value="") as asked:
            self.assertEqual(cursor_term.window_busy({"pid": 1, "workspace": "c", "folders": ["/w/c"]}), "")
        asked.assert_called_once_with(CLAUDE, "claude")

    def test_an_agent_mid_turn_in_any_terminal_is_working(self):
        self.chat("/w/d", "44444444-4444-4444-4444-444444444444", 7 * 3600)
        self.proc.cursor()
        with mock.patch("voice.desktop.cursor_cdp.generating", return_value=False), \
                mock.patch.object(cursor_term, "agent_busy", return_value="a Claude in one of them is working"):
            self.assertIn("Claude", cursor_term.window_busy({"pid": 1, "workspace": "c", "folders": ["/w/d"]}))

    def test_no_reload_when_it_would_close_the_terminals(self):
        off = '{\n  // mine\n  "terminal.integrated.enablePersistentSessions": false\n}'
        user = cursor_term.USER / "settings.json"
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text(off)
        self.addCleanup(user.unlink)
        self.assertIn("terminals would close", cursor_term._terminals_close({"folders": []}))
        user.write_text("{}")
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / ".vscode").mkdir()
        (folder / ".vscode/settings.json").write_text(off)
        self.assertIn("terminals would close", cursor_term._terminals_close({"folders": [str(folder)]}))
        (folder / ".vscode/settings.json").write_text("{}")
        self.assertEqual(cursor_term._terminals_close({"folders": [str(folder)]}), "")
        # a workspace file it can't find, and profiles it can't tell apart: unsure
        self.assertTrue(cursor_term._terminals_close({"folders": [], "workspace": "proj (Workspace)"}))
        (cursor_term.USER / "profiles/abc").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, cursor_term.USER / "profiles")
        self.assertIn("profiles", cursor_term._terminals_close({"folders": []}))

    def claude_log(self, *entries) -> None:
        chat = "33333333-3333-3333-3333-333333333333"
        where = self.home / ".claude/projects/p"
        where.mkdir(parents=True, exist_ok=True)
        log = where / f"{chat}.jsonl"
        log.write_text("".join(json.dumps(e) + "\n" for e in entries))
        then = time.time() - 60
        os.utime(log, (then, then))
        return chat

    def test_claude_by_its_own_log(self):
        prompt = {"type": "user", "message": {"role": "user", "content": "do it"}}
        chat = self.claude_log(prompt)
        with mock.patch.object(resume, "claude_record", return_value=chat):
            self.assertIn("working", cursor_term.agent_busy(CLAUDE, "claude"))
        self.claude_log(prompt, {"type": "assistant", "message": {"stop_reason": "end_turn"}})
        with mock.patch.object(resume, "claude_record", return_value=chat):
            self.assertEqual(cursor_term.agent_busy(CLAUDE, "claude"), "")
        with mock.patch.object(resume, "claude_record", return_value=None):
            self.assertTrue(cursor_term.agent_busy(CLAUDE, "claude"))  # unreadable: busy

    def test_an_agent_it_can_not_read_is_working(self):
        self.assertTrue(cursor_term.agent_busy(CLAUDE, "gemini"))
        self.proc.cursor()
        self.assertTrue(cursor_term.agent_busy(CLAUDE, "cursor"))  # no store open: unsure


class Delivering(unittest.TestCase):
    """What the user hears about words typed into a Cursor terminal (handsfree/core/routing.py)."""

    def deliver(self, typed: bool, why: str, heard=None):
        import routing
        from voice.state import Speaker
        tab = Tab(cursor_term.OWNER, "300@5000")
        speaker = Speaker("claude-x", "claude", "job", tab)
        terms = mock.Mock()
        terms.agent_running.return_value = True
        terms.type_line.return_value = typed
        with mock.patch.object(routing, "place", return_value=""), \
                mock.patch.object(routing, "desktop", return_value=mock.Mock(terminals=terms)), \
                mock.patch.object(cursor_term, "why_not", return_value=why), \
                mock.patch.object(routing, "say") as said:
            got = routing.deliver_words("hello", heard or routing.Heard.TEXTED, to=speaker)
        return got, said

    def test_words_the_bridge_never_confirmed_may_have_been_typed(self):
        got, said = self.deliver(True, "Cursor's window “ws” got the words but didn't say whether it typed them")
        self.assertTrue(got)
        self.assertIn("I may have typed that", got.why)
        self.assertIn("Check before sending it again", got.why)
        said.assert_called_once_with(got.why)

    def test_typed_and_confirmed_is_plain(self):
        got, said = self.deliver(True, "")
        self.assertEqual((bool(got), got.why), (True, ""))
        said.assert_not_called()

    def test_a_refusal_carries_the_bridges_reason(self):
        got, _ = self.deliver(False, "another program is in front")
        self.assertFalse(got)
        self.assertIn("another program is in front", got.why)


if __name__ == "__main__":
    unittest.main()
