# Henchline bridge for Cursor

Without it, Henchline can only type into the Cursor chat that happens to be open. With it, each Cursor IDE chat is
its own chat on the phone: named by Cursor's own title, and what you text, record or switch reaches that very chat.

## Install

```bash
./install.sh agents      # with Cursor installed: packs the extension and runs `cursor --install-extension`
```

No Node tools are needed (`pack.py` makes the `.vsix`). A Cursor window picks up a first install without a reload;
after an update, a window keeps the old version until it is reloaded (restarting the extension host alone keeps
it). Henchline reloads it itself when it needs the new one, through a second, tiny extension installed with it
(`reload/`, the reload helper: a first install, so it runs in open windows at once; it also lists its window's
terminals, so the window holding one is known even under the older bridge). It reloads in the background, and only
while nothing in that window is working: its terminals survive a reload (Cursor's persistent sessions, read from
every settings file that can set them), Cursor's debugging port shows no chat generating, no chat was written to
in the last minute, and no agent in that Cursor's terminals is in the middle of a turn (by its own log). Unsure
counts as working; then the message is refused with the reason, and the next one checks again. You can always run
**Developer: Reload Window** (Ctrl+Shift+P) yourself.

## How it works

- A Cursor chat's commands run with `CURSOR_CONVERSATION_ID` (its chat id) and `CURSOR_WORKSPACE_LABEL`. Henchline
  keys the chat by the first (`cursor-<id>`) and names it by Cursor's title for it (read only from Cursor's store,
  `state.vscdb`), so several chats in one window are told apart.
- Each Cursor window runs the extension, which listens on a private socket in
  `~/.local/state/ptt/cursor-bridge/` (folder 0700, socket 0600) and accepts a few requests about one chat by
  its id: open it (Cursor's `composer.openComposer`), stop it, and set its model (below). No text reaches a chat
  through it, and no other command runs (text goes only to an agent in one of its terminals, below).
- Cursor says yes to any id, so Henchline picks the window itself: the one whose workspace folder is the project
  holding that chat's transcript (`~/.cursor/projects/<project>/agent-transcripts/<id>`). No such window, or two:
  nothing is typed, and the phone says why.
- Then the words go in through Cursor's debugging port: Henchline finds that chat's input box on the page by its
  id, puts the words in and presses Enter there. No keys on the desktop, no window raised or focused, and it
  works with the screen locked. `./install.sh agents` adds `--remote-debugging-port=9333` to Cursor's launcher
  (`~/.local/share/applications/cursor.desktop`); it takes effect when Cursor next starts. The port listens on
  localhost, but while it is open any program or user on this computer can drive Cursor through it (agents
  included, Cursor's own too). Driving Cursor is running code in it: the debugging port evaluates any JavaScript
  in Cursor's window, which can read your files and start programs as you, with no password. So the port, and the
  bridge that needs it, are for a computer only you log in to; on a shared machine leave the port off (remove the
  flag from the launcher) and Henchline types into the raised window instead. Focus inside Cursor moves to the chat box for that moment and is put back.
- Without the port (Cursor started another way), Henchline brings the window forward and types into it as before
  (the chat box, paste, Enter), which needs the screen unlocked.
- The phone shows each chat's model (with its effort) from Cursor's store, and switches it with Cursor's own "Switch
  to Model Slug" (`{"model": "<chat id>", "config": {...}}`), on that chat as below. As when the
  model is changed in Cursor itself, it also becomes Cursor's default for new chats. The effort and fast picked
  go with it, and the chat's context size stays when the model does. The chat's model is read back from the store:
  the phone says so if Cursor didn't take it.
- The phone's stop button runs Cursor's own stop (`composer.cancelComposerStep`) on that chat, only while it is
  working (its stop on an idle chat would reject all its pending edits).
- Stop and model switches act on Cursor's selected chat, the one last focused. With the debugging port, Henchline
  focuses that chat's box on the page and checks it holds focus, then asks with `"focused": true`, so a chat in
  any pane works; focus stays in that box afterwards. Without the port, only the chat Cursor lists last is
  accepted.

## Agents in a Cursor terminal

Claude, Codex or the Cursor CLI started in Cursor's own terminal panel has no tmux pane or Konsole tab, so the
bridge (0.2.0 and later) types into it instead:

- Henchline knows the terminal by its top process (its shell, with its start time, so a reused pid never matches)
  and finds the agent in front of it from the process tree. Which window shows it is asked each time:
  `{"terminals": true}` lists a window's own terminals, and exactly one window must list it.
- `{"type": {shell, shell_born, front, front_born, agent, agent_born, text, enter}}` writes the words into that
  terminal as one bracketed paste (control characters taken out, here too), then Enter. Right before each write,
  the bridge checks in `/proc` that the program in front of that terminal and the agent (itself, or its child) are
  still the ones Henchline named (pids and start times), and that nothing else in that process group reads the
  terminal (an editor the agent opened). A shell or any other program in front: nothing is written. Requests for
  one terminal run one at a time.
- The paste brackets are always sent. tmux sends them only when the program in front asked for bracketed paste;
  Claude, Codex and the Cursor CLI are expected to ask, but a program that hasn't would show `[200~` around the
  words. `{"escape"}` (the phone's stop)
  and `{"enter"}` (the Cursor CLI's "send now") take the same check.
- No screen is read back: whether the words arrived is read from the agent's own log, as the phone's ticks are.
  Switching a model there, and reading its plan or its screen, work only in tmux.

## Limits

- Linux and X11, like the rest of Henchline. Without the debugging port, a locked screen can't be typed into.
- Cursor's internal commands and its store's layout aren't a public API: a Cursor update can change them, and then
  the bridge refuses rather than typing into the wrong chat. The same holds for its page: the debugging-port path
  finds a chat's box by the chat id on the page (`data-composer-id`) and checks the words are in that box before
  pressing Enter, so a changed page means nothing is sent, and the phone says why.

## Thought bubbles

While a Cursor IDE chat is open on the phone, its thinking (what Cursor's IDE lets you expand under each step) is
read from Cursor's store and shown as dashed thought bubbles. Only while you look: never pushed, never a
notification, not kept in the chat. Settings → Show agents' thinking turns them off.
