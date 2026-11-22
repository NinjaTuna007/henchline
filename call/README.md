# The phone call

`./install.sh call` runs a small web server (`call/server.py`, user unit `call.service`) on the laptop's Tailscale address, so only your own devices (and programs on the laptop) reach it, behind `tailscale serve` at `https://<laptop's Tailscale name>/` with a real certificate. Tailscale must let your user configure it, once: `sudo tailscale set --operator=$USER` (and HTTPS enabled for the tailnet). Until then it listens on the laptop's Tailscale address with a certificate of its own (`https://<laptop's Tailscale IP>:8790`; the phone accepts it once). Open the address on the phone and tap **Call**: your mic goes into a virtual sink that hands-free listens to instead of the laptop mic, and talkback plays into another one that is streamed to the phone, so the laptop stays quiet. The phone's browser cancels its own echo. A call turns hands-free and talkback on, and leaves them on when it ends.

- On a call the line is open: just talk, no wake word needed; talking over the agent and switching agents work as at home.
- The round button shows hands-free's state in the tray's colours (green listening, red recording, yellow transcribing, grey only the button gets in). **Hold** it to talk; **tap** it to turn the open line off in a noisy place, so noise cannot cut the agent off, and on again.
- The chat shows what the agents say and when a spoken message went out; typed messages go to the agent that spoke last, and typed switch commands switch.
- Nothing is typed while the laptop's screen is locked: agents in a terminal tab still get your words (over Konsole's D-Bus, no keys), the Cursor editor does not. Cursor's command-line agent in a terminal tab works.
- Keep the page in front: phone browsers may stop the microphone in the background (the Android app does not).

`--host tailscale` keeps the self-signed way even when `tailscale serve` would work.

The Android app, Henchline: [android/README.md](android/README.md).

## The signed host API (preview)

`voice-call --api-v1` also serves a versioned API for phones paired with this computer (more than one computer
per phone). It is off unless asked for, and it needs `~/.config/henchline/host.json`.

- `voice-call pair` shows the address, the key's fingerprint and a five-minute one-time code;
  `voice-call devices list` / `voice-call devices revoke ID` manage paired phones.
- `voice-call backup --output FILE` copies the host database (paired phones, the request ledger, chat history).
  The database's format is upgraded automatically and one way (version 2 adds where each chat event came from):
  to go back to an older build, restore the backup taken before the upgrade.
- Agents' texts to the phone are kept as chat history in `~/.local/share/henchline/host.sqlite3` for
  `ledger_days` (in host.json, default 30), so paired phones can read them; older ones are pruned and not
  taken in again.
- With `"legacy_routes": true` in host.json's capabilities, the server's other routes (texts, agents, the
  call...) also take a request signed by a paired phone, as that phone (revoking it cuts it off there too).
  Unsigned requests still need the call page's Origin, unless `--signed-only` is also given: then only paired
  phones get in, and the browser call page stops working on this computer. A signed upload (a shared file, a
  voice message) is kept only if what arrived matches the digest the phone signed.
  host.json's `base_url` should be the call page's own `https://` address: notification links use it when the
  call page hasn't been opened yet. Unlike `/api/v1`, these routes keep no ledger of request ids: a signature
  is never accepted twice, but a phone that retries with a fresh one makes the route run again.

