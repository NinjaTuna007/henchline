"""A chat's plan document, for the phone.

Cursor keeps one markdown file per plan in ~/.cursor/plans, with a to-do list in the header (pending, in progress,
done) and a mermaid diagram in the body. The phone shows the steps and how many are done. Claude records its plan
file too. Codex currently exposes no session-owned plan file, so the phone shows none instead of guessing from
another chat's files.
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

STATUSES = {"pending", "in_progress", "completed"}


def _split(text: str) -> tuple[str, str]:
    """The header and the body. A comment before the header (some plan files start with one) is not part of either."""
    rest = re.sub(r"^(?:<!--.*?-->\s*)+", "", text.lstrip(), count=1, flags=re.S)
    if rest.startswith("---"):
        end = rest.find("\n---", 3)
        if end != -1:
            return rest[4:end], rest[end + 4:]
    return "", text


def parse(text: str) -> dict:
    """The plan's name, overview, and steps. The mermaid stays in the body; the phone lists the steps."""
    front, body = _split(text)
    name = _field(front, "name")
    overview = _field(front, "overview")
    todos, current = [], None
    for line in front.splitlines():
        item = re.match(r"\s*-\s+id:\s*(.+)", line)
        if item:
            current = {"id": item.group(1).strip().strip("\"'"), "content": "", "status": "pending"}
            todos.append(current)
            continue
        if current is None:
            continue
        content = re.match(r"\s+content:\s*(.*)", line)
        status = re.match(r"\s+status:\s*(\w+)", line)
        if content:
            current["content"] = content.group(1).strip().strip("\"'")
        elif status and status.group(1) in STATUSES:
            current["status"] = status.group(1)
    if not todos:  # a plan written as Markdown (Claude's): its checkboxes are its steps
        for box in re.finditer(r"^\s*[-*]\s+\[([ xX~])\]\s+(.+)$", body, re.M):
            todos.append({"id": str(len(todos) + 1), "content": box.group(2).strip(),
                          "status": "completed" if box.group(1) in "xX" else "in_progress" if box.group(1) == "~" else "pending"})
    if not name:
        heading = re.search(r"^#\s+(.+)$", body, re.M)
        name = heading.group(1).strip() if heading else "Plan"
    done = sum(1 for step in todos if step["status"] == "completed")
    return {"name": name, "overview": overview, "todos": todos, "done": done, "total": len(todos),
            "body": body.strip()[:6000], "html": html(body.strip()[:20000]), "blocks": blocks(body.strip()[:40000], name)}


def html(markdown: str) -> str:
    """The plan's Markdown as the little HTML a phone's text view shows (headings, lists, bold, code); a diagram
    block becomes a note, as the phone doesn't draw it."""
    import html as escape

    out, code, fence = [], [], None
    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            if fence is None:
                fence = line.strip()[3:].strip().lower()
                code = []
            else:
                if fence == "mermaid":
                    out.append("<p><i>(a diagram: see it on the computer)</i></p>")
                else:
                    out.append("<p><tt>" + "<br>".join(escape.escape(c) for c in code) + "</tt></p>")
                fence = None
            continue
        if fence is not None:
            code.append(line)
            continue
        text = escape.escape(line.rstrip())
        spans: list[str] = []  # code first, kept out of the bold and italics (`a*b*c` stays as written)
        text = re.sub(r"`([^`]+)`", lambda m: spans.append(m.group(1)) or f"\x00{len(spans) - 1}\x00", text)
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        text = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<i>\1</i>", text)
        text = re.sub("\x00(\\d+)\x00", lambda m: f"<tt>{spans[int(m.group(1))]}</tt>", text)
        heading = re.match(r"(#{1,6})\s+(.*)", text)
        bullet = re.match(r"\s*(?:[-*]|\d+[.)])\s+(?:\[([ xX~])\]\s+)?(.*)", text)
        if heading:
            size = {1: "big", 2: "big"}.get(len(heading.group(1)), "")
            inner = f"<b>{heading.group(2)}</b>"
            out.append(f"<p><{size}>{inner}</{size}></p>" if size else f"<p>{inner}</p>")
        elif bullet:
            mark = {"x": "✓ ", "X": "✓ ", "~": "● ", " ": "○ "}.get(bullet.group(1) or "", "• ")
            out.append(f"&nbsp;&nbsp;{mark}{bullet.group(2)}<br>")
        elif text.strip():
            out.append(text + "<br>")
        else:
            out.append("<br>")
    return "".join(out)


def _diagram(code: str) -> dict:
    """A mermaid diagram as boxes in the order they appear, so the phone can draw them. Labels win over bare ids."""
    labels: dict[str, str] = {}
    for found in re.finditer(r"(\w+)\s*[\[({]\"?([^\]}\)\"]+?)\"?\s*[\])}]", code):
        if found.group(1) not in ("flowchart", "graph", "subgraph"):
            labels[found.group(1)] = found.group(2).strip()
    order: list[str] = []

    def add(name: str) -> None:
        name = name.strip()
        if name and name not in order and name not in ("flowchart", "graph", "subgraph", "end"):
            order.append(name)

    node = r"(\w+)(?:\[[^\]]*\]|\([^)]*\)|\{[^}]*\})?"
    arrow = r"(?:<-->|-{1,2}\.?->{1,2})"
    for found in re.finditer(rf"{node}\s*{arrow}\s*(?:\|[^|\n]*\|\s*)?{node}", code):
        add(labels.get(found.group(1), found.group(1)))
        add(labels.get(found.group(2), found.group(2)))
    return {"t": "diagram", "kind": "mermaid", "nodes": order[:16]}


def _inline(text: str) -> str:
    """One line's Markdown as the small HTML a phone's text view shows: code first (kept literal), then bold,
    italics and links. A link is its name. Everything escaped before any tag goes in."""
    import html as escape

    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = escape.escape(text)
    spans: list[str] = []
    text = re.sub(r"`([^`]+)`", lambda m: spans.append(m.group(1)) or f"\x00{len(spans) - 1}\x00", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return re.sub("\x00(\\d+)\x00", lambda m: f"<tt>{spans[int(m.group(1))]}</tt>", text)


def blocks(markdown: str, title: str = "") -> list[dict]:
    """The plan's Markdown as blocks the phone lays out one view each (the design review, 2026-11-17): headings,
    paragraphs, lists (depth, checkbox), code, diagrams (their node labels), tables, rules. The first heading
    is left out when it repeats the plan's name."""
    out: list[dict] = []
    lines = markdown.splitlines()
    i = 0
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append({"t": "p", "html": _inline(" ".join(x.strip() for x in para))})
            para.clear()

    while i < len(lines):
        line = lines[i]
        fence = re.match(r"\s*```\s*([\w+-]*)", line)
        if fence:
            flush()
            lang, code = fence.group(1).lower(), []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            if lang == "mermaid":
                out.append(_diagram("\n".join(code)))
            else:
                out.append({"t": "code", "lang": lang, "text": "\n".join(code)[:6000]})
            continue
        heading = re.match(r"(#{1,6})\s+(.*)", line)
        if heading:
            flush()
            words = heading.group(2).strip()
            if not (not out and title and words.lower() == title.lower()):
                out.append({"t": "h", "level": len(heading.group(1)), "html": _inline(words)})
            i += 1
            continue
        if re.match(r"\s*\|.*\|\s*$", line):
            flush()
            rows = []
            while i < len(lines) and re.match(r"\s*\|.*\|\s*$", lines[i]):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append([_inline(c) for c in cells])
                i += 1
            if rows:
                out.append({"t": "table", "head": rows[0], "rows": rows[1:60]})
            continue
        item = re.match(r"(\s*)(?:[-*+]|\d+[.)])\s+(?:\[([ xX~])\]\s+)?(.*)", line)
        if item:
            flush()
            items, ordered = [], bool(re.match(r"\s*\d", line))
            while i < len(lines) and (item := re.match(r"(\s*)(?:[-*+]|\d+[.)])\s+(?:\[([ xX~])\]\s+)?(.*)", lines[i])):
                check = item.group(2)
                items.append({"html": _inline(item.group(3).strip()), "depth": min(len(item.group(1).expandtabs(4)) // 2, 4),
                              "check": None if check is None else ("x" if check in "xX" else check)})
                i += 1
            out.append({"t": "list", "ordered": ordered, "items": items})
            continue
        if re.fullmatch(r"\s*(-{3,}|\*{3,}|_{3,})\s*", line):
            flush()
            out.append({"t": "rule"})
            i += 1
            continue
        if line.strip():
            para.append(line)
        else:
            flush()
        i += 1
    flush()
    return out[:400]


_PLAN_URI = re.compile(rb'"planUri"\s*:\s*"(file://[^"\\]+\.plan\.md)"')
_plan_cache: dict[str, tuple[str, list[str]]] = {}  # store path -> (latest blob, plan paths, last touched first)


def _plans_from_store(store: Path) -> list[Path]:
    """The plans this Cursor chat created, from its own store (`planUri` on the plan tool's result), the one it
    touched last first: a plan is touched where its result blob, or a blob naming that result, comes in the chat."""
    import contextlib
    import json
    import sqlite3

    try:
        with contextlib.closing(sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=1)) as db:
            row = db.execute("select value from meta where key = '0'").fetchone()
            if not row:
                return []
            raw = row[0]
            if isinstance(raw, str) and re.fullmatch(r"[0-9a-f]+", raw):
                raw = bytes.fromhex(raw).decode()
            elif isinstance(raw, bytes):
                raw = raw.decode()
            root_id = str(json.loads(raw).get("latestRootBlobId") or "")
            cached = _plan_cache.get(str(store))
            if cached and cached[0] == root_id:
                return [Path(p) for p in cached[1] if Path(p).is_file()]
            blobs = {bid: data for bid, data in db.execute("select id, data from blobs")
                     if isinstance(data, (bytes, bytearray))}
            root = blobs.get(root_id)
            if not root:
                return []
            seen: set[str] = set()
            children: list[str] = []
            for i in range(len(root) - 32):
                child = root[i:i + 32].hex()
                if child in blobs and child not in seen:
                    seen.add(child)
                    children.append(child)
            owners: dict[str, str] = {}
            for bid, blob in blobs.items():
                if b'"planUri"' not in blob:
                    continue
                found = _PLAN_URI.search(blob)
                if found:
                    owners[bid] = found.group(1).decode()
            needles = [(bytes.fromhex(bid), uri) for bid, uri in owners.items()]
            touched: dict[str, int] = {}
            for at, child in enumerate(children):
                if child in owners:
                    touched[owners[child]] = at
                blob = blobs.get(child, b"")
                for needle, uri in needles:
                    if needle in blob:
                        touched[uri] = at
    except (OSError, sqlite3.Error, ValueError):
        return []
    found = [str(Path(urllib.parse.unquote(uri.removeprefix("file://"))))
             for uri in sorted(touched, key=lambda uri: -touched[uri])]
    _plan_cache[str(store)] = (root_id, found)
    return [Path(p) for p in found if Path(p).is_file()]


def _cursor_store(home: Path, key: str) -> Path | None:
    found = list((home / ".cursor/chats").glob(f"*/{key}/store.db"))
    return found[0] if len(found) == 1 else None


def plans_for_session(session: str, home: Path | None = None) -> list[Path]:
    """Every plan file that chat itself wrote or opened, from the agent's own records (never guessed by name), the
    one it touched last first: Claude logs planFilePath in its session log; Cursor links a chat to its plans in its
    store, in the plan's opening comment, or (an editor chat) in Cursor's own database. (A plan path a transcript
    merely mentions is not the chat's own: the CLI read another project's plan once.) File times don't order
    them: two plans saved in the same minute are common."""
    home = home or Path.home()
    key = session.split("-", 1)[1] if "-" in session else ""
    if session.startswith("claude-") and re.fullmatch(r"[0-9a-f-]{36}", key):
        return _claude_plans(home, key)
    if not session.startswith("cursor-") or not re.fullmatch(r"[0-9a-f-]{36}", key):
        return []
    # This chat's store records the plans it created (planUri): the plans it is on, not the newest files in the
    # folder and not whichever file happens to mention the project.
    found: list[Path] = []
    store = _cursor_store(home, key)
    for source in (lambda: _plans_from_store(store) if store is not None else [],
                   lambda: _marked(home, key), lambda: _editor_plans(home, key)):
        for path in source():
            if path not in found:
                found.append(path)
    return found


def for_session(session: str, home: Path | None = None) -> Path | None:
    """The plan that chat touched last (see plans_for_session)."""
    found = plans_for_session(session, home)
    return found[0] if found else None


def _claude_plans(home: Path, key: str) -> list[Path]:
    """Claude's planFilePath entries in its session log, the last one logged first."""
    import json

    for log in (home / ".claude/projects").glob(f"*/{key}.jsonl"):
        try:
            with log.open("rb") as f:
                f.seek(max(0, log.stat().st_size - 8 * 1024 * 1024))
                found = re.findall(rb'"planFilePath":"([^"]+)"', f.read())
        except OSError:
            continue
        if not found:
            continue
        out: list[Path] = []
        for raw in reversed(found):
            try:
                path = Path(json.loads(b'"' + raw + b'"'))  # a JSON string: its escapes undone
            except ValueError:
                continue
            if path not in out and path.is_file():
                out.append(path)
        return out
    return []


_editor_cache: dict[str, tuple[tuple, list[str]]] = {}  # chat -> (what it was read from, plan paths)


def _editor_plans(home: Path, key: str) -> list[Path]:
    """A Cursor editor chat's plans (its ofsContent rows), the one its conversation touched last first. The
    conversation's order is composerData's header list; a bubble whose tool call names a plan file touches it
    (reading, editing, creating it), and any other mention counts only between plans no tool call touched. Plans it
    never names follow, the newest row first. Read only; anything unreadable leaves the rows' own order."""
    import contextlib
    import json
    import sqlite3

    db_path = home / ".config/Cursor/User/globalStorage/state.vscdb"
    if not db_path.is_file():
        return []
    paths: list[Path] = []
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)) as db:
            # glob: case-sensitive, and a literal ".plan.md" (like matched SLIDE_PLAN.md too)
            rows = db.execute("select key from cursorDiskKV where key glob ?",
                              (f"ofsContent:{key}:*.plan.md",)).fetchall()
            for (row,) in reversed(rows):
                path = Path(urllib.parse.unquote(row.split(":file://", 1)[-1]))
                if path.name.endswith(".plan.md") and path not in paths and path.is_file():
                    paths.append(path)
            if len(paths) < 2:
                return paths
            row = db.execute("select value from cursorDiskKV where key = ?", (f"composerData:{key}",)).fetchone()
            headers = (json.loads(row[0]).get("fullConversationHeadersOnly") or []) if row and row[0] else []
            order = {str(h.get("bubbleId")): at for at, h in enumerate(headers) if isinstance(h, dict)}
            stamp = (str(db_path), tuple(map(str, paths)), len(headers), headers[-1].get("bubbleId") if headers else "")
            cached = _editor_cache.get(key)
            if cached and cached[0] == stamp:
                return [Path(p) for p in cached[1]]
            # A plan is known by its file name (a tool may name it relative to the workspace); two plans with the
            # same name are known only by their full paths, so neither takes the other's touches.
            shared = {p.name for p in paths if sum(q.name == p.name for q in paths) > 1}
            names = {path: {str(path), urllib.parse.quote(str(path))} if path.name in shared
                     else {path.name, urllib.parse.quote(path.name)} for path in paths}
            needles = sorted({n for words in names.values() for n in words})
            where = " or ".join("instr(value, ?) > 0" for _ in needles)
            prefix = f"bubbleId:{key}:"
            strong: dict[Path, int] = {}
            weak: dict[Path, int] = {}
            for bubble_key, value in db.execute(
                    f"select key, value from cursorDiskKV where key >= ? and key < ? and ({where})",
                    (prefix, prefix[:-1] + ";", *needles)):
                at = order.get(bubble_key[len(prefix):])
                if at is None:
                    continue
                text = value.decode(errors="replace") if isinstance(value, (bytes, bytearray)) else str(value or "")
                try:
                    tool = json.loads(text).get("toolFormerData") or {}
                    called = " ".join(str(tool.get(k) or "") for k in ("params", "rawArgs"))
                except (ValueError, AttributeError):  # not JSON, or a tool record that isn't an object
                    called = ""
                for path, words in names.items():
                    if any(w in called for w in words):
                        strong[path] = max(strong.get(path, -1), at)
                    if any(w in text for w in words):
                        weak[path] = max(weak.get(path, -1), at)
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        return paths
    ranked = sorted(paths, key=lambda p: (strong.get(p, -1), weak.get(p, -1)), reverse=True)  # stable: rows' order
    _editor_cache[key] = (stamp, [str(p) for p in ranked])
    return ranked


def _marked(home: Path, key: str) -> list[Path]:
    """The plan files that name this chat in their opening comment, the newest first."""
    folder = home / ".cursor/plans"
    if not folder.is_dir():
        return []
    mark = f"<!-- {key}"
    found = []
    for path in folder.glob("*.plan.md"):
        try:
            head = path.read_text(errors="replace")[:80]
            if head.lstrip().startswith(mark):
                found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return [path for _, path in sorted(found, key=lambda item: -item[0])]


def summary(path: Path) -> dict | None:
    """A plan's name and how many of its steps are done, for a list of plans."""
    try:
        plan = parse(path.read_text(errors="replace"))
    except OSError:
        return None
    return {"file": str(path), "name": plan["name"], "done": plan["done"], "total": plan["total"]}


def of_session(session: str, project: Path | None, file: str | None = None) -> dict | None:
    """That exact chat's own plan, parsed: the one it touched last, or `file` when that is one of its own plans
    (never any other path). With "plans": every plan of that chat, the last touched first. Never infer ownership
    from its agent or project."""
    owned = plans_for_session(session)
    if file:
        owned_path = next((p for p in owned if str(p) == file), None)
        if owned_path is None:
            return None
        path = owned_path
    elif owned:
        path = owned[0]
    else:
        return None
    try:
        plan = parse(path.read_text(errors="replace"))
    except OSError:
        return None
    plan["file"] = str(path)
    plan["session"] = session
    plan["plans"] = [s for s in (summary(p) for p in owned[:20]) if s]
    return plan


def _field(front: str, key: str) -> str:
    found = re.search(rf"^{key}:\s*(.*)$", front, re.M)
    return found.group(1).strip().strip("\"'") if found else ""


def _places(project: Path | None) -> list[Path]:
    home = Path.home()
    found = [home / ".cursor/plans"]
    for folder in (home / ".claude/plans", home / ".codex/plans"):
        found.append(folder)
    if project:
        found.append(project)
    return found


def find(project: Path | None) -> dict | None:
    """The newest plan that belongs to that project. A plan belongs when it names the project, or it lives in the
    project. With no project, the newest plan anywhere these are kept."""
    files = []
    for folder in _places(project):
        if not folder.is_dir():
            continue
        files += [path for path in folder.glob("*.plan.md") if path.is_file()]
    if not files:
        return None
    mark = ""
    if project:
        mark = project.name.lower()
        named = []
        for path in files:
            try:
                text = path.read_text(errors="replace")[:40000]
            except OSError:
                continue
            if mark and (mark in text.lower() or str(project) in text) or path.parent == project:
                named.append((path, text))
        if not named:
            return None
        path, text = max(named, key=lambda item: item[0].stat().st_mtime)
    else:
        path = max(files, key=lambda item: item.stat().st_mtime)
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return None
    plan = parse(text)
    plan["file"] = str(path)
    return plan


def approval(plan: dict) -> str:
    """What the agent is told when the user taps build. It names the file, so the agent updates that document."""
    return (f"The plan \"{plan['name']}\" is approved. Build it, and keep the plan document updated as you go "
            f"({plan['file']}): mark each step pending, in progress, or done.")
