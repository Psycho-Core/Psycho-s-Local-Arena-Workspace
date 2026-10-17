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
