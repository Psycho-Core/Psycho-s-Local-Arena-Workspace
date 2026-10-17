# Psycho's Local Arena Workspace — Session Reconnect Guide

> **Tool name: Psycho's Local Arena Workspace** (engine: Arena Workspace Bridge).
> **Read this first if you are a NEW AI agent session.** This explains how to
> (re)connect to the operator's Windows workspace and how the traffic token works.
> Written: 2026-10-17.

## What this system is

A small bridge that lets an Arena agent session browse, read, search and edit a
folder on the operator's PC:

```
Operator's Windows PC                    Arena agent sandbox
+------------------------+   HTTPS   +----------------------------+
| arena_bridge.py (GUI)  | outbound  | relay.py  <-- agent talks  |
|  +- shared folder      |<--------->| public URL (e2b preview)   |
+------------------------+           +----------------------------+
```

## What persists where

| Thing | Where | Survives a new session? |
|-------|-------|------------------------|
| Desktop app + saved config | Operator's PC (`~/.arena_bridge_config.json`) | YES |
| Shared folder | Operator's PC (e.g. `D:\MangosCata-bots`) | YES |
| `relay.py`, `agent_cli.py`, running relay | Agent sandbox | **NO - rebuild** |
| This guide + toolkit | `tools/arena-bridge/` in the operator's repo | YES if pushed to GitHub |

## Step-by-step for a new agent session

1. **Get `relay.py` and `agent_cli.py`** - full sources are embedded at the bottom
   of this guide (copy them into your workspace). If the operator has pushed
   `tools/arena-bridge/` to GitHub you can instead fetch them:
   `curl -fsSL -o relay.py https://raw.githubusercontent.com/<user>/<repo>/<branch>/tools/arena-bridge/relay.py`
   (same for `agent_cli.py`).
2. **Generate a session token:**
   `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`
3. **Start the relay** (background process, must outlive your turn):
   `BRIDGE_TOKEN=<token> python3 relay/relay.py --port 8765`
   Bind is `0.0.0.0` so the preview proxy can reach it. Keep the same token for
   the whole session.
4. **Find the public URL:** `echo $E2B_SANDBOX_ID` ->
   `https://8765-$E2B_SANDBOX_ID.e2b.app` (format: `https://` + port + `-` + sandboxId + `.e2b.app`).
5. **Sanity check:** `curl -s http://127.0.0.1:8765/health` -> `{"ok": true ...}`.
6. **Give the operator these three things** (copy-paste):

   > Relay URL: `https://8765-<sandboxId>.e2b.app`
   > Token: `<session token>`
   > Traffic token: open the Relay URL in your Arena browser window - the page
   > shows **"Your traffic token"** in a copyable box. (If not: F12 -> Network ->
   > Ctrl+R -> click the first request -> Request headers -> copy
   > `e2b-traffic-access-token`. Fallback: Application -> Cookies -> same name.)

7. **Operator side:** open the bridge app (`run_bridge.bat`), paste Relay URL +
   Token + Traffic token, pick the folder, **Connect**. Settings are remembered -
   next time they just click Connect.
8. **Verify:** `python3 agent_cli.py ping` -> expect their hostname and root folder.

## Why the traffic token exists

The relay sits behind the e2b sandbox proxy, which requires the header
`e2b-traffic-access-token`. The Arena platform injects it automatically **in the
user's browser** (that is what powers live previews), but a desktop app must send
it itself. It is per-sandbox: a new session means a new one. The relay's landing
page (`GET /`) tries to display it automatically; DevTools is the fallback.

## Agent ops cheat-sheet (`agent_cli.py`)

| Command | Effect |
|---------|--------|
| `python3 agent_cli.py status` | who is connected |
| `python3 agent_cli.py ping` | client info + permissions |
| `python3 agent_cli.py list [path]` | directory listing |
| `python3 agent_cli.py read <path> [max_bytes]` | read a file (binary -> base64) |
| `printf TEXT \| python3 agent_cli.py write <path>` | write/overwrite (append: `--append`) |
| `base64 -w0 img.png \| python3 agent_cli.py write <path> --b64` | binary write |
| `python3 agent_cli.py mkdir <path>` / `delete <path> [--recursive]` | dirs (delete needs app toggle) |
| `python3 agent_cli.py move <src> <dst>` | rename/move |
| `python3 agent_cli.py glob <pattern> [path]` | find files |
| `python3 agent_cli.py grep <regex> [path] [--ignore-case]` | search contents |
| `python3 agent_cli.py batch '<json ops>'` | up to 25 ops per round trip (new app versions) |

Add `--nowait` to queue a command without waiting (useful when the client is
offline - commands deliver on reconnect). Env overrides: `BRIDGE_URL`,
`BRIDGE_TOKEN`.

## Gotchas

- **New session = new URL + token + traffic token.** The app's saved settings
  must be updated with all three (only the values change).
- **Ops are slow (~15-20 s round trip)** through the preview proxy - prefer
  `batch`, and queue independent work with `--nowait`.
- **Proxy flakiness:** if a result comes back `{"code": "internal"}` with a
  transport message, requeue once: `POST /cmd/<id>/requeue` (agent_cli does this
  automatically). The desktop app retries result delivery too (v1.1+).
- **Deletes are off by default** on the client; reads always work.
- **Never commit live tokens** - repo copies of the scripts use `REPLACE_ME` /
  env vars for a reason.

## Security model

- Folder jail: every path is realpath-resolved and must stay inside the shared
  folder (no `..`, absolute paths, or symlink escapes).
- Two secrets gate access: the session bridge token and the e2b traffic token.
- No shell/command execution - filesystem ops only.
- The operator can Disconnect or close the app at any time; every op is logged
  in the app's activity pane.

---

## Full source - `relay.py`

```python
#!/usr/bin/env python3
"""Arena Workspace Bridge — relay server.

Runs on the agent side (the Arena sandbox). A desktop client on your machine
connects OUT to this relay over HTTPS and long-polls for filesystem commands.
The agent submits commands and reads results over the same HTTP API.

Stdlib only — no dependencies.

Endpoints (all JSON):
  GET  /health                       -> {ok: true}                  (no auth)
  GET  /status                       -> queue/client summary        (token)
  POST /register   {client_id, workspace_label, platform, ...}     (token)
  GET  /poll?wait=25                 -> {command: {...}|null}       (token)
  POST /result     {id, ok, result|error}                          (token)
  POST /cmd        {op, params}     -> {id}                        (token)
  GET  /cmd/<id>?wait=25             -> {id, status, result|error}  (token)
  POST /cmd/<id>/requeue             -> {id, status}                (token)

Auth: header  X-Bridge-Token: <token>   (or ?token=... for quick curl tests).
"""

import hmac
import html
import json
import os
import secrets
import sys
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 64 * 1024 * 1024  # 64 MB (base64 writes)
DEFAULT_WAIT = 25.0
MAX_WAIT = 60.0


def log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


class State:
    def __init__(self):
        self.cv = threading.Condition()
        self.started = time.time()
        self.seq = 0
        self.commands = {}          # id -> command dict
        self.pending = deque()      # ids of commands waiting to be sent
        self.clients = {}           # client_id -> info dict
        self.last_poll = None       # time of last client /poll
        self.welcome_at = 0.0       # throttle for the auto welcome document

    def new_command(self, op, params):
        with self.cv:
            self.seq += 1
            cid = "c%06d" % self.seq
            cmd = {
                "id": cid,
                "op": op,
                "params": params or {},
                "status": "pending",   # pending -> sent -> done|error
                "created_at": time.time(),
                "sent_at": None,
                "done_at": None,
                "result": None,
                "error": None,
                "client_id": None,
            }
            self.commands[cid] = cmd
            self.pending.append(cid)
            self.cv.notify_all()
            return cid

    def finish(self, cid, ok, result, error):
        with self.cv:
            cmd = self.commands.get(cid)
            if not cmd:
                return None
            cmd["status"] = "done" if ok else "error"
            cmd["done_at"] = time.time()
            cmd["result"] = result if ok else None
            cmd["error"] = None if ok else (error if error is not None else "failed")
            self.cv.notify_all()
            return cmd

    def requeue(self, cid):
        with self.cv:
            cmd = self.commands.get(cid)
            if not cmd:
                return None
            # 'error' is eligible too: a flaky proxy can drop a good result and
            # replace it with a transport-error report — one re-run recovers it.
            if cmd["status"] in ("pending", "sent", "error"):
                cmd["status"] = "pending"
                cmd["sent_at"] = None
                cmd["done_at"] = None
                cmd["result"] = None
                cmd["error"] = None
                if cid not in self.pending:
                    self.pending.append(cid)
                self.cv.notify_all()
            return cmd

    def touch_client(self, client_id, info=None):
        with self.cv:
            c = self.clients.get(client_id) or {"client_id": client_id, "connected_at": time.time()}
            c["last_seen"] = time.time()
            if info:
                c.update(info)
            self.clients[client_id] = c
            self.cv.notify_all()

    def snapshot(self):
        with self.cv:
            counts = {"pending": 0, "sent": 0, "done": 0}
            for cmd in self.commands.values():
                counts[cmd["status"]] = counts.get(cmd["status"], 0) + 1
            return {
                "uptime_sec": round(time.time() - self.started, 1),
                "last_poll": self.last_poll,
                "clients": [
                    {
                        "client_id": c.get("client_id"),
                        "workspace_label": c.get("workspace_label"),
                        "platform": c.get("platform"),
                        "connected_at": c.get("connected_at"),
                        "last_seen": c.get("last_seen"),
                    }
                    for c in self.clients.values()
                ],
                "commands": counts,
                "total_commands": len(self.commands),
            }

    def public_view(self, cmd):
        """Command view safe to hand to a desktop client."""
        return {
            "id": cmd["id"],
            "op": cmd["op"],
            "params": cmd["params"],
            "created_at": cmd["created_at"],
        }


STATE = State()
TOKEN = os.environ.get("BRIDGE_TOKEN") or secrets.token_urlsafe(24)
TOKEN_GENERATED = not bool(os.environ.get("BRIDGE_TOKEN"))


# ---------------- welcome document (auto-sent on each fresh connect) ----------------

WELCOME_ART = """                                                              
                       .:--=+++=++=--:.                       
                  .-=+*######**++*******+=-:                  
              .:=***+=-:..   .:-:    .:-=+***+-.              
            :+*#*=:.       ::    ::.       :=*%#+:            
          :*##+:.         :#+.  .=:::::---:.  -*%@#-          
        .*%%+:         .....=:::-..:---==+*#+:  :*%%*.        
       =%%*:        . .:-++*+=+=+=-.:-=+****++=   -*##-       
      +##+.       . .-*#%%%%#***#**+-:-*##*+=-=-   .+**=      
     +#*=        ..-*%%@@%%%#####***+=:.:-===-:-.   .++*=     
    =**+       ...+%#%%##****+++*****++=:. .::::.    .++*:    
   .*++:      ...+#*-:....       .=***++=-: ..:-.     -+=+    
   =++=      ..:=#*: :+=.    :=:   ++**+=:-::..:       +=+:   
   +=+=-    ..:++*+  -%*.    +%+   =+**+:#++-...     .-+==-   
   =-+-*.   ..::+**:.             -+**+-.+-+-::..    -+=+-=   
   =-==     ..:.-**#****++=====+++***++- .::::..      :+-=-   
   -===      ..::+**##%%%#####*******+==:..:::..      :*-=:   
   .=-+-     ...::+**######********++==--:=-:...      *+==    
    -==*:     ....:+**####*******+++===-:--...       =*=+:    
     ===*:      ...:+*********++++====::--...       =#=+-     
      =+=*-       . =********+++===--::-:..        +#++-      
       -++**:       +++**++++===---::::..        :**++-       
        :+*##+:     -+=======----:::..         :+#*+=.        
          :+*##*-.   .:-----::::...         .-*##+=:          
            :=+***+-:                   .:=+##*+=.            
              .:-=+**++=-::..-==:..:--=+*##*+=:               
                  .:-==+++*+*=+++**###*++-:.                  
                       ..:::-------::..                       
                                                              """

WELCOME_TEMPLATE = """HELLO!
============================================
   Psycho's Local Arena Workspace
   your workspace is connected
============================================

                                                              
                       .:--=+++=++=--:.                       
                  .-=+*######**++*******+=-:                  
              .:=***+=-:..   .:-:    .:-=+***+-.              
            :+*#*=:.       ::    ::.       :=*%#+:            
          :*##+:.         :#+.  .=:::::---:.  -*%@#-          
        .*%%+:         .....=:::-..:---==+*#+:  :*%%*.        
       =%%*:        . .:-++*+=+=+=-.:-=+****++=   -*##-       
      +##+.       . .-*#%%%%#***#**+-:-*##*+=-=-   .+**=      
     +#*=        ..-*%%@@%%%#####***+=:.:-===-:-.   .++*=     
    =**+       ...+%#%%##****+++*****++=:. .::::.    .++*:    
   .*++:      ...+#*-:....       .=***++=-: ..:-.     -+=+    
   =++=      ..:=#*: :+=.    :=:   ++**+=:-::..:       +=+:   
   +=+=-    ..:++*+  -%*.    +%+   =+**+:#++-...     .-+==-   
   =-+-*.   ..::+**:.             -+**+-.+-+-::..    -+=+-=   
   =-==     ..:.-**#****++=====+++***++- .::::..      :+-=-   
   -===      ..::+**##%%%#####*******+==:..:::..      :*-=:   
   .=-+-     ...::+**######********++==--:=-:...      *+==    
    -==*:     ....:+**####*******+++===-:--...       =*=+:    
     ===*:      ...:+*********++++====::--...       =#=+-     
      =+=*-       . =********+++===--::-:..        +#++-      
       -++**:       +++**++++===---::::..        :**++-       
        :+*##+:     -+=======----:::..         :+#*+=.        
          :+*##*-.   .:-----::::...         .-*##+=:          
            :=+***+-:                   .:=+##*+=.            
              .:-=+**++=-::..-==:..:--=+*##*+=:               
                  .:-==+++*+*=+++**###*++-:.                  
                       ..:::-------::..                       
                                                              

Connected at : __TS__
Shared folder: __FOLDER__
Computer     : __PLATFORM__

The agent can now read and edit files in this folder.
This document is regenerated every time the connection is made.
============================================
"""


def welcome_text(info):
    return (WELCOME_TEMPLATE
            .replace("__TS__", time.strftime("%Y-%m-%d %H:%M:%S"))
            .replace("__FOLDER__", str(info.get("workspace_label") or "(shared folder)"))
            .replace("__PLATFORM__", str(info.get("platform") or "")))


def token_ok(presented):
    if not presented:
        return False
    return hmac.compare_digest(str(presented), TOKEN)


def find_traffic_token(headers):
    """Best-effort extraction of the e2b proxy token from an inbound request.

    The proxy authenticates the browser with `e2b-traffic-access-token`; if it
    forwards it (header or cookie) we can show it on the landing page so the
    user doesn't have to dig through DevTools. Returns (value, source) or (None, "").
    """
    for name in ("e2b-traffic-access-token", "x-e2b-traffic-access-token"):
        v = headers.get(name)
        if v:
            return v, "header:" + name
    raw = headers.get("Cookie") or ""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k in ("e2b-traffic-access-token", "x-e2b-traffic-access-token") and v:
            return v, "cookie:" + k
    for k, v in headers.items():
        if "traffic" in k.lower() and v:
            return v, "header:" + k
    return None, ""


def landing_page(headers):
    """HTML landing page for GET / — shows the relay is alive and (when the
    proxy exposes it) the traffic token the desktop app needs."""
    token, source = find_traffic_token(headers)
    all_headers = "".join(
        "<tr><td>%s</td><td>%s</td></tr>" % (html.escape(k), html.escape(v))
        for k, v in sorted(headers.items())
    )
    if token:
        token_block = """
      <h2>Your traffic token</h2>
      <p class="ok">The proxy passed it through — copy this into the
      <b>Traffic token</b> field in the desktop app:</p>
      <input id="tok" readonly value="%s">
      <p class="small">source: %s &nbsp;·&nbsp; click the box, then Ctrl+A, Ctrl+C</p>""" % (
            html.escape(token, quote=True), html.escape(source))
    else:
        token_block = """
      <h2>Your traffic token</h2>
      <p class="warn">The proxy didn't hand the token to me, so grab it from
      your browser instead:</p>
      <ol>
        <li>Stay on this page and press <b>F12</b> (Chrome / Edge).</li>
        <li>Open the <b>Network</b> tab, press <b>Ctrl+R</b> to refresh.</li>
        <li>Click the first request in the list.</li>
        <li>In <b>Headers → Request headers</b>, find
            <code>e2b-traffic-access-token</code> and copy its value.</li>
        <li>Not there? Check <b>Application → Cookies</b> (left sidebar) for a
            cookie with the same name and copy its value.</li>
      </ol>
      <p class="small">Then paste it into the <b>Traffic token</b> field in the
      desktop app and press Connect.</p>
      <details><summary>What I can see (for debugging)</summary>
        <p class="small">page URL: <code id="href">…</code><br>
        document.cookie: <code id="ck">…</code></p>
        <table><tr><th>header</th><th>value</th></tr>%s</table>
      </details>""" % all_headers
    return """<!doctype html><html><head><meta charset="utf-8">
<title>Psycho's Local Arena Workspace — relay</title><style>
  body { font-family: "Segoe UI", sans-serif; background:#14161a; color:#e8e8e8; margin:0; padding:2rem; }
  .card { max-width:44rem; margin:auto; background:#1d2026; border:1px solid #2c313a;
          border-radius:12px; padding:1.6rem 2.2rem; }
  h1 { font-size:1.3rem; margin-top:0; } h2 { font-size:1.05rem; margin-bottom:.4rem; }
  .ok { color:#6fdc8c; } .warn { color:#ffcf6e; } .small { color:#9aa3af; font-size:.85rem; }
  code, input, table { font-family: Consolas, monospace; }
  input { width:100%%; box-sizing:border-box; padding:.65rem; border-radius:8px;
          border:1px solid #3a4150; background:#14161a; color:#9f9; font-size:1rem; }
  table { width:100%%; border-collapse:collapse; font-size:.8rem; }
  td { padding:.25rem .5rem; border-top:1px solid #2c313a; word-break:break-all; }
  td:first-child { color:#8fb8ff; white-space:nowrap; }
  details { margin-top:1rem; } ol { line-height:1.7; }
</style></head><body><div class="card">
  <h1>🌉 Psycho's Local Arena Workspace — relay <span class="ok">● running</span></h1>
  <p class="small">This relay is waiting for your Windows app to connect.
  You can close this page once you've copied the token.</p>
  %s
</div><script>
  document.getElementById("href") && (document.getElementById("href").textContent = location.href);
  document.getElementById("ck") && (document.getElementById("ck").textContent = document.cookie || "(none visible)");
</script></body></html>""" % token_block


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PsychoWorkspaceRelay/1.0"

    # ---------- plumbing ----------

    def log_message(self, fmt, *args):
        log("%s - %s" % (self.address_string(), fmt % args))

    def _url(self):
        return urlsplit(self.path)

    def _query(self):
        return parse_qs(self._url().query)

    def _q(self, name, default=None):
        vals = self._query().get(name)
        return vals[0] if vals else default

    def _authed(self):
        presented = self.headers.get("X-Bridge-Token") or self._q("token")
        return token_ok(presented)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # client hung up on a long poll — fine

    def _deny(self):
        self._send_json({"error": "unauthorized"}, 401)

    def _send_html(self, page, status=200):
        data = page.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Bridge-Token")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- routing ----------

    def do_GET(self):
        path = self._url().path
        try:
            if path == "/":
                # human-facing landing page (also the live preview target)
                self._send_html(landing_page(self.headers))
            elif path == "/whoami":
                # echo of what the proxy sent us — no secrets beyond the
                # requester's own request headers
                token, source = find_traffic_token(self.headers)
                self._send_json({
                    "traffic_token": token,
                    "traffic_token_source": source,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "remote": self.client_address[0] if self.client_address else None,
                })
            elif path == "/health":
                self._send_json({"ok": True, "service": "arena-bridge-relay"})
            elif path == "/status":
                if not self._authed():
                    return self._deny()
                self._send_json(STATE.snapshot())
            elif path == "/poll":
                if not self._authed():
                    return self._deny()
                self._handle_poll()
            elif path.startswith("/cmd/"):
                if not self._authed():
                    return self._deny()
                parts = path.strip("/").split("/")
                if len(parts) == 2:
                    self._handle_cmd_get(parts[1])
                else:
                    self._send_json({"error": "not found"}, 404)
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception:
            log(traceback.format_exc())
            self._send_json({"error": "internal"}, 500)

    def do_POST(self):
        path = self._url().path
        try:
            if not self._authed():
                return self._deny()
            body = self._read_body()

            if path == "/register":
                client_id = str(body.get("client_id") or "default")
                STATE.touch_client(
                    client_id,
                    {
                        "workspace_label": body.get("workspace_label"),
                        "platform": body.get("platform"),
                        "app_version": body.get("app_version"),
                    },
                )
                log("client registered: %s (%s)" % (client_id, body.get("workspace_label")))
                now = time.time()
                fresh = STATE.last_poll is None or (now - (STATE.last_poll or 0.0)) > 45.0
                if fresh and (now - STATE.welcome_at) > 30.0:
                    STATE.welcome_at = now
                    wid = STATE.new_command("write", {"path": "HELLO.txt", "content": welcome_text(body)})
                    log("welcome document queued as %s" % wid)
                self._send_json({"ok": True, "server_time": time.time()})

            elif path == "/poll":
                self._handle_poll(body)

            elif path == "/result":
                cid = str(body.get("id") or "")
                ok = bool(body.get("ok"))
                cmd = STATE.finish(cid, ok, body.get("result"), body.get("error"))
                if not cmd:
                    return self._send_json({"error": "unknown command id"}, 404)
                self._send_json({"ok": True, "status": cmd["status"]})

            elif path == "/cmd":
                op = str(body.get("op") or "").strip()
                if not op:
                    return self._send_json({"error": "missing op"}, 400)
                cid = STATE.new_command(op, body.get("params"))
                self._send_json({"ok": True, "id": cid}, 201)

            elif path.startswith("/cmd/") and path.endswith("/requeue"):
                cid = path.strip("/").split("/")[1]
                cmd = STATE.requeue(cid)
                if not cmd:
                    return self._send_json({"error": "unknown command id"}, 404)
                self._send_json({"ok": True, "id": cmd["id"], "status": cmd["status"]})

            else:
                self._send_json({"error": "not found"}, 404)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
        except Exception:
            log(traceback.format_exc())
            self._send_json({"error": "internal"}, 500)

    # ---------- long-poll handlers ----------

    def _handle_poll(self, body=None):
        STATE.last_poll = time.time()
        try:
            wait = float((body or {}).get("wait", self._q("wait", DEFAULT_WAIT)))
        except (TypeError, ValueError):
            wait = DEFAULT_WAIT
        wait = max(0.0, min(wait, MAX_WAIT))

        deadline = time.time() + wait
        cmd = None
        while True:
            with STATE.cv:
                while STATE.pending:
                    cid = STATE.pending.popleft()
                    cand = STATE.commands.get(cid)
                    if cand and cand["status"] == "pending":
                        cand["status"] = "sent"
                        cand["sent_at"] = time.time()
                        cmd = cand
                        break
                if cmd is None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    STATE.cv.wait(remaining)
                    continue
            break

        if cmd is not None:
            self._send_json({"command": STATE.public_view(cmd)})
        else:
            self._send_json({"command": None})

    def _handle_cmd_get(self, cid):
        try:
            wait = float(self._q("wait", DEFAULT_WAIT))
        except (TypeError, ValueError):
            wait = DEFAULT_WAIT
        wait = max(0.0, min(wait, MAX_WAIT))

        deadline = time.time() + wait
        with STATE.cv:
            cmd = STATE.commands.get(cid)
            if cmd is None:
                self._send_json({"error": "unknown command id"}, 404)
                return
            while cmd["status"] not in ("done", "error"):
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                STATE.cv.wait(remaining)

            view = {
                "id": cmd["id"],
                "op": cmd["op"],
                "status": cmd["status"],
                "result": cmd["result"],
                "error": cmd["error"],
                "created_at": cmd["created_at"],
                "done_at": cmd["done_at"],
            }
        self._send_json(view)


def main():
    global TOKEN, TOKEN_GENERATED
    import argparse

    ap = argparse.ArgumentParser(description="Arena Workspace Bridge relay")
    ap.add_argument("--port", type=int, default=int(os.environ.get("BRIDGE_PORT", 8765)))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--token", default=None, help="shared secret (default: $BRIDGE_TOKEN or generated)")
    args = ap.parse_args()

    if args.token:
        TOKEN = args.token
        TOKEN_GENERATED = False

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True

    log("=" * 62)
    log("Psycho's Local Arena Workspace relay listening on %s:%d" % (args.host, args.port))
    log("Token: %s%s" % (TOKEN, "   (auto-generated)" if TOKEN_GENERATED else ""))
    log("Endpoints: /health /status /register /poll /result /cmd")
    log("=" * 62)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
```

## Full source - `agent_cli.py`

```python
#!/usr/bin/env python3
"""Agent-side CLI for the Arena Workspace Bridge.

Usage (from the agent sandbox):
  python3 agent_cli.py ping
  python3 agent_cli.py list [path]
  python3 agent_cli.py read <path> [max_bytes]
  python3 agent_cli.py write <path> [--append]          (content on stdin)
  python3 agent_cli.py mkdir <path>
  python3 agent_cli.py delete <path> [--recursive]
  python3 agent_cli.py move <src> <dst>
  python3 agent_cli.py glob <pattern> [path]
  python3 agent_cli.py grep <regex> [path] [--ignore-case] [--max-hits N]
  python3 agent_cli.py stat <path>
  python3 agent_cli.py status
"""

import json
import os
import sys
import time
import urllib.request

RELAY = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8765")
TOKEN = os.environ.get("BRIDGE_TOKEN", "REPLACE_ME")


def api(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(RELAY + path, data=data, method=method)
    req.add_header("X-Bridge-Token", TOKEN)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def run(op, params, wait=60, retries=2):
    cid = api("POST", "/cmd", {"op": op, "params": params})["id"]
    res = {"status": "pending"}
    for attempt in range(retries + 1):
        deadline = time.time() + wait
        while time.time() < deadline:
            res = api("GET", "/cmd/%s?wait=%d" % (cid, min(25, wait)))
            if res["status"] in ("done", "error"):
                break
        st = res.get("status")
        if st == "done":
            return res
        if st == "pending":
            return res   # client offline — retrying is pointless
        err = res.get("error") or {}
        if attempt < retries and err.get("code") == "internal":
            api("POST", "/cmd/%s/requeue" % cid)   # transport hiccup — one re-run
            time.sleep(0.5)
            continue
        return res
    return res


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    argv = sys.argv[1:]
    nowait = "--nowait" in argv
    argv = [a for a in argv if a != "--nowait"]
    cmd = argv[0]
    args = argv[1:]

    if cmd == "status":
        print(json.dumps(api("GET", "/status"), indent=2))
        return 0

    params = {}
    if cmd == "ping":
        pass
    elif cmd == "list":
        params["path"] = args[0] if args else ""
    elif cmd == "stat":
        params["path"] = args[0]
    elif cmd == "read":
        params["path"] = args[0]
        if len(args) > 1:
            params["max_bytes"] = int(args[1])
    elif cmd == "write":
        params["path"] = args[0]
        raw = sys.stdin.buffer.read()
        if "--b64" in args:
            params["content_b64"] = raw.decode("ascii").strip()
        else:
            params["content"] = raw.decode("utf-8")
        params["append"] = "--append" in args
    elif cmd == "mkdir":
        params["path"] = args[0]
    elif cmd == "delete":
        params["path"] = args[0]
        params["recursive"] = "--recursive" in args
    elif cmd == "move":
        params["src"], params["dst"] = args[0], args[1]
    elif cmd == "glob":
        params["pattern"] = args[0]
        if len(args) > 1:
            params["path"] = args[1]
    elif cmd == "grep":
        params["pattern"] = args[0]
        if len(args) > 1 and not args[1].startswith("--"):
            params["path"] = args[1]
        params["ignore_case"] = "--ignore-case" in args
        if "--max-hits" in args:
            params["max_hits"] = int(args[args.index("--max-hits") + 1])
    elif cmd == "batch":
        params["ops"] = json.loads(args[0] if args else sys.stdin.read())
    else:
        print("unknown command: %s" % cmd)
        return 1

    if nowait:
        cid = api("POST", "/cmd", {"op": cmd, "params": params})["id"]
        print(json.dumps({"queued": True, "id": cid, "op": cmd}))
        return 0

    res = run(cmd, params)
    print(json.dumps(res, indent=2))
    return 0 if res["status"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
```
