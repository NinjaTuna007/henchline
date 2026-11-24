"""voice/plans.py: a plan file's steps, and which file belongs to a project."""

import tempfile
import unittest
from pathlib import Path

from tests import sandbox  # noqa: F401
from voice import plans

SAMPLE = """---
name: Phone plan
overview: Show the steps on the phone.
todos:
  - id: one
    content: "Read the file"
    status: completed
  - id: two
    content: "Draw the steps"
    status: in_progress
  - id: three
    content: A button
    status: pending
isProject: false
---

# Phone plan

```mermaid
flowchart LR
  a --> b
```
"""


class Parsed(unittest.TestCase):
    def test_the_steps_and_how_many_are_done(self):
        got = plans.parse(SAMPLE)
        self.assertEqual(got["name"], "Phone plan")
        self.assertEqual(got["done"], 1)
        self.assertEqual(got["total"], 3)
        self.assertEqual(got["todos"][1]["status"], "in_progress")
        self.assertIn("mermaid", got["body"])

    def test_a_cursor_file_takes_its_title_from_the_heading(self):
        text = """---
todos:
  - id: one
    content: "Read the file"
    status: pending
isProject: false
---
# Email into the list
"""
        got = plans.parse(text)
        self.assertEqual(got["name"], "Email into the list")
        self.assertEqual(got["total"], 1)

    def test_a_comment_before_the_header_is_not_the_document(self):
        text = """<!-- 2ed6eebc-f2ce-43e5-8730-fc126b9f6794 -->
---
todos:
  - id: voices
    content: "A voices sheet"
    status: pending
isProject: false
---
# Voices

See [plan_mode](call/server.py) and the rest.

```mermaid
flowchart LR
  phone[Phone] --> laptop[Laptop]
```
"""
        got = plans.parse(text)
        self.assertEqual(got["name"], "Voices")
        self.assertEqual(got["total"], 1)
        self.assertEqual(got["todos"][0]["content"], "A voices sheet")
        self.assertNotIn("<!--", got["body"])
        self.assertNotIn("id:", got["body"])
        paragraph = next(b for b in got["blocks"] if b["t"] == "p")
        self.assertNotIn("](", paragraph["html"])
        self.assertIn("plan_mode", paragraph["html"])
        diagram = next(b for b in got["blocks"] if b["t"] == "diagram")
        self.assertEqual(diagram["nodes"], ["Phone", "Laptop"])

    def test_the_newest_file_for_this_chat_is_the_plan(self):
        import os
        root = Path(tempfile.mkdtemp())
        folder = root / ".cursor/plans"
        folder.mkdir(parents=True)
        chat = "2ed6eebc-f2ce-43e5-8730-fc126b9f6794"
        old = folder / "old.plan.md"
        new = folder / "new.plan.md"
        old.write_text(f"<!-- {chat} -->\n---\nname: Old\ntodos:\n  - id: a\n    content: waiting\n    status: pending\n---\n")
        new.write_text(f"<!-- {chat} -->\n---\nname: New\ntodos:\n  - id: a\n    content: done\n    status: completed\n---\n")
        os.utime(old, (1, 1_000))
        os.utime(new, (1, 2_000))
        got = plans.for_session(f"cursor-{chat}", root)
        self.assertEqual(got, new)

    def test_a_plan_that_names_the_project_is_the_one(self):
        root = Path(tempfile.mkdtemp())
        folder = root / "plans"
        folder.mkdir()
        (folder / "other.plan.md").write_text("---\nname: Other\ntodos:\n  - id: a\n    content: no\n    status: pending\n---\n")
        (folder / "mine.plan.md").write_text(SAMPLE.replace("Phone plan", "henchline work"))
        project = root / "henchline"
        project.mkdir()
        import voice.plans as mod
        original = mod._places
        mod._places = lambda _project: [folder]
        try:
            got = plans.find(project)
        finally:
            mod._places = original
        self.assertEqual(got["name"], "henchline work")
        self.assertTrue(got["file"].endswith("mine.plan.md"))

    def test_codex_does_not_infer_a_plan_from_agent_or_project(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cursor = root / ".cursor/plans/cursor.plan.md"
            codex = root / ".codex/plans/codex.plan.md"
            cursor.parent.mkdir(parents=True)
            codex.parent.mkdir(parents=True)
            project = root / "henchline"
            project.mkdir()
            local = project / "local.plan.md"
            cursor.write_text(SAMPLE.replace("Phone plan", "henchline cursor work"))
            codex.write_text(SAMPLE.replace("Phone plan", "henchline codex work"))
            local.write_text(SAMPLE.replace("Phone plan", "henchline local work"))

            with mock.patch.object(Path, "home", return_value=root):
                first = plans.of_session("codex-00000000-0000-0000-0000-000000000001", project)
                second = plans.of_session("codex-00000000-0000-0000-0000-000000000002", project)
                unknown = plans.of_session("agent-some-chat", project)

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertIsNone(unknown)

    def test_codex_with_no_plan_does_not_show_the_cursor_plan(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cursor = root / ".cursor/plans/cursor.plan.md"
            cursor.parent.mkdir(parents=True)
            project = root / "henchline"
            project.mkdir()
            cursor.write_text(SAMPLE.replace("Phone plan", "henchline cursor work"))

            with mock.patch.object(Path, "home", return_value=root):
                got = plans.of_session("codex-00000000-0000-0000-0000-000000000001", project)

        self.assertIsNone(got)


def _plan(folder: Path, name: str, title: str) -> Path:
    path = folder / name
    path.write_text(f"---\nname: {title}\ntodos:\n  - id: a\n    content: one\n    status: completed\n"
                    f"  - id: b\n    content: two\n    status: pending\n---\n")
    return path


def _editor_db(root: Path, chat: str, rows: list[Path], bubbles: list[tuple[str, dict]]) -> None:
    """A fake Cursor editor database: the chat's plan rows, its conversation order, and its bubbles."""
    import json
    import sqlite3
    import urllib.parse

    path = root / ".config/Cursor/User/globalStorage/state.vscdb"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("create table if not exists cursorDiskKV (key text unique on conflict replace, value blob)")
    for plan in rows:
        db.execute("insert into cursorDiskKV values (?, ?)",
                   (f"ofsContent:{chat}:file://{urllib.parse.quote(str(plan))}", "x"))
    headers = [{"bubbleId": bid, "type": 2} for bid, _ in bubbles]
    db.execute("insert into cursorDiskKV values (?, ?)",
               (f"composerData:{chat}", json.dumps({"fullConversationHeadersOnly": headers})))
    for bid, value in bubbles:
        db.execute("insert into cursorDiskKV values (?, ?)", (f"bubbleId:{chat}:{bid}", json.dumps(value)))
    db.commit()
    db.close()


class SessionPlans(unittest.TestCase):
    CHAT = "1e6e96a3-0000-4000-8000-000000000001"

    def setUp(self):
        plans._editor_cache.clear()
        self.root = Path(tempfile.mkdtemp())
        self.folder = self.root / ".cursor/plans"
        self.folder.mkdir(parents=True)

    def test_an_editor_chat_orders_its_plans_by_its_own_conversation(self):
        import os
        old = _plan(self.folder, "old_one.plan.md", "Old")
        new = _plan(self.folder, "new one.plan.md", "New")
        os.utime(new, (1, 1_000))  # file times say nothing: the old plan was saved last
        os.utime(old, (1, 2_000))
        import json
        def tool(path):
            return {"toolFormerData": {"params": json.dumps({"relativeWorkspacePath": str(path)})}}
        _editor_db(self.root, self.CHAT, [new, old], [  # the rows' own order says old
            ("b1", tool(old)), ("b2", tool(new)), ("b3", {"text": "edited"}),
            # a later message with the old plan only as attached context, not a tool call on it
            ("b4", {"context": {"fileSelections": {f"file://{old}": []}}}),
        ])
        got = plans.plans_for_session(f"cursor-{self.CHAT}", self.root)
        self.assertEqual(got, [new, old])
        self.assertEqual(plans.for_session(f"cursor-{self.CHAT}", self.root), new)

    def test_a_later_tool_call_moves_a_plan_first(self):
        import json
        a = _plan(self.folder, "a.plan.md", "A")
        b = _plan(self.folder, "b.plan.md", "B")
        def tool(path):
            return {"toolFormerData": {"params": json.dumps({"targetFile": str(path)})}}
        _editor_db(self.root, self.CHAT, [a, b], [("x1", tool(b)), ("x2", tool(a)), ("x3", tool(b)), ("x4", tool(a))])
        self.assertEqual(plans.plans_for_session(f"cursor-{self.CHAT}", self.root), [a, b])

    def test_plans_never_named_follow_the_rows_and_missing_files_drop(self):
        a = _plan(self.folder, "a.plan.md", "A")
        b = _plan(self.folder, "b.plan.md", "B")
        gone = self.folder / "gone.plan.md"
        _editor_db(self.root, self.CHAT, [a, gone, b], [("x1", {"text": "nothing"})])
        self.assertEqual(plans.plans_for_session(f"cursor-{self.CHAT}", self.root), [b, a])

    def test_only_plan_files_count_and_same_names_do_not_tie(self):
        import json
        other = self.root / "elsewhere"
        other.mkdir()
        mine = _plan(self.folder, "same.plan.md", "Mine")
        twin = _plan(other, "same.plan.md", "Twin")
        slides = _plan(self.folder, "SLIDE_PLAN.md", "Slides")
        upper = _plan(self.folder, "x.PLAN.MD", "Upper")

        def tool(path):
            return {"toolFormerData": {"params": json.dumps({"targetFile": str(path)})}}
        _editor_db(self.root, self.CHAT, [twin, mine, slides, upper], [("x1", tool(twin)), ("x2", tool(mine))])
        self.assertEqual(plans.plans_for_session(f"cursor-{self.CHAT}", self.root), [mine, twin])
        plans._editor_cache.clear()
        _editor_db(self.root, self.CHAT, [], [("x1", tool(twin)), ("x2", tool(mine)), ("x3", tool(twin))])  # twin last
        self.assertEqual(plans.plans_for_session(f"cursor-{self.CHAT}", self.root), [twin, mine])

    def test_an_unreadable_database_is_no_plan(self):
        db = self.root / ".config/Cursor/User/globalStorage/state.vscdb"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"not a database")
        self.assertEqual(plans.plans_for_session(f"cursor-{self.CHAT}", self.root), [])

    def test_claude_lists_each_logged_plan_the_last_logged_first(self):
        chat = "00000000-0000-4000-8000-0000000000aa"
        first = _plan(self.root, "first.md", "First")
        second = _plan(self.root, "second.md", "Second")
        log = self.root / f".claude/projects/p/{chat}.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("\n".join(f'{{"planFilePath":"{p}"}}' for p in (first, second, first, self.root / "nope.md")))
        self.assertEqual(plans.plans_for_session(f"claude-{chat}", self.root), [first, second])

    def test_of_session_lists_the_plans_and_shows_only_its_own_on_request(self):
        from unittest import mock

        a = _plan(self.folder, "a.plan.md", "A")
        b = _plan(self.folder, "b.plan.md", "B")
        stranger = _plan(self.folder, "stranger.plan.md", "Stranger")
        session = f"cursor-{self.CHAT}"
        with mock.patch.object(plans, "plans_for_session", return_value=[a, b]):
            latest = plans.of_session(session, None)
            picked = plans.of_session(session, None, str(b))
            refused = plans.of_session(session, None, str(stranger))
            relative = plans.of_session(session, None, "../" + b.name)
        self.assertEqual(latest["name"], "A")
        self.assertEqual(latest["plans"], [{"file": str(a), "name": "A", "done": 1, "total": 2},
                                           {"file": str(b), "name": "B", "done": 1, "total": 2}])
        self.assertEqual(latest["session"], session)
        self.assertEqual(picked["name"], "B")
        self.assertEqual(picked["file"], str(b))
        self.assertIsNone(refused)
        self.assertIsNone(relative)


if __name__ == "__main__":
    unittest.main()
