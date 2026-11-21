"""voice/subagents.py: a Claude chat's own helpers, from invented records shaped like Claude Code's."""

import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from voice import subagents

UUID = "11111111-2222-3333-4444-555555555555"
SESSION = f"claude-{UUID}"


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def user(t: float, text: str = "do it") -> dict:
    return {"type": "user", "timestamp": iso(t), "message": {"role": "user", "content": text}}


def result(t: float, tool: str = "toolu_1") -> dict:
    return {"type": "user", "timestamp": iso(t),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool, "content": "ok"}]}}


def calls(t: float, name: str = "Bash") -> dict:
    return {"type": "assistant", "timestamp": iso(t), "message": {
        "role": "assistant", "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": name, "input": {}}]}}


def says(t: float, text: str = "Done.") -> dict:
    return {"type": "assistant", "timestamp": iso(t),
            "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}}


class Helpers(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sdir = self.root / "-home-someone-project" / UUID
        (self.sdir / "subagents").mkdir(parents=True)
        self.now = time.time()
        subagents.reset_cache()

    def agent(self, aid: str, description: str, lines: list[dict], finished: bool | None = None,
              kind: str = "general-purpose") -> None:
        meta = {"agentType": kind, "description": description}
        if finished is not None:
            meta["finished"] = finished
        (self.sdir / "subagents" / f"agent-{aid}.meta.json").write_text(json.dumps(meta))
        head = [{"type": "fork-context-ref", "agentId": aid}]  # a fork's first line has no time
        (self.sdir / "subagents" / f"agent-{aid}.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in head + lines))

    def states(self) -> dict:
        return {h["description"]: h["state"] for h in subagents.helpers(SESSION, self.root, self.now)}

    def test_running_done_and_handed_back(self):
        n = self.now
        self.agent("a1", "Measuring", [user(n - 300), calls(n - 10)])
        self.agent("a2", "Wrote the fix", [user(n - 900), says(n - 600)], finished=True)
        self.agent("a3", "Reviewed", [user(n - 900), calls(n - 20, "SubagentHandback"), result(n - 19)], finished=True)
        self.assertEqual(self.states(), {"Measuring": "running", "Wrote the fix": "done", "Reviewed": "done"})

    def test_a_finished_one_sent_another_message_runs_again(self):
        n = self.now
        self.agent("a1", "Reviewer", [says(n - 600), user(n - 5, "Round 2: please look again")], finished=True)
        self.assertEqual(self.states(), {"Reviewer": "running"})
        subagents.reset_cache()
        self.agent("a1", "Reviewer", [calls(n - 900, "SubagentHandback"), result(n - 899), user(n - 5, "Round 2")],
                   finished=True)
        self.assertEqual(self.states(), {"Reviewer": "running"})

    def test_quiet_unfinished_one_counts_as_stopped_and_old_ones_drop_off(self):
        n = self.now
        stale, subagents.STALE = subagents.STALE, 600
        self.addCleanup(setattr, subagents, "STALE", stale)
        self.agent("a1", "Crashed", [user(n - 1200), calls(n - 660)])
        self.agent("a2", "Long ago", [user(n - 9000), says(n - subagents.RECENT - 60)], finished=True)
        self.assertEqual(self.states(), {"Crashed": "stopped"})

    def test_started_comes_from_the_first_timed_line(self):
        n = self.now
        self.agent("a1", "Working", [user(n - 300), calls(n - 10)])
        (item,) = subagents.helpers(SESSION, self.root, n)
        self.assertAlmostEqual(item["started"], n - 300, delta=1)
        self.assertEqual((item["kind"], item["id"]), ("subagent", "a1"))

    def test_running_first_then_newest_and_capped(self):
        n = self.now
        for i in range(10):
            self.agent(f"d{i}", f"done {i}", [user(n - 1000), says(n - 100 - i)], finished=True)
        self.agent("r", "still going", [user(n - 50), calls(n - 1)])
        found = subagents.helpers(SESSION, self.root, n)
        self.assertEqual(len(found), subagents.MOST)
        self.assertEqual([h["description"] for h in found[:3]], ["still going", "done 0", "done 1"])

    def test_a_workflow_run_is_one_row(self):
        run = self.sdir / "subagents/workflows/wf_abc"
        run.mkdir(parents=True)
        (run / "journal.jsonl").write_text(
            json.dumps({"type": "started", "agentId": "x", "label": "one", "phase": "Look"}) + "\n")
        scripts = self.sdir / "workflows/scripts"
        scripts.mkdir(parents=True)
        (scripts / "probe-wf_abc.js").write_text("export const meta = { name: 'probe', phases: [{ title: 'Look' }] }\n")
        found = subagents.helpers(SESSION, self.root, self.now)
        self.assertEqual([(h["kind"], h["description"], h["state"]) for h in found], [("workflow", "probe", "running")])

    def test_other_agents_and_odd_records(self):
        self.assertEqual(subagents.helpers("cursor-abc", self.root, self.now), [])
        (self.sdir / "subagents" / "agent-bad.meta.json").write_text("not json")
        (self.sdir / "subagents" / "agent-bad.jsonl").write_text("{broken\n")
        self.assertEqual(subagents.helpers(SESSION, self.root, self.now), [])

    def test_a_long_transcript_is_read_by_its_ends(self):
        n = self.now
        middle = [says(n - 500, "x" * 2000) for _ in range(80)]  # well past HEAD + TAIL
        self.agent("a1", "Big", [user(n - 600)] + middle + [calls(n - 3)])
        (item,) = subagents.helpers(SESSION, self.root, n)
        self.assertEqual(item["state"], "running")
        self.assertAlmostEqual(item["started"], n - 600, delta=1)


if __name__ == "__main__":
    unittest.main()
