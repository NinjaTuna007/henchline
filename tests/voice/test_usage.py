"""The Cursor CLI's /usage panel, read as text; limits whose window has ended read as reset."""

import time
import unittest
from datetime import datetime

from tests import sandbox  # first: a throwaway HOME
from voice import usage

assert sandbox.HOME  # imported for its effect

PANEL = """
 Usage • Pro+                                                     Resets Nov 30
 Monthly plan and on-demand usage
 Category        Current             Usage
 Included        44% used            ██████░░░░
   Auto          40% used            █████░░░░░
   API           94% used            █████████░
 On-Demand       Disabled            ——————————
 On-demand usage is off
"""


class CursorPanel(unittest.TestCase):
    def test_rows_reset_and_plan(self):
        limits, notes = usage.parse_cursor_usage(PANEL, datetime(2026, 11, 17))
        self.assertEqual([(l["label"], l["percent"]) for l in limits],
                         [("Month: included", 44.0), ("Month: Auto model", 40.0), ("Month: other models", 94.0)])
        self.assertEqual(datetime.fromtimestamp(limits[0]["resets"]).date(), datetime(2026, 11, 30).date())
        self.assertEqual(notes, ["Plan: Pro+", "On-demand usage is off"])

    def test_reset_date_after_new_year(self):
        limits, _ = usage.parse_cursor_usage(PANEL.replace("Nov 30", "Jan 2"), datetime(2026, 12, 20))
        self.assertEqual(datetime.fromtimestamp(limits[0]["resets"]).year, 2027)

    def test_nothing_shown(self):
        self.assertEqual(usage.parse_cursor_usage(""), ([], []))


class Limits(unittest.TestCase):
    def test_ended_window_reads_as_reset(self):
        self.assertEqual(usage._limit("Week", 80, time.time() - 60), {"label": "Week", "percent": 0.0, "resets": 0})
        self.assertEqual(usage._limit("Week", 80, time.time() + 60)["percent"], 80.0)


class CodexName(unittest.TestCase):
    def test_newest_name_of_the_folders_thread(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            day = root / "2026/11/17"
            day.mkdir(parents=True)
            (day / "rollout-x.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {
                "id": "t-1", "cwd": "/home/me/proj"}}) + "\n")
            (Path(tmp) / "session_index.jsonl").write_text(
                json.dumps({"id": "t-1", "thread_name": "First"}) + "\n" +
                json.dumps({"id": "t-2", "thread_name": "Other"}) + "\n" +
                json.dumps({"id": "t-1", "thread_name": "Renamed"}) + "\n")
            self.assertEqual(usage.codex_name("codex-_home_me_proj", root), "Renamed")
            self.assertEqual(usage.codex_name("codex-_elsewhere", root), "")


class Readout(unittest.TestCase):
    def test_usage_and_context_are_different_sentences(self):
        found = {"limits": [{"label": "Week", "percent": 12}], "notes": ["Plan: Pro"],
                 "context": {"percent": 21, "size": 500000, "used": 100000,
                             "parts": [{"label": "Chat", "tokens": 80000}]}}
        self.assertIn("Week 12%", usage.words(found, "usage"))
        self.assertNotIn("Context", usage.words(found, "usage"))
        self.assertIn("21% full", usage.words(found, "context"))
        self.assertIn("Chat 80K", usage.words(found, "context"))
        self.assertEqual(usage.words({}, "usage"), "Usage hasn't been reported here.")
        self.assertEqual(usage.words({}, "context"), "Context hasn't been reported for this chat.")


if __name__ == "__main__":
    unittest.main()


CLAUDE_PANEL = """
   Settings  Status   Config   Usage   Stats
   Current session
   ████                                               8% used
   Resets 4:50am (Europe/Stockholm)
   Current week (all models)
   ██████▌                                            31% used
   Resets Nov 28, 5am (Europe/Stockholm)
   Current week (Fable)
   ██                                                 7% used
   Resets Nov 28, 5am (Europe/Stockholm)
   Current week (Sonnet)
                                                      0% used
   Resets Nov 29, 5:30pm (Europe/Stockholm)
   What's contributing to your usage?
"""


class ClaudePanel(unittest.TestCase):
    def test_every_row_in_panel_order_and_wording(self):
        found = usage.parse_claude_usage(CLAUDE_PANEL, datetime(2026, 11, 22, 9))
        self.assertEqual([(f["label"], f["percent"]) for f in found],
                         [("Current session", 8.0), ("Current week (all models)", 31.0),
                          ("Current week (Fable)", 7.0), ("Current week (Sonnet)", 0.0)])
        self.assertEqual(datetime.fromtimestamp(found[0]["resets"]), datetime(2026, 11, 23, 4, 50))  # next 4:50am
        self.assertEqual(datetime.fromtimestamp(found[2]["resets"]), datetime(2026, 11, 28, 5, 0))
        self.assertEqual(datetime.fromtimestamp(found[3]["resets"]), datetime(2026, 11, 29, 17, 30))

    def test_a_per_model_session_row_is_kept(self):
        panel = CLAUDE_PANEL.replace("   Current week (all models)",
                                     "   Current session (Fable)\n   █ 3% used\n   Resets 4:50am (Europe/Stockholm)\n"
                                     "   Current week (all models)")
        labels = [f["label"] for f in usage.parse_claude_usage(panel, datetime(2026, 11, 22, 9))]
        self.assertEqual(labels[:3], ["Current session", "Current session (Fable)", "Current week (all models)"])

    def test_nothing_shown(self):
        self.assertEqual(usage.parse_claude_usage(""), [])

    def test_card_rows_live_values_panel_order_and_fallback(self):
        status = [{"label": "Current session", "percent": 9.0, "resets": 1},
                  {"label": "Current week (all models)", "percent": 32.0, "resets": 2}]
        panel = usage.parse_claude_usage(CLAUDE_PANEL, datetime(2026, 11, 22, 9))
        rows = usage.claude_rows(status, panel)
        self.assertEqual([(r["label"], r["percent"]) for r in rows],
                         [("Current session", 9.0), ("Current week (all models)", 32.0),
                          ("Current week (Fable)", 7.0), ("Current week (Sonnet)", 0.0)])
        self.assertEqual(usage.claude_rows(status, []), status)  # no panel: the status line's two rows

    def test_status_line_rows_use_the_panels_wording(self):
        import json
        uuid = "12345678-1234-4123-8123-123456789abc"
        usage.CLAUDE_STATUS.mkdir(parents=True, exist_ok=True)
        (usage.CLAUDE_STATUS / f"{uuid}.json").write_text(json.dumps({"rate_limits": {
            "five_hour": {"used_percentage": 5, "resets_at": 4_000_000_000},
            "seven_day": {"used_percentage": 12, "resets_at": 4_000_000_000}}}))
        labels = [l["label"] for l in usage.claude(f"claude-{uuid}")["limits"]]
        self.assertEqual(labels, ["Current session", "Current week (all models)"])


def _rollout(root, day, name, lines):
    folder = root / "2026" / "11" / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{name}.jsonl"
    import json
    path.write_text("".join(json.dumps({"timestamp": at, "type": "event_msg",
                                        "payload": {"type": "token_count", "rate_limits": limits}}) + "\n"
                            for at, limits in lines))
    return path


NO_CREDITS = {"limit_id": "premium", "primary": None, "secondary": None,
              "credits": {"has_credits": False, "unlimited": False, "balance": None}, "plan_type": None}
WINDOWS = {"primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 4_000_000_000},
           "secondary": {"used_percent": 37.0, "window_minutes": 10080, "resets_at": 4_000_000_000},
           "credits": {"has_credits": True}, "plan_type": "team"}


class CodexAccount(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.root = Path(tempfile.mkdtemp())

    def test_out_of_credits_with_last_known_windows(self):
        import os
        old = _rollout(self.root, "20", "a", [("2026-11-20T10:00:00Z", WINDOWS)])
        os.utime(old, (1, 1))
        _rollout(self.root, "21", "b", [("2026-11-21T18:25:00Z", NO_CREDITS)])
        now = datetime.fromisoformat("2026-11-21T20:25:00+00:00").timestamp()
        card = usage.codex_account(self.root, now=now)
        self.assertEqual([(l["label"], l["percent"]) for l in card["limits"]],
                         [("Out of credits", 100.0), ("5-hour session", 12.0), ("Week", 37.0)])
        self.assertEqual(card["notes"][0], usage.OUT_OF_CREDITS)
        self.assertIn("Limits as last reported, 34 h ago", card["notes"])

    def test_normal_account(self):
        _rollout(self.root, "21", "b", [("2026-11-21T18:25:00Z", WINDOWS)])
        card = usage.codex_account(self.root)
        self.assertEqual([l["label"] for l in card["limits"]], ["5-hour session", "Week"])
        self.assertEqual(card["notes"], ["Plan: team"])

    def test_no_reports(self):
        self.assertEqual(usage.codex_account(self.root), {})


class ProbeLog(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.projects = Path(tempfile.mkdtemp())
        self.room = Path("/home/me/henchline")
        self.folder = self.projects / "-home-me-henchline"
        self.folder.mkdir()

    def _log(self, name, lines):
        import json
        stamp = datetime.now().astimezone().isoformat()  # the log begins now, as the probe's would
        lines = [dict(l, timestamp=stamp) if "timestamp" in l else l for l in lines]
        path = self.folder / f"{name}.jsonl"
        path.write_text("".join(json.dumps(l) + "\n" for l in lines))
        return path

    USAGE = [{"type": "user", "timestamp": "", "message": {"role": "user", "content":
              "<command-name>/usage</command-name>\n<command-message>usage</command-message>\n<command-args></command-args>"}},
             {"type": "system", "subtype": "local_command", "content": "<local-command-stdout>panel</local-command-stdout>"}]

    def test_the_probes_own_log_is_dropped(self):
        started = time.time() - 5
        log = self._log("probe", self.USAGE)
        self.assertTrue(usage.drop_probe_log(started, self.projects, self.room).startswith("dropped"))
        self.assertFalse(log.exists())

    def test_a_real_chat_is_kept(self):
        started = time.time() - 5
        log = self._log("chat", self.USAGE + [{"type": "user", "message": {"role": "user", "content": "fix the tests"}}])
        self.assertIn("isn't /usage", usage.drop_probe_log(started, self.projects, self.room))
        self.assertTrue(log.exists())

    def test_model_output_or_an_older_log_is_kept(self):
        started = time.time() - 5
        log = self._log("answered", self.USAGE + [{"type": "assistant", "message": {"role": "assistant", "content": []}}])
        self.assertIn("model output", usage.drop_probe_log(started, self.projects, self.room))
        self.assertTrue(log.exists())
        self.assertEqual(usage.drop_probe_log(time.time() + 60, self.projects, self.room), "no probe log")
        self.assertTrue(log.exists())

    def test_no_folder_or_log(self):
        import shutil
        shutil.rmtree(self.folder)
        self.assertEqual(usage.drop_probe_log(time.time(), self.projects, self.room), "no probe log")


class ResetSince(unittest.TestCase):
    def test_an_old_window_says_it_has_reset(self):
        now = 2_000_000_000
        items = usage._windows({"primary": {"used_percent": 40, "window_minutes": 300, "resets_at": now - 10},
                                "secondary": {"used_percent": 37, "window_minutes": 10080, "resets_at": now + 10}}, now)
        self.assertEqual([(i["label"], i["percent"]) for i in items],
                         [("5-hour session (reset since)", 0.0), ("Week", 37.0)])
