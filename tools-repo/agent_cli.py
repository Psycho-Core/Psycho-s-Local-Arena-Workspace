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
