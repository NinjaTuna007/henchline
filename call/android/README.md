# Henchline, the Android app

`call/android/` is **Henchline**, the same call as an Android app (Kotlin, OkHttp, Android 10+): you, calling your crew of agents. The call runs in a foreground service, so it keeps going with the screen off or the app in the background, which a browser tab does not; the phone's own voice-call audio cancels its echo, and the voice goes to earbuds or a headset when there are some, else the loudspeaker. The screen is the page's: who you talk to, the round talk button, the chat. Build and install over USB (needs the Android SDK, platform 35):

```bash
cd call/android
echo "sdk.dir=$HOME/Android/Sdk" > local.properties   # where your SDK is
./gradlew assembleDebug && adb install -r app/build/outputs/apk/debug/app-debug.apk
```

## Settings and sound

Hold your avatar for Settings: the laptop's address (`https://<laptop's Tailscale name>/`), how calls start (Clear sound or Call sound), Stay or check and Cut in for new calls, and the agents' voices. On a call, tapping the green call bar opens the call menu, which changes this call only:

- **Sound:** Speaker or Earpiece (and Earbuds when some are connected); Clear sound (media sound, with the phone's own echo canceller where it has one; the default) or Call sound (the phone-call path, muffled but with the phone's call processing).
- **The line:** Open line or Hold to talk; Stay on (no "are you still there?", no hang-up on a quiet line; with Hold to talk, a walkie-talkie) or Check on me; Cut in (what you say interrupts the agent's work) or Let it wait (it reaches the agent when it finishes; untouched, Cursor cuts in and the others wait).
- **Voices:** where talkback is made, Laptop or Phone · Jenny, switchable mid-call with a line in the new voice, and each agent's laptop voice.

Speak on the phone: Voices → Phone. Jenny (about 64 MB) downloads from your laptop, not the internet, once the laptop has prepared it (`./install.sh call` does; by hand, `~/.local/share/henchline/venv/bin/python -m voice.phonevoice prepare` in this repository).

## Hanging up

Saying "hang up" or "end the call" ends the call from the laptop (an agent can run `handsfree hangup`); a typed "hang up" goes to the agent as a message, the app's Hang up button is for that.

## Rings

`handsfree ring "why"` asks you to call: an ntfy notification (topic read from `~/.config/ptt/ntfy-topic`) whose Answer opens Henchline on an incoming-call screen (Answer connects; the henchline://call link alone never turns the mic on, since any app could open it), at most once every two minutes (`--now` when you asked for the ring). Henchline reports back how the ring went (rang, declined, missed, couldn't reach the laptop) on a one-time ntfy address the ring carries; like the push address it is a secret, never shown, used once. Going back out of the ring screen counts as declining it. It waits for the answer and tells the agent: exit 0 answered, 2 no answer, in which case a "Missed call from <agent>" notification with the reason is left in ntfy. The wait is up to a minute (a locked phone connects only once unlocked), so an agent should run it in the background and read the exit code, or pass `--no-wait`. Once Henchline has registered with the phone's UnifiedPush distributor (the ntfy app) and made a call, `handsfree ring` makes it ring full-screen instead: the calling agent's henchman, its own ringtone and a henchman-speak phrase (`call/android/ringtones.py`). The push address is kept in `~/.local/state/ptt/call.ring-endpoint` (private; only ntfy.sh addresses are accepted, so a self-hosted ntfy distributor needs its host added to `RING_HOSTS` in `call/server.py`) and works like an ntfy topic: whoever has it can make the phone ring. On a locked phone, Answer connects once the phone is unlocked. While you are using the phone, Android shows a ring as a banner instead of full screen; the banner then rings with the agent's tone and has Answer and Decline. Leaving the ring screen without a button keeps the call ringing as a banner until it times out; opening the app while it rings shows it with Answer and Decline; a call made by hand during a ring answers it. A ring left as a banner and declined after the laptop stopped waiting leaves both a missed call in ntfy and the decline.

## Your own voice on the ring

To ring in your own voice: `call/android/record_phrases.py` (each phrase on screen, recorded from your mic) and `call/android/henchify_takes.py` (cleaned and made henchman-sized) put your phrases into `app/src/debug/res/raw/`, which only your own build uses; it is git-ignored, and a pre-commit hook in your clone (`.git/hooks/pre-commit`, refusing that folder) keeps your voice out of the repository. The banner rings with the agent's tone and phrases mixed into one sound (`call/android/banner_mix.py`, run by `ringtones.py` and `henchify_takes.py`).

## Do Not Disturb

With Do Not Disturb access (the app asks once; or `adb shell cmd notification allow_dnd io.github.ninjatuna007.henchline`), rings get through Do Not Disturb, playing as an alarm while it is on.

## Texts

Agents can text instead of ringing: `handsfree text "words"` shows a message notification on the phone with a reply box; the reply comes back to that agent tagged `[phone · text]` (on a call the message goes to the chat instead).

## Earbuds and quick controls

On a call, the earbuds' button (play/pause) is push-to-talk: a tap starts talking with a rising beep, the next tap sends with a falling one; the call's notification has the same Talk/Send button, on the lock screen too. A Quick Settings tile (call, or hang up) is there to add from the Quick Settings editor; Android never adds it by itself.

## Terminal mirror

On a call, a Claude Code hook (`call/hooks/mirror.sh`, added to `~/.claude/settings.json` by `./install.sh call`; off a call it only tests a file) also puts what you type in the terminal and Claude's written replies (not its tool calls) into that agent's chat. The phone keeps them with the rest of the chat (90 days, app-private, not backed up); key blocks, tokens and passwords in common shapes are replaced by [hidden] first, but not everything secret has a recognisable shape. `call/hooks/claude_hooks.py remove` takes the hook out.

## Certificates

The app trusts only real certificates, so it needs the `tailscale serve` address, not the self-signed fallback (`--host tailscale`).

## Icons and drawings

Thirteen of its interface icons (listed in `call/android/LICENSE-MATERIAL-ICONS`) are Google's Material Design icons, under the Apache License 2.0; the henchmen, and you as their boss next to your own messages, are drawn by `call/android/avatars.py`.

## Files and chats

Files go both ways. The paperclip beside the chat's message box attaches pictures or files for the agent whose chat is showing (with anything typed in the box). Share anything (pictures, video, audio, zips, documents, links) from any app's Share menu to Henchline: it uploads to the laptop over Tailscale into `~/Downloads/Henchline/<date>/` (nothing overwritten, refused if it would leave the laptop's disk under 2 GB free), and the agent you are talking to gets one message, tagged `[phone · shared]`, with each file's path, type and size and any words you added. An agent runs `handsfree send FILE [FILE ...] [WORDS]`: on a call the file appears in the chat (pictures shown, audio with a play button, anything else a card), otherwise as a notification (if that can't reach the phone, it waits for the next call's chat; ntfy only says a file is waiting, never its link). Keys and credentials (`~/.ssh`, `~/.gnupg`, `*.pem`, `id_*` and the like) are refused, but any other file an agent can read it can send: the agent is trusted with your files already. Opening one saves it to the phone's Downloads/Henchline and opens it there or in the app for its type. The laptop keeps an agent's file for a week at `/files/<secret>/<name>`; only the secret finds it, and only your own devices reach the server. Only pictures, audio and video are shown in a browser (sandboxed); anything else, HTML and SVG included, is served as a download, so a file can't run script on the call page's address. One upload is at most 16 GB. The call page shows them too. Henchline keeps each agent's chat on the phone (the newest 400 messages per agent session), readable off a call; files sent while no call was up land in that agent's chat as well.

## Voice messages, new chats and call history

Off a call, an empty message box turns the send button into a mic. Hold it and speak; let go to send. Slide left to drop it, slide up to lock it on (a clock runs in the box; the button then sends and ✕ drops). Leaving the app drops a held recording and sends a locked one; five minutes is the most. The laptop's Whisper (the one hands-free uses) writes it down and types it to that chat's agent tagged `[phone · voice message]`; the bubble shows what was heard, or why it wasn't sent. A recording not transcribed within 150 seconds is dropped, never typed late.

The round button on the chat list starts a new chat: the laptop offers the folders Claude Code already trusts (so its trust prompt never takes your first message), newest first, and the agents installed (Claude, Codex, the Cursor CLI). The agent starts in its own window of the tmux session `henchline` (`tmux attach -t henchline` to watch or type there), in a systemd scope of its own so restarting the call server leaves it running. Words reach it through a tmux paste buffer, so this works on any desktop. Agents started this way never wait for a keypress the phone can't give: Claude starts in auto mode, the Cursor CLI on Run Everything (`--force`), Codex sends its approvals to its own reviewer (`approvals_reviewer = "auto_review"` and `check_for_update_on_startup = false` in `~/.codex/config.toml`), and a folder trust question is answered yes only when that exact question, with that folder's path, is on screen (you get a notification when it is). Long-press a chat to clear it on the phone only, or, for an agent the phone started, to end it (asked first). Agents you started yourself are never ended from the phone.

The bottom bar has three tabs, Usage, Chats and Calls; swipe sideways between them. Usage shows every agent's plan limits, reset times and context. Calls lists rings (who, why, answered, declined, missed or out of reach) and calls with their length; an ended call also leaves a line in its chat. Chat lines show their time, with a line between days.

Messages from the phone off a call are tagged `[phone · text]`, `[phone · voice message]` or `[phone · shared]` (calls stay `[hands-free · call …]`), and the agents are told to answer those with `handsfree text`.

## In the app

- **Away:** tap your avatar (sunglasses on). Agents text you instead of speaking or ringing, and the laptop stops listening. Tap again to come back.
- **Outbox and later:** a message typed while the laptop is out of reach waits with a clock and goes when it can. Hold send and slide up, or type `/later your words`, to pick a time to send it.
- **Interrupt and ticks:** hold send (it turns amber) and let go to send now, interrupting the agent; slide left to cancel. One tick when sent, two once the agent takes it in.
- **Plans:** a Plan tab in the chat shows the agent's steps and progress; the chip beside the message box puts Claude or the Cursor CLI into plan mode and back.
- **Workflows:** while one of Claude's workflows runs, a strip above the chat shows its phase; tap it for a sheet with each phase and what its agents found.
- **Commands:** type `/` for the agent's commands and skills, and the app's own (`/later`, `/usage`, `/away`, `/show` and more).
- **Selecting:** long-press a message and choose Select, then tap more; copy, share or delete them. Swipe a message to reply to it.
- **The crew:** a helper's chat sits under the agent that started it. A white hard hat runs a crew, a yellow one is on one, no hat works alone. Tap an avatar to see its helpers. A finished helper you were talking to, or that holds words not yet reported, waits for End or Keep.
