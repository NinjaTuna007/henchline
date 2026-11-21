#!/usr/bin/env python3
"""Claude Code's status line command, for Henchline: Claude hands it each session's status as JSON (its model's
exact name, which says "(1M context)" when it has it); this keeps it under
~/.local/state/ptt/claude-status/<session>.json for the phone's model pill (the user's wish, 2026-11-16: no
guessing the context). It prints nothing, so Claude's screen shows no status line of it.

Turned on in ~/.claude/settings.json:  "statusLine": {"type": "command", "command": "python3 <this file>"}
"""

import json
import os
import re
import sys
from pathlib import Path

DIR = Path.home() / ".local/state/ptt/claude-status"

if os.environ.get("HENCHLINE_USAGE_PROBE"):  # Henchline's hidden Claude reading its /usage: not a chat
    sys.exit(0)
try:
    status = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
session = str(status.get("session_id", "")) if isinstance(status, dict) else ""
if re.fullmatch(r"[0-9a-f-]{36}", session):
    DIR.mkdir(parents=True, exist_ok=True)
    aside = DIR / f".{session}.{os.getpid()}"
    aside.write_text(json.dumps(status))
    os.chmod(aside, 0o600)
    aside.replace(DIR / f"{session}.json")
