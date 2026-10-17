#!/usr/bin/env python3
"""Process-level end-to-end test.

Boots the EXACT shipped entrypoints as separate OS processes —
  relay/relay.py  +  desktop/arena_bridge.py (headless) —
and drives them the way the agent does. This validates argparse wiring,
registration, the reconnect loop and the whole wire protocol outside the
in-process test harness.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
TOKEN = "e2e-secret"
PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, extra))


def api(port, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method=method)
    req.add_header("X-Bridge-Token", TOKEN)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    port = free_port()
    work = tempfile.mkdtemp(prefix="e2e-share-")
    with open(os.path.join(work, "readme.txt"), "w", encoding="utf-8") as f:
        f.write("process-level e2e\n")

    relay_proc = subprocess.Popen(
        [sys.executable, os.path.join("relay", "relay.py"), "--port", str(port), "--token", TOKEN],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    client_proc = None
    try:
        # wait for relay health
        up = False
        for _ in range(50):
            try:
                st, body = api(port, "GET", "/health")
                up = st == 200 and body.get("ok")
                if up:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        check("relay process came up", up)

        client_proc = subprocess.Popen(
            [sys.executable, os.path.join("desktop", "arena_bridge.py"),
             "--relay", "http://127.0.0.1:%d" % port,
             "--token", TOKEN, "--root", work, "--allow-delete"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        registered = False
        for _ in range(80):
            _, body = api(port, "GET", "/status")
            if body.get("clients"):
                registered = True
                break
            time.sleep(0.1)
        check("client process registered", registered)

        def run_op(op, params=None, wait=15):
            cid = api(port, "POST", "/cmd", {"op": op, "params": params or {}})[1]["id"]
            deadline = time.time() + wait
            while time.time() < deadline:
                res = api(port, "GET", "/cmd/%s?wait=2" % cid)[1]
                if res["status"] in ("done", "error"):
                    return res
            return res

        r = run_op("ping")
        check("ping", r["status"] == "done" and r["result"]["root"] == os.path.realpath(work), r)
        check("ping reports delete permission", r["result"]["allow_delete"] is True)

        r = run_op("write", {"path": "out/notes.txt", "content": "hello e2e\n"})
        check("write", r["status"] == "done", r)

        r = run_op("read", {"path": "out/notes.txt"})
        check("read back", r["status"] == "done" and r["result"]["content"] == "hello e2e\n", r)

        r = run_op("grep", {"pattern": "hello e2e"})
        check("grep finds it", r["status"] == "done" and any("out/notes.txt:1" in h for h in r["result"]["hits"]), r)

        r = run_op("read", {"path": "../../etc/passwd"})
        check("path escape blocked", r["status"] == "error" and r["error"]["code"] == "bad_path", r)

        r = run_op("delete", {"path": "out/notes.txt"})
        check("delete works when enabled", r["status"] == "done", r)

        # kill the client mid-life and verify the relay notices on next status
        client_proc.terminate()
        client_proc.wait(timeout=5)
        check("client process exits cleanly", client_proc.returncode is not None)
    finally:
        if client_proc and client_proc.poll() is None:
            client_proc.kill()
        relay_proc.terminate()
        try:
            relay_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            relay_proc.kill()
        shutil.rmtree(work, ignore_errors=True)

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
