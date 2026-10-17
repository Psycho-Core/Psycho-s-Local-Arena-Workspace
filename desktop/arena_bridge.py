#!/usr/bin/env python3
"""Psycho's Local Arena Workspace — Windows desktop client (Arena Workspace Bridge).

Connects OUT to the Arena bridge relay, exposes one folder you choose, and
lets the Arena agent (working in its chat session) browse, read, search and
edit files in that folder.

Safety model:
  * Only the folder you pick is reachable (realpath jail, no escapes).
  * Reads are always allowed; writes/deletes are toggles you control.
  * NO shell/command execution — filesystem operations only.
  * Shared random token authenticates every request.

Stdlib only. Requires Python 3.8+ (python.org installer or `winget install Python.Python.3.12`).

Usage:
  GUI:        python arena_bridge.py
  Headless:   python arena_bridge.py --relay https://... --token XXX --root C:\\work\\proj
"""

import argparse
import base64
import fnmatch
import json
import os
import platform
import queue
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

APP_VERSION = "1.0.0"
CLIENT_ID = "%s-%s" % (socket.gethostname(), uuid.uuid4().hex[:8])
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".arena_bridge_config.json")

READ_CAP = 10 * 1024 * 1024        # 10 MB per read
GREP_FILE_CAP = 2 * 1024 * 1024    # skip files larger than this when grepping
GREP_HIT_CAP = 500
GLOB_CAP = 5000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
             ".cache", ".idea", ".vs"}


class OpError(Exception):
    def __init__(self, message, code="error"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- config

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- core

class BridgeCore:
    """Long-poll client + filesystem op dispatcher. GUI-free, thread-safe."""

    def __init__(self, relay_url, token, root,
                 allow_write=True, allow_delete=False, traffic_token=None,
                 on_log=None, on_status=None):
        self.relay_url = relay_url.rstrip("/")
        self.token = token
        self.traffic_token = traffic_token  # e2b preview-proxy token, if any
        self.root = os.path.realpath(os.path.expanduser(root))
        self.allow_write = allow_write
        self.allow_delete = allow_delete
        self.on_log = on_log or (lambda s: None)
        self.on_status = on_status or (lambda s: None)
        self.stop_event = threading.Event()
        self.thread = None

    # ---- lifecycle ----

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name="bridge-loop")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.on_status("Disconnected")

    # ---- http ----

    def _http(self, method, path, body=None, timeout=45):
        url = self.relay_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Bridge-Token", self.token)
        req.add_header("Connection", "close")
        if self.traffic_token:
            req.add_header("e2b-traffic-access-token", self.traffic_token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ---- main loop ----

    def _run(self):
        backoff = 1.0
        registered = False
        self.on_status("Connecting…")
        while not self.stop_event.is_set():
            try:
                if not registered:
                    self._register()
                    registered = True
                resp = self._http("GET", "/poll?wait=15", timeout=40)
                backoff = 1.0
                self.on_status("Connected")
                cmd = resp.get("command")
                if not cmd:
                    continue
                self._handle(cmd)
            except Exception as e:
                if self.stop_event.is_set():
                    break
                registered = False
                self.on_status("Reconnecting…")
                self.on_log("connection problem: %s" % self._explain(e))
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 30.0)
        self.on_status("Disconnected")

    def _explain(self, e):
        """Turn transport errors into actionable log lines."""
        if isinstance(e, urllib.error.HTTPError):
            detail = ""
            try:
                detail = " — %s" % e.read().decode("utf-8", "replace").strip()[:300]
            except Exception:
                pass
            if e.code == 403:
                return ("HTTP 403: the proxy rejected us. Check the Traffic token field "
                        "(copy 'e2b-traffic-access-token' from your browser DevTools)%s" % detail)
            return "HTTP %s %s%s" % (e.code, e.reason, detail)
        return str(e)

    def _register(self):
        info = {
            "client_id": CLIENT_ID,
            "workspace_label": self.root,
            "platform": "%s %s (%s)" % (platform.system(), platform.release(), platform.machine()),
            "app_version": APP_VERSION,
        }
        self._http("POST", "/register", info, timeout=15)
        self.on_log("registered with relay as %s" % CLIENT_ID)

    def _handle(self, cmd):
        cid, op = cmd.get("id"), cmd.get("op", "?")
        params = cmd.get("params") or {}
        t0 = time.time()
        try:
            result = self.dispatch(op, params)
            payload = {"id": cid, "ok": True, "result": result}
            note = "✓ %s %s (%.2fs)" % (op, _brief(params), time.time() - t0)
        except OpError as e:
            payload = {"id": cid, "ok": False, "error": {"code": e.code, "message": str(e)}}
            note = "✗ %s %s → %s" % (op, _brief(params), e)
        except Exception as e:
            payload = {"id": cid, "ok": False, "error": {"code": "internal", "message": str(e)}}
            note = "✗ %s %s → internal error: %s" % (op, _brief(params), e)
        if not self._post_result(payload):
            note += "  (result delivery failed — agent can requeue)"
        self.on_log(note)

    def _post_result(self, payload, attempts=4):
        """Deliver a command result, retrying through transient proxy drops."""
        for i in range(attempts):
            try:
                self._http("POST", "/result", payload, timeout=30)
                return True
            except Exception as e:
                if i == attempts - 1:
                    self.on_log("result delivery failed: %s" % self._explain(e))
                    return False
                self.stop_event.wait(1.0 + i)
        return False

    # ---- path safety ----

    def safe_path(self, rel):
        rel = (rel or "").strip().replace("\\", "/")
        if rel in ("", "."):
            return self.root
        if rel.startswith("/") or rel.startswith("~") or (len(rel) > 1 and rel[1] == ":"):
            raise OpError("absolute paths are not allowed: %r" % rel, "bad_path")
        full = os.path.realpath(os.path.join(self.root, rel))
        root_cmp = os.path.normcase(self.root)
        full_cmp = os.path.normcase(full)
        if full_cmp != root_cmp and not full_cmp.startswith(root_cmp + os.sep):
            raise OpError("path escapes the shared folder: %r" % rel, "bad_path")
        return full

    # ---- op dispatch ----

    def dispatch(self, op, params):
        handlers = {
            "ping": self.op_ping,
            "list": self.op_list,
            "stat": self.op_stat,
            "read": self.op_read,
            "write": self.op_write,
            "mkdir": self.op_mkdir,
            "delete": self.op_delete,
            "move": self.op_move,
            "glob": self.op_glob,
            "grep": self.op_grep,
            "batch": self.op_batch,
        }
        fn = handlers.get(op)
        if not fn:
            raise OpError("unknown op: %r" % op, "unknown_op")
        try:
            return fn(params)
        except OpError:
            raise
        except OSError as e:
            raise OpError("%s: %s" % (e.strerror or type(e).__name__, e.filename or ""), "os_error")

    def op_batch(self, params):
        """Run several ops in one round trip (the proxy path is slow per call)."""
        ops = params.get("ops") or []
        if len(ops) > 25:
            raise OpError("batch too large (max 25 ops)", "bad_request")
        out = []
        for i, item in enumerate(ops):
            try:
                r = {"ok": True, "result": self.dispatch(item.get("op"), item.get("params") or {})}
            except OpError as e:
                r = {"ok": False, "error": {"code": e.code, "message": str(e)}}
            except Exception as e:
                r = {"ok": False, "error": {"code": "internal", "message": str(e)}}
            out.append(r)
        return {"results": out}

    def op_ping(self, params):
        return {
            "ok": True,
            "app_version": APP_VERSION,
            "client_id": CLIENT_ID,
            "root": self.root,
            "hostname": socket.gethostname(),
            "user": os.environ.get("USERNAME") or os.environ.get("USER") or "",
            "platform": "%s %s (%s)" % (platform.system(), platform.release(), platform.machine()),
            "allow_write": self.allow_write,
            "allow_delete": self.allow_delete,
            "server_time": time.time(),
        }

    def op_list(self, params):
        target = self.safe_path(params.get("path", ""))
        if not os.path.isdir(target):
            raise OpError("not a directory: %s" % params.get("path"), "not_found")
        entries = []
        with os.scandir(target) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    is_dir = e.is_dir(follow_symlinks=False)
                    is_link = e.is_symlink()
                    entries.append({
                        "name": e.name,
                        "type": "dir" if is_dir else ("link" if is_link else "file"),
                        "size": st.st_size if not is_dir else None,
                        "mtime": st.st_mtime,
                    })
                except OSError:
                    entries.append({"name": e.name, "type": "unknown"})
        entries.sort(key=lambda x: (x.get("type") != "dir", x["name"].lower()))
        return {"path": params.get("path", ""), "entries": entries}

    def op_stat(self, params):
        target = self.safe_path(params.get("path", ""))
        if not os.path.exists(target) and not os.path.islink(target):
            raise OpError("not found: %s" % params.get("path"), "not_found")
        st = os.stat(target)
        return {
            "path": params.get("path", ""),
            "is_dir": os.path.isdir(target),
            "size": st.st_size,
            "mtime": st.st_mtime,
            "mode": oct(st.st_mode),
        }

    def op_read(self, params):
        target = self.safe_path(params.get("path", ""))
        if not os.path.isfile(target):
            raise OpError("not a file: %s" % params.get("path"), "not_found")
        size = os.path.getsize(target)
        max_bytes = int(params.get("max_bytes") or READ_CAP)
        max_bytes = min(max_bytes, READ_CAP)
        with open(target, "rb") as f:
            data = f.read(max_bytes)
        if b"\x00" in data[:8192]:
            content, encoding = base64.b64encode(data).decode("ascii"), "base64"
        else:
            content, encoding = data.decode("utf-8", errors="replace"), "utf-8"
        return {
            "path": params.get("path", ""),
            "size": size,
            "truncated": size > len(data),
            "encoding": encoding,
            "content": content,
        }

    def op_write(self, params):
        if not self.allow_write:
            raise OpError("writes are disabled on this client", "forbidden")
        target = self.safe_path(params.get("path", ""))
        if params.get("content_b64") is not None:
            try:
                data = base64.b64decode(params["content_b64"])
            except Exception:
                raise OpError("invalid base64 content", "bad_request")
        elif params.get("content") is not None:
            data = str(params["content"]).encode("utf-8")
        else:
            raise OpError("missing content", "bad_request")
        parent = os.path.dirname(target)
        if params.get("mkdirs", True) and parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        if os.path.isdir(target):
            raise OpError("target is a directory", "bad_request")
        mode = "ab" if params.get("append") else "wb"
        with open(target, mode) as f:
            f.write(data)
        return {"path": params.get("path", ""), "bytes": len(data), "appended": bool(params.get("append"))}

    def op_mkdir(self, params):
        if not self.allow_write:
            raise OpError("writes are disabled on this client", "forbidden")
        target = self.safe_path(params.get("path", ""))
        os.makedirs(target, exist_ok=True)
        return {"path": params.get("path", ""), "created": True}

    def op_delete(self, params):
        if not self.allow_delete:
            raise OpError("deletes are disabled on this client", "forbidden")
        target = self.safe_path(params.get("path", ""))
        if target == self.root:
            raise OpError("refusing to delete the shared folder itself", "forbidden")
        if os.path.islink(target) or os.path.isfile(target):
            os.remove(target)
        elif os.path.isdir(target):
            if params.get("recursive"):
                import shutil
                shutil.rmtree(target)
            else:
                os.rmdir(target)  # fails if non-empty — on purpose
        else:
            raise OpError("not found: %s" % params.get("path"), "not_found")
        return {"path": params.get("path", ""), "deleted": True}

    def op_move(self, params):
        if not self.allow_write:
            raise OpError("writes are disabled on this client", "forbidden")
        src = self.safe_path(params.get("src", ""))
        dst = self.safe_path(params.get("dst", ""))
        if not os.path.exists(src):
            raise OpError("source not found", "not_found")
        os.rename(src, dst)
        return {"src": params.get("src"), "dst": params.get("dst"), "moved": True}

    def op_glob(self, params):
        pattern = params.get("pattern") or "*"
        base_rel = params.get("path", "")
        base = self.safe_path(base_rel)
        if not os.path.isdir(base):
            raise OpError("not a directory: %r" % base_rel, "not_found")
        hits = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            rel_dir = os.path.relpath(dirpath, self.root).replace(os.sep, "/")
            rel_dir = "" if rel_dir == "." else rel_dir + "/"
            for name in filenames + dirnames:
                rel = rel_dir + name
                if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
                    hits.append(rel)
                    if len(hits) >= GLOB_CAP:
                        return {"pattern": pattern, "matches": hits, "truncated": True}
        return {"pattern": pattern, "matches": hits, "truncated": False}

    def op_grep(self, params):
        pattern = params.get("pattern") or ""
        if not pattern:
            raise OpError("missing pattern", "bad_request")
        flags = re.IGNORECASE if params.get("ignore_case") else 0
        try:
            rx = re.compile(pattern, flags)
        except re.error as e:
            raise OpError("bad regex: %s" % e, "bad_request")
        base_rel = params.get("path", "")
        base = self.safe_path(base_rel)
        if not os.path.isdir(base) and not os.path.isfile(base):
            raise OpError("path not found: %r" % base_rel, "not_found")
        max_hits = min(int(params.get("max_hits") or GREP_HIT_CAP), GREP_HIT_CAP)
        include_all = bool(params.get("include_all"))
        hits = []
        scanned = 0

        def scan_file(full, rel):
            nonlocal scanned
            try:
                if os.path.getsize(full) > GREP_FILE_CAP:
                    return
                with open(full, "rb") as f:
                    sample = f.read(8192)
                    if b"\x00" in sample:
                        return
                    f.seek(0)
                    text = f.read(GREP_FILE_CAP).decode("utf-8", errors="replace")
            except OSError:
                return
            scanned += 1
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append("%s:%d:%s" % (rel, i, line[:400]))
                    if len(hits) >= max_hits:
                        return

        if os.path.isfile(base):
            rel = os.path.relpath(base, self.root).replace(os.sep, "/")
            scan_file(base, rel)
        else:
            for dirpath, dirnames, filenames in os.walk(base):
                if not include_all:
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for name in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, name), self.root).replace(os.sep, "/")
                    scan_file(os.path.join(dirpath, name), rel)
                    if len(hits) >= max_hits:
                        return {"pattern": pattern, "hits": hits, "files_scanned": scanned, "truncated": True}
        return {"pattern": pattern, "hits": hits, "files_scanned": scanned, "truncated": len(hits) >= max_hits}


def _brief(params):
    p = params.get("path") or params.get("src") or params.get("pattern") or ""
    return (str(p)[:60])


# ---------------------------------------------------------------- GUI

def run_gui(initial_cfg):
    try:
        import tkinter as tk
        from tkinter import filedialog, ttk
    except ImportError:
        print("tkinter is not available. Install Python from python.org, or run headless:\n"
              "  python arena_bridge.py --relay URL --token TOKEN --root FOLDER", file=sys.stderr)
        sys.exit(1)

    cfg = dict(initial_cfg)
    core_holder = {"core": None}
    ui_queue = queue.Queue()

    root = tk.Tk()
    root.title("Psycho's Local Arena Workspace")
    root.geometry("680x620")
    root.minsize(560, 480)

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)

    # --- connection fields
    ttk.Label(frm, text="Relay URL").grid(row=0, column=0, sticky="w")
    relay_var = tk.StringVar(value=cfg.get("relay", ""))
    ttk.Entry(frm, textvariable=relay_var, width=64).grid(row=0, column=1, columnspan=2, sticky="we", pady=2)

    ttk.Label(frm, text="Token").grid(row=1, column=0, sticky="w")
    token_var = tk.StringVar(value=cfg.get("token", ""))
    ttk.Entry(frm, textvariable=token_var, width=64, show="•").grid(row=1, column=1, columnspan=2, sticky="we", pady=2)

    ttk.Label(frm, text="Traffic token").grid(row=2, column=0, sticky="w")
    traffic_var = tk.StringVar(value=cfg.get("traffic_token", ""))
    ttk.Entry(frm, textvariable=traffic_var, width=64, show="•").grid(row=2, column=1, columnspan=2, sticky="we", pady=2)

    ttk.Label(frm, text="Shared folder").grid(row=3, column=0, sticky="w")
    root_var = tk.StringVar(value=cfg.get("root", ""))
    ttk.Entry(frm, textvariable=root_var, width=52).grid(row=3, column=1, sticky="we", pady=2)

    def browse():
        d = filedialog.askdirectory(initialdir=root_var.get() or os.path.expanduser("~"))
        if d:
            root_var.set(d)

    ttk.Button(frm, text="Browse…", command=browse).grid(row=3, column=2, sticky="e", padx=(6, 0))

    # --- permissions
    perm = ttk.LabelFrame(frm, text="Permissions", padding=8)
    perm.grid(row=4, column=0, columnspan=3, sticky="we", pady=8)
    write_var = tk.BooleanVar(value=cfg.get("allow_write", True))
    delete_var = tk.BooleanVar(value=cfg.get("allow_delete", False))
    ttk.Checkbutton(perm, text="Allow writes (create / edit / rename files)",
                    variable=write_var).grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(perm, text="Allow deletes (off = read & edit only)",
                    variable=delete_var).grid(row=1, column=0, sticky="w")
    ttk.Label(perm, text="Reads are always allowed. Shell/command execution is never allowed.",
              foreground="#666").grid(row=2, column=0, sticky="w", pady=(4, 0))

    # --- buttons + status
    btns = ttk.Frame(frm)
    btns.grid(row=5, column=0, columnspan=3, sticky="we")
    status_var = tk.StringVar(value="Disconnected")
    status_lbl = ttk.Label(btns, textvariable=status_var, foreground="#b00")
    status_lbl.pack(side="left")
    btn_connect = ttk.Button(btns, text="Connect")
    btn_connect.pack(side="right", padx=4)
    btn_disconnect = ttk.Button(btns, text="Disconnect", state="disabled")
    btn_disconnect.pack(side="right", padx=4)

    # --- log
    ttk.Label(frm, text="Activity").grid(row=6, column=0, sticky="w", pady=(8, 0))
    log_txt = tk.Text(frm, height=18, wrap="word", state="disabled",
                      background="#111", foreground="#9f9", font=("Consolas", 9))
    log_txt.grid(row=7, column=0, columnspan=3, sticky="nsew", pady=4)

    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(7, weight=1)

    def ui_log(msg):
        ui_queue.put(("log", msg))

    def ui_status(msg):
        ui_queue.put(("status", msg))

    def drain_queue():
        try:
            while True:
                kind, msg = ui_queue.get_nowait()
                if kind == "log":
                    log_txt.configure(state="normal")
                    log_txt.insert("end", "[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
                    log_txt.see("end")
                    log_txt.configure(state="disabled")
                else:
                    status_var.set(msg)
                    color = {"Connected": "#080", "Disconnected": "#b00"}.get(msg, "#c60")
                    status_lbl.configure(foreground=color)
                    connected = msg == "Connected"
                    btn_connect.configure(state="disabled" if connected or msg in ("Connecting…", "Reconnecting…") else "normal")
                    btn_disconnect.configure(state="normal" if msg not in ("Disconnected",) else "disabled")
        except queue.Empty:
            pass
        root.after(120, drain_queue)

    def do_connect():
        relay = relay_var.get().strip()
        token = token_var.get().strip()
        traffic = traffic_var.get().strip()
        folder = root_var.get().strip()
        if not relay.startswith(("http://", "https://")):
            ui_log("Relay URL must start with https:// (or http:// for testing)")
            return
        if not token:
            ui_log("Token is required — copy it from the agent")
            return
        if not os.path.isdir(folder):
            ui_log("Pick an existing folder to share")
            return
        cfg.update({
            "relay": relay, "token": token, "traffic_token": traffic, "root": folder,
            "allow_write": write_var.get(), "allow_delete": delete_var.get(),
        })
        save_config(cfg)
        prev = core_holder.get("core")
        if prev:
            prev.stop()
        core = BridgeCore(relay, token, folder,
                          allow_write=write_var.get(), allow_delete=delete_var.get(),
                          traffic_token=traffic or None,
                          on_log=ui_log, on_status=ui_status)
        core_holder["core"] = core
        core.start()
        ui_log("sharing folder: %s" % core.root)

    def do_disconnect():
        core = core_holder.get("core")
        if core:
            core.stop()
        ui_status("Disconnected")

    btn_connect.configure(command=do_connect)
    btn_disconnect.configure(command=do_disconnect)

    def on_close():
        core = core_holder.get("core")
        if core:
            core.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    ui_log("Psycho's Local Arena Workspace v%s — paste the relay URL and token the agent gives you," % APP_VERSION)
    ui_log("pick a folder, then Connect. You can Disconnect or close the window at any time.")
    drain_queue()
    root.mainloop()


# ---------------------------------------------------------------- headless

def run_headless(args):
    def log(s):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), s), flush=True)

    core = BridgeCore(args.relay, args.token, args.root,
                      allow_write=not args.read_only,
                      allow_delete=args.allow_delete,
                      traffic_token=args.e2b_token,
                      on_log=log, on_status=lambda s: log("status: %s" % s))
    log("sharing folder: %s" % core.root)
    log("writes=%s deletes=%s" % (core.allow_write, core.allow_delete))
    core.start()
    try:
        while core.thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        core.stop()
        log("stopped")


# ---------------------------------------------------------------- entry

def main():
    ap = argparse.ArgumentParser(description="Arena Workspace Bridge (desktop client)")
    ap.add_argument("--relay", help="relay base URL, e.g. https://8765-xxxx.e2b.app")
    ap.add_argument("--token", help="shared bridge token")
    ap.add_argument("--root", help="folder to share (headless mode)")
    ap.add_argument("--read-only", action="store_true", help="disable writes")
    ap.add_argument("--allow-delete", action="store_true", help="enable deletes")
    ap.add_argument("--e2b-token", help="traffic token for the e2b preview proxy (e2b-traffic-access-token)")
    ap.add_argument("--gui", action="store_true", help="force GUI even with args")
    args = ap.parse_args()

    if args.relay and args.root and not args.gui:
        if not args.token:
            print("--token is required in headless mode", file=sys.stderr)
            sys.exit(2)
        run_headless(args)
    else:
        cfg = load_config()
        if args.relay:
            cfg["relay"] = args.relay
        if args.token:
            cfg["token"] = args.token
        if args.root:
            cfg["root"] = args.root
        if args.e2b_token:
            cfg["traffic_token"] = args.e2b_token
        run_gui(cfg)


if __name__ == "__main__":
    main()
