#!/usr/bin/env python3
"""End-to-end smoke test for the Arena Workspace Bridge.

Starts the relay in-process, connects a BridgeCore against a temp folder,
then drives it the same way the agent does (POST /cmd, GET /cmd/<id>).
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "relay"))
sys.path.insert(0, os.path.join(HERE, "..", "desktop"))

os.environ["BRIDGE_TOKEN"] = "test-token-123"

import relay  # noqa: E402
import arena_bridge  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, extra))


def http(method, path, body=None, token="test-token-123"):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Bridge-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def submit(op, params=None):
    st, body = http("POST", "/cmd", {"op": op, "params": params or {}})
    assert st == 201, body
    return body["id"]


def result(cid, wait=10):
    st, body = http("GET", "/cmd/%s?wait=%d" % (cid, wait))
    assert st == 200, body
    return body


def main():
    global PORT

    # --- start relay on an ephemeral port
    server = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.Handler)
    PORT = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("relay on port %d" % PORT)

    # --- temp workspace + client
    work = tempfile.mkdtemp(prefix="bridge-test-")
    os.makedirs(os.path.join(work, "src"))
    with open(os.path.join(work, "src", "hello.txt"), "w", encoding="utf-8") as f:
        f.write("hello bridge\nsecond line\n")

    core = arena_bridge.BridgeCore("http://127.0.0.1:%d" % PORT, "test-token-123", work,
                                   allow_write=True, allow_delete=False)
    core.start()
    time.sleep(0.3)

    print("\n-- auth --")
    st, _ = http("GET", "/health", token="")
    check("health is open", st == 200)
    st, _ = http("GET", "/status", token="wrong")
    check("bad token rejected", st == 401)

    print("-- basics --")
    r = result(submit("ping"))
    check("ping", r["status"] == "done" and r["result"]["ok"], r)
    check("ping reports root", r["result"]["root"] == core.root)

    r = result(submit("list", {"path": ""}))
    names = [e["name"] for e in r["result"]["entries"]]
    check("list root", "src" in names, names)

    r = result(submit("read", {"path": "src/hello.txt"}))
    check("read", r["result"]["content"].startswith("hello bridge"), r)

    print("-- write / mkdir --")
    r = result(submit("write", {"path": "src/new/notes.md", "content": "# notes\n"}))
    check("write with mkdirs", r["status"] == "done", r)
    r = result(submit("read", {"path": "src/new/notes.md"}))
    check("read back", r["result"]["content"] == "# notes\n", r)

    b64 = base64.b64encode(b"\x00\x01\x02binary").decode()
    r = result(submit("write", {"path": "src/blob.bin", "content_b64": b64}))
    check("binary write", r["status"] == "done", r)
    r = result(submit("read", {"path": "src/blob.bin"}))
    check("binary read back as base64", r["result"]["encoding"] == "base64"
          and base64.b64decode(r["result"]["content"]) == b"\x00\x01\x02binary", r)

    r = result(submit("mkdir", {"path": "src/empty"}))
    check("mkdir", r["status"] == "done", r)

    print("-- search --")
    r = result(submit("glob", {"pattern": "*.txt"}))
    check("glob", "src/hello.txt" in r["result"]["matches"], r["result"])
    r = result(submit("grep", {"pattern": "second line"}))
    check("grep", any("hello.txt:2" in h for h in r["result"]["hits"]), r["result"])

    print("-- safety --")
    r = result(submit("read", {"path": "../etc/passwd"}))
    check("path escape blocked", r["status"] == "error" and r["error"]["code"] == "bad_path", r)
    r = result(submit("read", {"path": "/etc/passwd"}))
    check("absolute path blocked", r["status"] == "error", r)
    r = result(submit("delete", {"path": "src/hello.txt"}))
    check("delete blocked when disabled", r["status"] == "error" and r["error"]["code"] == "forbidden", r)

    print("-- delete allowed client --")
    core.allow_delete = True
    r = result(submit("delete", {"path": "src/hello.txt"}))
    check("delete file", r["status"] == "done", r)
    r = result(submit("delete", {"path": "src/empty"}))
    check("delete empty dir", r["status"] == "done", r)
    r = result(submit("delete", {"path": "src/new", "recursive": False}))
    check("non-recursive delete refuses non-empty dir",
          r["status"] == "error" and r["error"]["code"] in ("os_error", "bad_request"), r)

    print("-- readonly client --")
    core.allow_write = False
    r = result(submit("write", {"path": "src/x.txt", "content": "no"}))
    check("write blocked when disabled", r["status"] == "error" and r["error"]["code"] == "forbidden", r)

    print("-- misc --")
    core.allow_write = True
    r = result(submit("move", {"src": "src/blob.bin", "dst": "src/blob2.bin"}))
    check("move", r["status"] == "done", r)
    r = result(submit("stat", {"path": "src/blob2.bin"}))
    check("stat", r["status"] == "done" and r["result"]["size"] == 9, r)

    r = result(submit("nope", {}))
    check("unknown op rejected", r["status"] == "error" and r["error"]["code"] == "unknown_op", r)

    st, body = http("GET", "/status")
    check("status snapshot", st == 200 and body["total_commands"] > 0, body)

    core.stop()
    server.shutdown()
    shutil.rmtree(work, ignore_errors=True)

    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
