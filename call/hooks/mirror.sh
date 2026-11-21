#!/bin/sh
# Claude Code's hook for call/hooks/mirror.py: off a call (nearly always) it costs a file test and nothing else, and
# whatever happens it never fails a prompt or prints anything to Claude Code.
[ -n "$HENCHLINE_USAGE_PROBE" ] && exit 0  # Henchline's hidden Claude reading /usage: not a chat
[ -e "$HOME/.local/state/ptt/call" ] || exit 0
"$(dirname "$(readlink -f "$0")")/mirror.py" >/dev/null 2>&1
exit 0
