# Hands-free

`handsfree on` talks to the same agents without a key (`handsfree/handsfree.py` is the command; `listener.py` decides when to listen, `sensing.py` hears the mic, wake word, earbuds and speakers, `routing.py` picks the agent and types to it, `tray.py` is the icon; user unit `handsfree/system/handsfree.service`; `./install.sh handsfree` installs it with talkback, and `handsfree on` also starts it at every login). Its echo canceller (`handsfree/system/talkback-echo-cancel.conf`, its own user unit) captures the mic on the same sound card as the speakers, which shares their clock; the installer picks that mic. It listens for a few seconds after an agent speaks, while an agent speaks (talking over it stops it), and for a wake word the rest of the time. The words are typed into the Konsole tab of the agent that spoke last, prefixed `[hands-free · how it was heard]`. "Over" sends at once, "give me a minute" keeps listening, "never mind" drops it, "hands-free off" ends it. A tap on Bluetooth earbuds works like the wake word, silently: hands-free grabs the earbuds' media-key device ("… (AVRCP)") while it runs, so the tap does not also pause music, and lets go when it stops. Earbuds send those keys in their music profile (A2DP); in the headset profile a tap becomes a call command that never reaches hands-free. Hands-free listens through the laptop mic anyway, so music mode loses nothing.

**Keep laptop speakers moderate.** Near full volume, laptop speakers distort enough that no echo canceller can follow, and hands-free starts hearing its own voice as yours: it cuts itself off, and its own words can come back to the agent as your message. Measured on a Dell Precision 5690: at 80% the canceller left the agent's voice about 60 dB below the playback and nothing went wrong; at 100% only 6 to 13 dB, and it heard itself over and over. WebRTC's newer canceller (AEC3), tried on the same recordings, failed at the same moments, so this is not one canceller's bug. Up to 80% worked in our tests; with earbuds or headphones there is no echo to worry about. Talking over a clip stops it once about half a second of your voice has come through; a brief echo slip is shorter than that.

Replies go to the agent that spoke last; switching is by voice.

## Switching agents: fixed phrases, on purpose

Switching recognises a handful of phrase shapes, not free-form language: there is no language model in the loop, by choice, to keep the dependencies small. Say the whole utterance as one of these (an agent name is Claude, Cursor or Codex; "in <workspace>" picks one of several):

| Say | Does |
|---|---|
| "switch to Cursor", "hand it back to Claude", "pass me over to Cursor", "back to Claude", "let me talk to Cursor in copilot ptt", "put me through to Claude", "can you switch me to Cursor?" | your replies go to that agent from now on |
| "send that to Claude" | forwards your last message there, and switches |
| "…never mind / scratch that / forget what I said, hand this to Cursor" | drops what came before, then switches |
| "who am I talking to?" | says who your replies go to |

An agent asked to hand you over runs `handsfree switch claude|cursor|codex [workspace]`, which does the same and announces it (the agent snippet tells them to).

A leading "okay", "no" or "actually" is fine. Anything negated ("don't switch me to Claude") or longer is an ordinary message to the current agent. A switch never sends your words anywhere by itself and is announced out loud, so a misheard one is undone with "back to …". `handsfree listen N` and `handsfree close` widen or close the window.

Turns are transcribed while they are recorded by NVIDIA's Nemotron Speech Streaming (`voice/speech/nemotron.py`, run by sherpa-onnx on the CPU, with the hint words from `~/.config/ptt/config.json`), so the words are ready about 0.15 s after you stop, on battery too. `install.sh handsfree` downloads it (about 630 MB, NVIDIA Open Model License) into `~/.local/share/handsfree/nemotron`; without it, or with `"streaming": false` in the config, Whisper transcribes each turn once it has ended, as before. With Nemotron's words so far, a turn that already ends a sentence ends after a shorter pause (0.8 s instead of 1.5 s; 0.6 s instead of 0.9 s on a call); `"early_end": false` in the config waits the full pause every time.

The wake word and a trailing "over" are confirmed by Whistle (Cactus Compute, Apache-2.0, 17 MB, `voice/speech/whistle.py`) in about 0.15 s on the CPU, so a wake on battery is quick; `install.sh handsfree` fetches it into `~/.local/share/handsfree/whistle`. Without it, Whisper confirms them, which takes about 2.5 s on the CPU.

The wake word is a personal detector: `handsfree/tools/wakeword_collect.py` records you at a few spots around the house, `handsfree/tools/wakeword_train.py` fits it on openWakeWord embeddings (`pip install --user --no-deps openwakeword`; its TFLite dependency has no Python 3.12 build), and Whistle (else Whisper) confirms each candidate on just those two seconds.

Log: `~/.local/state/ptt/handsfree.log` (events, no words)
