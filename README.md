<p align="center">
  <img src="assets/logo-enhanced.png" width="220" alt="Psycho's Local Arena Workspace logo">
</p>

<h1 align="center">Psycho's Local Arena Workspace</h1>

<p align="center">
  <b>Connect a folder on your PC to an AI agent working in a browser chat.</b><br>
  The agent can browse, read, search, and edit your local files — live, securely,
  and only inside the folder you choose.
</p>

<p align="center">
  <a href="#-quick-start-windows">Quick Start</a> ·
  <a href="#-how-it-works">How It Works</a> ·
  <a href="#-what-the-agent-can-do">Capabilities</a> ·
  <a href="#-security">Security</a> ·
  <a href="#-the-traffic-token">Traffic Token</a> ·
  <a href="SESSION_GUIDE.md">Session Guide</a>
</p>

<p align="center">
  <sub>Powered by the Arena Workspace Bridge engine · Python 3.8+ · stdlib only · Windows / macOS / Linux · outbound-only · no accounts · no telemetry</sub>
</p>

---

## Why

AI coding agents usually run in the cloud and can't touch the files on your
machine. **Psycho's Local Arena Workspace** fixes that: a tiny desktop app dials
*out* to a relay run by your agent session — no firewall holes, no port
forwarding, no cloud storage middleman. Pick a folder, and that folder becomes
the agent's workspace.

## ✨ Features

- **Zero install** — one Python file, standard library only; `run_bridge.bat` and go
- **Outbound-only** — your PC never accepts incoming connections
- **Folder jail** — `..`, absolute paths, and symlink escapes are all rejected
- **You hold the switches** — reads always work; writes and deletes are separate
  toggles (deletes off by default)
- **No shell access** — filesystem operations only (deliberate; see Roadmap)
- **Welcome document** — every fresh connection drops a `HELLO.txt` into your
  shared folder, complete with the mascot in ASCII art
- **Live activity log** — every operation the agent performs, on screen, as it happens
- **Auto-reconnect with backoff** — survives network blips; remembers your settings
- **Batch mode** — up to 25 operations in a single round trip
- **Binary-safe** — reads/writes images and binaries via base64

## 🚀 Quick Start (Windows)

1. **Install Python 3** — [python.org](https://www.python.org/downloads/) (tick
   *"Add python.exe to PATH"*) or `winget install Python.Python.3.12`.
2. **Get the app** — download `desktop/arena_bridge.py` + `desktop/run_bridge.bat`
   anywhere on your PC and double-click `run_bridge.bat`.
3. **Ask your agent session** to start the relay. It will hand you three things:
   the **Relay URL**, a **Token**, and instructions for the **Traffic token**
   (see [below](#-the-traffic-token)).
4. **Paste all three into the app**, pick the folder to share, tick permissions,
   hit **Connect**. Status turns green when the agent can see your folder — and a
   friendly `HELLO.txt` appears in the folder to prove it.

Settings are remembered in `%USERPROFILE%\.arena_bridge_config.json` — next time
you just double-click and Connect.

Headless mode (optional):

```
python arena_bridge.py --relay https://8765-xxxx.e2b.app --token PASTE-TOKEN ^
    --e2b-token PASTE-TRAFFIC-TOKEN --root C:\work\my-project [--read-only] [--allow-delete]
```

## 🏗 How It Works

```
┌────────────────────────────┐            ┌─────────────────────────────┐
│  Your PC                   │            │  Agent sandbox              │
│                            │  outbound  │                             │
│  arena_bridge.py (GUI)     │   HTTPS    │  relay.py  ◄── agent talks  │
│   └─ the folder you pick   │◄──────────►│  (public preview URL)       │
│      long-polls for ops    │            │                             │
└────────────────────────────┘            └─────────────────────────────┘
```

Your app keeps a long-poll open against the relay. The agent enqueues JSON
commands (`read`, `write`, `grep`, …); the app executes them inside the folder
jail and posts results back. A shared random token authenticates every request,
and every fresh connection regenerates `HELLO.txt` in your folder.

## 🤖 What the Agent Can Do

| Op       | Description                                        |
|----------|----------------------------------------------------|
| `ping`   | Client info: hostname, folder, permissions         |
| `list`   | List a directory                                   |
| `stat`   | File/dir metadata                                  |
| `read`   | Read a file (≤10 MB; binary returned as base64)    |
| `write`  | Create / overwrite / append a file                 |
| `mkdir`  | Create a directory                                 |
| `delete` | Delete a file or dir (toggle; recursive is explicit)|
| `move`   | Rename / move within the shared folder             |
| `glob`   | Find files by name pattern                         |
| `grep`   | Search file contents with a regex                  |
| `batch`  | Run up to 25 of the above in one round trip        |

**Deliberately NOT included:** running shell commands or programs on your PC.

## 🔑 The Traffic Token

The relay lives behind the sandbox preview proxy (e2b), which requires one extra
header — `e2b-traffic-access-token`. **Your browser gets it automatically** from
the platform (that's what powers live previews), but a desktop app must present
it itself. To grab the value:

1. Open the Relay URL in the same browser where your agent chat is open —
   the landing page shows **"Your traffic token"** in a copyable box.
2. Not shown? Press **F12 → Network → Ctrl+R**, click the first request, and copy
   the `e2b-traffic-access-token` **request header** (fallback: **Application →
   Cookies**).

It's per-session: when a new agent session gives you a new Relay URL, grab a
fresh one the same way.

## 🔒 Security

- **Folder jail** — every path is `realpath`-resolved and rejected if it escapes
  the shared root (including `..`, absolute paths, and symlink tricks).
- **Two secrets** — a session bridge token *and* the proxy traffic token gate
  all traffic.
- **Permission toggles** — reads always on; writes and deletes are yours to
  grant (deletes default **off**).
- **No command execution** — ever, in this version.
- **Full transparency** — the app's activity log shows every operation;
  Disconnect or close the window to stop instantly.

Treat the tokens like passwords for the shared folder. They rotate every session.

## 🧪 Testing

Three suites, **43 checks**, nothing to install:

```
python3 tests/test_flow.py        # 24 — protocol ops, path jail, permissions
python3 tests/test_gui_smoke.py   #  9 — every GUI code path (display-free)
python3 tests/test_e2e_process.py # 10 — real entrypoints, end-to-end
```

## 📦 Repo Layout

```
psycho-local-arena-workspace/
├── README.md               ← you are here
├── SESSION_GUIDE.md        ← how a new agent session reconnects the bridge
├── assets/logo-512.png     ← the mascot
├── desktop/
│   ├── arena_bridge.py     ← desktop app (GUI + headless, stdlib only)
│   └── run_bridge.bat      ← double-click launcher (Windows)
├── relay/
│   └── relay.py            ← relay server (agent side; auto-sends HELLO.txt)
├── agent_cli.py            ← agent-side command helper
└── tests/                  ← the three test suites
```

## 🗺 Roadmap

- [ ] Opt-in "allow command execution" with per-command approval prompts
- [ ] System-tray mode with pause/resume
- [ ] Standalone AI chat in the app (bring your own API key)
- [ ] One-file `.exe` via PyInstaller (`pyinstaller -F -w arena_bridge.py`)
- [ ] Multi-folder shares and per-folder permissions

---

<p align="center">
  <sub>Psycho's Local Arena Workspace · Add a license before publishing.</sub>
</p>
