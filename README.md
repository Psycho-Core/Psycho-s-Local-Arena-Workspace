# Arena Workspace Bridge

Connect a folder on your **Windows PC** to the Arena agent working in this chat —
so I can browse, read, search, and edit your local files directly.

```
┌────────────────────────────┐            ┌─────────────────────────────┐
│  Your Windows PC           │            │  Arena agent sandbox        │
│                            │  outbound  │                             │
│  arena_bridge.py (GUI)     │   HTTPS    │  relay.py  ◄── agent talks  │
│   └─ the folder you pick   │◄──────────►│  (public preview URL)       │
│      long-polls for ops    │            │                             │
└────────────────────────────┘            └─────────────────────────────┘
```

Your PC makes **outbound** connections only — no firewall holes, no port forwarding,
no account. One shared random token authenticates everything.

---

## Quick start (Windows)

1. **Install Python 3** if you don't have it:
   - Download from <https://www.python.org/downloads/> and tick **"Add python.exe to PATH"**, or
   - run `winget install Python.Python.3.12` in a terminal.
2. **Get the files** — download the `desktop/` folder (`arena_bridge.py` + `run_bridge.bat`)
   anywhere on your PC, e.g. `C:\Tools\arena-bridge\`.
3. **Ask the agent** (in the Arena chat) to start the relay. It will give you:
   - a **Relay URL** (looks like `https://8765-xxxx.e2b.app`)
   - a **Token** (long random string)
   - a **Traffic token** (see below — needed to reach the relay from your PC)
4. **Run** `run_bridge.bat` (or `python arena_bridge.py`).
5. Paste the **Relay URL**, **Token** and **Traffic token**, **Browse** to the folder
   you want to share, tick permissions, click **Connect**.

Status turns green when the agent can see your folder. Tell me in the chat and I'll
start working on your files. The window log shows every operation I perform.

Settings are remembered in `%USERPROFILE%\.arena_bridge_config.json` — next time
you just double-click and hit Connect.

### Traffic token (preview proxy)

The relay URL lives behind Arena's sandbox proxy (e2b), which requires one extra
header — `e2b-traffic-access-token`. The **Arena platform automatically supplies it
in your browser** (that's what powers the live preview), but a desktop app has to
send it itself. To get the value:

1. Click the **live preview** of the relay in the Arena chat (or open the Relay
   URL in that same browser).
2. Open **DevTools → Network**, refresh, click any request to the relay.
3. Copy the value of the request header `e2b-traffic-access-token`
   (or the same-named cookie under *Application → Cookies*).
4. Paste it into the **Traffic token** field in the app.

It's per-sandbox and expires with the session — when the agent gives you a new
Relay URL, grab a fresh traffic token the same way.

### Headless mode (optional)

```
python arena_bridge.py --relay https://8765-xxxx.e2b.app --token PASTE-TOKEN ^
    --e2b-token PASTE-TRAFFIC-TOKEN --root C:\work\my-project
python arena_bridge.py ... --read-only            # no writes
python arena_bridge.py ... --allow-delete         # permit deletes
```

---

## What the agent can do

| Op       | Description                                        |
|----------|----------------------------------------------------|
| `ping`   | Client info: hostname, folder, permissions         |
| `list`   | List a directory                                   |
| `stat`   | File/dir metadata                                  |
| `read`   | Read a file (≤10 MB; binary returned as base64)    |
| `write`  | Create/overwrite/append a file                     |
| `mkdir`  | Create a directory                                 |
| `delete` | Delete a file or dir (toggle; recursive is explicit)|
| `move`   | Rename / move within the shared folder             |
| `glob`   | Find files by name pattern                         |
| `grep`   | Search file contents with a regex                  |

**Deliberately NOT included:** running shell commands or programs on your PC.
This is a file bridge only. (An opt-in "allow commands" mode may come later.)

---

## Security model

- **Folder jail** — only the folder you pick is reachable. Every path is
  `realpath`-resolved and rejected if it escapes the root (including `..`,
  absolute paths, and symlink escapes).
- **You hold the switches** — reads are always on; writes and deletes are
  separate checkboxes (deletes default **off**). The app refuses ops that
  exceed the toggles.
- **Shared random token** — every request must carry the session token.
  Don't paste it into public chats/screenshots.
- **Traffic token** — the e2b preview proxy additionally requires the
  `e2b-traffic-access-token` header (grab it from your browser as described
  above). It gates *all* traffic into this sandbox, not just the bridge.
- **No inbound ports on your PC** — the app dials out to the relay over HTTPS.
- **Everything is logged** — the app window shows each operation as it happens.
- **You can stop it anytime** — Disconnect button, or just close the window.

Treat the token like a password for the shared folder: anyone holding the URL +
token could queue file commands for the connected client. The token is
single-session and changes whenever a new relay is started.

---

## Session lifetime & reconnecting

The relay lives in the Arena chat sandbox. If the sandbox restarts, the agent
will start a new relay and give you a **new URL and token** — paste them into
the app and reconnect. Your client auto-retries with backoff if the connection
drops. For a permanent setup you can run `relay/relay.py` yourself on any
always-on machine (`python relay.py --port 8765 --token <pick-one>`) and point
the app at it.

---

## Getting an Arena API key

You asked about an Arena API key — **yes, keys exist** and you create them
yourself in the Arena API dashboard:

- **Create a key:** <https://portal.api.preview.arena.ai/dashboard/keys>
  (open the Keys page → *Create a new key* → copy it; it's shown only once).
- **What it's for:** the Arena gateway (`https://api.preview.arena.ai`) is
  Anthropic-compatible — it can power Claude Code / Claude Desktop / OpenCode
  on your machine instead of an Anthropic account. There's even a quickstart
  script for that: `curl -fsSL https://portal.api.preview.arena.ai/setup.sh | bash`
- **Good to know:** free-router keys are currently limited to the Claude Code
  harness (Cowork / Agent SDK return 403 unless your workspace has API access),
  so it's in preview and scope may vary by plan.

**You don't need an API key for this bridge** — I'm already the AI on this side
of the chat. A key only becomes useful if you later want the desktop app to
have its own standalone AI features (see Roadmap).

---

## How it works (protocol)

All JSON over HTTP. Auth header: `X-Bridge-Token: <token>`.

| Call | Who | Purpose |
|------|-----|---------|
| `GET /health` | anyone | liveness probe |
| `GET /status` | agent | clients + queue summary |
| `POST /register` | client | announce client + workspace label |
| `GET /poll?wait=25` | client | long-poll for the next command |
| `POST /result` | client | report command outcome |
| `POST /cmd` | agent | enqueue `{op, params}` → `{id}` |
| `GET /cmd/<id>?wait=25` | agent | long-poll for the result |
| `POST /cmd/<id>/requeue` | agent | resend a command lost mid-flight |

Command status lifecycle: `pending → sent → done | error`.

---

## Repo layout

```
arena-bridge/
├── README.md               ← you are here
├── desktop/
│   ├── arena_bridge.py     ← Windows app (GUI + headless, stdlib only)
│   └── run_bridge.bat      ← double-click launcher
├── relay/
│   └── relay.py            ← relay server (runs on the agent side)
└── tests/
    ├── test_flow.py        ← protocol suite: 24 checks (ops, jail, permissions)
    ├── test_gui_smoke.py   ← GUI suite: 9 checks (every window code path)
    └── test_e2e_process.py ← process suite: 10 checks (real entrypoints end-to-end)
```

Run the tests anywhere (needs nothing installed):

```
python3 tests/test_flow.py
python3 tests/test_gui_smoke.py
python3 tests/test_e2e_process.py
```

---

## Roadmap ideas

- [ ] Opt-in "allow command execution" with per-command approval prompts
- [ ] System-tray mode with pause/resume
- [ ] Standalone AI chat in the app via an Arena API key
- [ ] One-file `.exe` build via PyInstaller (`pyinstaller -F -w arena_bridge.py`)
- [ ] Multi-folder shares and per-folder permissions
