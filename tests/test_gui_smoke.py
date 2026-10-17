#!/usr/bin/env python3
"""GUI smoke test — executes every line of run_gui() without a display.

A fake tkinter records widgets and callbacks; the test then drives the real
Connect / Disconnect / window-close handlers and verifies the core registers
with a real in-process relay and answers commands.
"""

import json
import os
import sys
import tempfile
import threading
import time
import types
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "desktop"))
sys.path.insert(0, os.path.join(HERE, "..", "relay"))

os.environ["BRIDGE_TOKEN"] = "gui-test-token"

import relay            # noqa: E402
import arena_bridge     # noqa: E402

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
    req.add_header("X-Bridge-Token", "gui-test-token")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


# ------------------------------------------------------------ fake tkinter

class FakeVar:
    def __init__(self, value=None, **kw):
        self._v = value

    def get(self):
        return self._v

    def set(self, v):
        self._v = v


class FakeWidget:
    def __init__(self, master=None, **kw):
        self.kw = dict(kw)
        self.__dict__.update({k: v for k, v in kw.items() if k.isidentifier()})
        self.grid_kw = {}
        self.pack_kw = {}

    def grid(self, **kw):
        self.grid_kw = kw

    def pack(self, **kw):
        self.pack_kw = kw

    def configure(self, **kw):
        self.kw.update(kw)

    def columnconfigure(self, *a, **kw):
        pass

    def rowconfigure(self, *a, **kw):
        pass

    def insert(self, *a):
        pass

    def see(self, *a):
        pass


class FakeButton(FakeWidget):
    instances = []

    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        FakeButton.instances.append(self)

    def invoke(self):
        self.kw["command"]()


class FakeText(FakeWidget):
    instances = []

    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.lines = []
        FakeText.instances.append(self)

    def insert(self, _where, text):
        self.lines.append(text)


class FakeTk:
    instances = []

    def __init__(self):
        self.after_fn = None
        self.protocols = {}
        self.destroyed = False
        FakeTk.instances.append(self)

    def title(self, s):
        pass

    def geometry(self, s):
        pass

    def minsize(self, w, h):
        pass

    def protocol(self, name, fn):
        self.protocols[name] = fn

    def after(self, ms, fn):
        self.after_fn = fn   # recorded, not run (drain_queue re-schedules itself)

    def mainloop(self):
        pass

    def destroy(self):
        self.destroyed = True


FAKE_DIR = tempfile.mkdtemp(prefix="gui-fake-dir-")


def install_fake_tk():
    tk = types.ModuleType("tkinter")
    ttk = types.ModuleType("tkinter.ttk")
    filedialog = types.ModuleType("tkinter.filedialog")
    tk.Tk = FakeTk
    tk.StringVar = FakeVar
    tk.BooleanVar = FakeVar
    for mod in (tk, ttk):
        mod.Frame = FakeWidget
        mod.Label = FakeWidget
        mod.Entry = FakeWidget
        mod.Checkbutton = FakeWidget
        mod.Button = FakeButton
        mod.LabelFrame = FakeWidget
    tk.Text = FakeText
    filedialog.askdirectory = lambda **kw: FAKE_DIR
    tk.ttk = ttk
    tk.filedialog = filedialog
    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.filedialog"] = filedialog


# ------------------------------------------------------------ the test

def main():
    install_fake_tk()

    # keep the real config file out of the way
    scratch = tempfile.mkdtemp(prefix="gui-cfg-")
    arena_bridge.CONFIG_PATH = os.path.join(scratch, "cfg.json")

    # shared folder with one file
    shared = tempfile.mkdtemp(prefix="gui-share-")
    with open(os.path.join(shared, "hello.txt"), "w", encoding="utf-8") as f:
        f.write("hi from the gui test\n")

    # real relay in-process
    server = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    errors = []

    def run():
        try:
            arena_bridge.run_gui({
                "relay": "http://127.0.0.1:%d" % port,
                "token": "gui-test-token",
                "root": shared,
                "allow_write": True,
                "allow_delete": False,
            })
        except Exception as e:  # pragma: no cover
            errors.append(repr(e))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    for _ in range(100):
        if FakeButton.instances:
            break
        time.sleep(0.02)

    check("run_gui built the window without errors", not errors, errors)
    check("window widgets created", len(FakeButton.instances) >= 3
          and len(FakeText.instances) == 1 and len(FakeTk.instances) == 1)

    def button(text):
        return next((b for b in FakeButton.instances if b.kw.get("text") == text), None)

    connect, disconnect = button("Connect"), button("Disconnect")
    check("Connect and Disconnect buttons wired",
          connect is not None and callable(connect.kw.get("command"))
          and disconnect is not None and callable(disconnect.kw.get("command")))

    window = FakeTk.instances[0]
    check("window-close handler installed", "WM_DELETE_WINDOW" in window.protocols)

    # --- press Connect: the GUI callback starts a real BridgeCore
    connect.invoke()
    registered = False
    body = {}
    for _ in range(60):
        try:
            body = api(port, "GET", "/status")
            if body.get("clients"):
                registered = True
                break
        except Exception:
            pass
        time.sleep(0.1)
    check("Connect button registered a client with the relay", registered, body)
    check("config saved on connect", os.path.exists(arena_bridge.CONFIG_PATH))

    # --- drive one op through the GUI-started core
    cid = api(port, "POST", "/cmd", {"op": "read", "params": {"path": "hello.txt"}})["id"]
    res = None
    for _ in range(100):
        res = api(port, "GET", "/cmd/%s?wait=2" % cid)
        if res["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    check("read op served by GUI-started core",
          res and res["status"] == "done" and "hi from the gui test" in res["result"]["content"], res)

    # --- exercise the queued log drain (drain_queue is the recorded after() callback)
    if window.after_fn:
        window.after_fn()
        check("activity log captured events", any("sharing folder" in ln for ln in FakeText.instances[0].lines),
              FakeText.instances[0].lines[:5])
    else:
        check("activity log captured events", False, "no after() callback recorded")

    # --- Disconnect, then close the window the way the window manager does
    disconnect.invoke()
    window.protocols["WM_DELETE_WINDOW"]()
    check("window destroyed on close", window.destroyed)

    server.shutdown()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
