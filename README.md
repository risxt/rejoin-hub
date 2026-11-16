# Rejoin Hub agent

A small agent for rooted Android cloud phones (Redfinger, LDCloud, emulators). It keeps Roblox clones in game: it
rejoins after crashes, kicks and disconnects, refreshes the Delta key through izen with the owner's own API key, tiles
the windows, and takes orders from a private dashboard over Firebase.

This repository holds only the phone side. It contains no keys, tokens or passwords. Each phone gets its access from
a pairing code that the owner's dashboard generates.

## Install on a phone (Termux, root on)

Copy the command from the dashboard (Settings → Connection → Install on a new phone) and paste it into Termux. It
looks like this:

```bash
curl -fsSL https://raw.githubusercontent.com/risxt/rejoin-hub/main/agent/install.sh | bash -s -- RH1.<code> "RF-01"
```

Run the same line without a code to update.

## Commands

| Command | What it does |
|---|---|
| `rh status` | Shows pairing and whether the agent runs |
| `rh log` | Follows the agent log |
| `rh doctor` | Read-only check of root, clones, executor folders, windows |
| `rh stop` / `rh start` | Stops or starts the background agent |

## Requirements

- Termux from F-Droid
- Root granted to Termux
- Python 3.10+ (the installer adds it)

The agent uses only the Python standard library.
