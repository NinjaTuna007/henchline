# Henchline bridge for Cursor

Without it, Henchline can only type into the Cursor chat that happens to be open. With it, each Cursor IDE chat is
its own chat on the phone: named by Cursor's own title, and what you text, record or switch reaches that very chat.

## Install

```bash
./install.sh agents      # with Cursor installed: packs the extension and runs `cursor --install-extension`
```

No Node tools are needed (`pack.py` makes the `.vsix`). A Cursor window picks up a first install without a reload;
after an update, run **Developer: Reload Window** (Ctrl+Shift+P) when no chat is in the middle of work (restarting
the extension host alone keeps the old version).

## How it works

- A Cursor chat's commands run with `CURSOR_CONVERSATION_ID` (its chat id) and `CURSOR_WORKSPACE_LABEL`. Henchline
  keys the chat by the first (`cursor-<id>`) and names it by Cursor's title for it (read only from Cursor's store,
  `state.vscdb`), so several chats in one window are told apart.
- Each Cursor window runs the extension, which listens on a private socket in
  `~/.local/state/ptt/cursor-bridge/` (folder 0700, socket 0600) and accepts a few requests about one chat by
  its id: open it (Cursor's `composer.openComposer`), stop it, and set its model (below). No text and no other
  command.
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
