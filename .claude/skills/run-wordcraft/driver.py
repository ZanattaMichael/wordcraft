#!/usr/bin/env python3
"""Drive the WordCraft desktop app (egui + wgpu) on a headless Linux box.

`start` launches target/release/wordcraft under its own Xvfb display with the JSON-lines
control port (docs/control-protocol.md) and waits until the first frame has rendered. Every
other subcommand opens one connection to 127.0.0.1:PORT, sends one request, prints the reply
as JSON and exits non-zero when the reply says `"ok": false`. The app keeps running between
calls, so state (document, selection, open dialog) carries over.

  driver.py start [--sample] [--file F] [--port 7981] [--build]
  driver.py shot [NAME]                 window PNG (the app's own ui.screenshot)
  driver.py cmd COMMAND [PARAMS_JSON]   run any engine/UI command by id
  driver.py call METHOD [PARAMS_JSON]   any raw control method
  driver.py type TEXT | key KEY [--cmd --shift --alt] | click X Y [--right] [--count N]
  driver.py render [NAME] [--page N] [--scale S]   one page rendered by the engine
  driver.py inspect [ui|doc] | commands [FILTER] | script FILE.jsonl | xshot [NAME]
  driver.py focus                       re-give the window X keyboard focus
  driver.py status | log | stop

Run it from the repo root (paths are resolved from this file, so any cwd works).
"""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BIN = ROOT / "target" / "release" / "wordcraft"
STATE = Path(os.environ.get("WORDCRAFT_RUN_DIR", "/tmp/wordcraft-run"))
SHOTS = Path(os.environ.get("WORDCRAFT_SHOT_DIR", "/tmp/wordcraft-shots"))
DEFAULT_PORT = int(os.environ.get("WORDCRAFT_CONTROL_PORT", "7981"))
DEFAULT_DISPLAY = os.environ.get("WORDCRAFT_DISPLAY", ":99")


def die(msg, code=1):
    print(f"driver: {msg}", file=sys.stderr)
    sys.exit(code)


def read_state():
    try:
        return json.loads((STATE / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A zombie still answers kill(0); treat it as dead.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


def port():
    return read_state().get("port", DEFAULT_PORT)


def request(method, params=None, timeout=90.0, port_=None):
    """One request on a fresh connection; returns the parsed reply."""
    p = port_ or port()
    line = json.dumps({"id": 1, "method": method, "params": params or {}}) + "\n"
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=5) as s:
            s.settimeout(timeout)
            s.sendall(line.encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
    except OSError as e:
        die(f"can't reach the app on 127.0.0.1:{p} ({e}); run `driver.py start` first")
    try:
        return json.loads(buf.decode() or "null")
    except ValueError:
        die(f"bad reply: {buf[:200]!r}")


def emit(reply):
    print(json.dumps(reply, indent=2))
    if not (isinstance(reply, dict) and reply.get("ok")):
        sys.exit(1)


def params_arg(s):
    if not s:
        return {}
    try:
        return json.loads(s)
    except ValueError as e:
        die(f"params must be JSON ({e}): {s}")


def shot_path(name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    name = name or time.strftime("shot-%H%M%S")
    p = Path(name)
    if not p.is_absolute():
        p = SHOTS / p
    return p if p.suffix == ".png" else p.with_suffix(".png")


# ---------------------------------------------------------------- lifecycle


def start(a):
    st = read_state()
    if alive(st.get("app_pid")):
        print(f"already running (pid {st['app_pid']}, port {st['port']}, DISPLAY {st['display']})")
        return
    if a.build or not BIN.exists():
        print("building target/release/wordcraft (first build takes ~5 min)...", flush=True)
        r = subprocess.run(["cargo", "build", "--release", "--locked", "-p", "wordcraft"], cwd=ROOT)
        if r.returncode:
            die("cargo build failed")
    STATE.mkdir(parents=True, exist_ok=True)

    # Our own Xvfb, so a stale :99 from another tool doesn't matter. -nolisten tcp keeps it local.
    disp = a.display
    xvfb = None
    lock = Path(f"/tmp/.X{disp.lstrip(':')}-lock")
    if not lock.exists():
        xlog = open(STATE / "xvfb.log", "w")
        xvfb = subprocess.Popen(
            ["Xvfb", disp, "-screen", "0", a.screen, "-nolisten", "tcp"],
            stdout=xlog, stderr=subprocess.STDOUT, start_new_session=True,
        )
        for _ in range(50):
            if Path(f"/tmp/.X11-unix/X{disp.lstrip(':')}").exists():
                break
            time.sleep(0.1)
        else:
            die(f"Xvfb {disp} didn't come up; see {STATE / 'xvfb.log'}")

    env = dict(os.environ)
    env.pop("WAYLAND_DISPLAY", None)  # winit prefers Wayland when set; we want the Xvfb X11 display
    env.update({
        "DISPLAY": disp,
        "WORDCRAFT_NO_PREFS": "1",  # don't read/write ~/.config/wordcraft/ui.json
    })
    args = [str(BIN), "--control", str(a.port)]
    if a.sample:
        args.append("--sample")
    if a.file:
        args.append(str(Path(a.file).resolve()))
    log = open(STATE / "app.log", "w")
    app = subprocess.Popen(args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (STATE / "state.json").write_text(json.dumps({
        "app_pid": app.pid, "xvfb_pid": xvfb.pid if xvfb else None,
        "port": a.port, "display": disp, "started": time.time(),
    }))

    # Ready = the control port answers ui.inspect with a real window size (first frame done).
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        if app.poll() is not None:
            kill_started(None, xvfb)
            die(f"app exited with {app.returncode}; last log lines:\n" + tail(STATE / "app.log", 30))
        try:
            with socket.create_connection(("127.0.0.1", a.port), timeout=1):
                pass
        except OSError:
            time.sleep(0.25)
            continue
        r = request("ui.inspect", timeout=30, port_=a.port)
        win = (r or {}).get("result", {}).get("window") or [0, 0]
        if r.get("ok") and win[0] > 0:
            focused = x_focus(app.pid, disp)
            print(f"ready: pid {app.pid}, port {a.port}, DISPLAY {disp}, window {win[0]:.0f}x{win[1]:.0f} pt, "
                  f"keyboard focus {'yes' if focused else 'NO (install xdotool)'}, "
                  f"{(time.time() - (deadline - a.timeout)):.1f}s")
            return
        time.sleep(0.25)
    kill_started(app, xvfb)
    die(f"not ready after {a.timeout}s; log:\n" + tail(STATE / "app.log", 30))


def kill_started(app, xvfb):
    for p in (app, xvfb):
        if p and p.poll() is None:
            os.killpg(p.pid, signal.SIGTERM)
    (STATE / "state.json").unlink(missing_ok=True)


def x_focus(pid, disp):
    """Give the window X input focus. Xvfb has no window manager, so nobody else does, and
    egui drops every key and text event (ui.key, ui.text, real typing) until the window has it.
    Pointer events (ui.click) work without it."""
    if not shutil.which("xdotool"):
        return False
    env = dict(os.environ, DISPLAY=disp)
    try:
        wid = subprocess.run(["xdotool", "search", "--sync", "--pid", str(pid)], env=env,
                             capture_output=True, text=True, timeout=10).stdout.split()
        if not wid:
            return False
        subprocess.run(["xdotool", "windowfocus", "--sync", wid[0]], env=env, timeout=10, check=True)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def focus(_a):
    st = read_state()
    if not alive(st.get("app_pid")):
        die("app is not running")
    ok = x_focus(st["app_pid"], st["display"])
    print(json.dumps({"ok": ok}))
    sys.exit(0 if ok else 1)


def tail(p, n):
    try:
        return "\n".join(Path(p).read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return "(no log)"


def stop(_a):
    st = read_state()
    pid = st.get("app_pid")
    if alive(pid):
        try:
            request("app.quit", timeout=5)
        except SystemExit:
            pass
        for _ in range(30):
            if not alive(pid):
                break
            time.sleep(0.1)
        if alive(pid):
            os.killpg(pid, signal.SIGTERM)
    xp = st.get("xvfb_pid")
    if alive(xp):
        os.killpg(xp, signal.SIGTERM)
    (STATE / "state.json").unlink(missing_ok=True)
    print("stopped")


def status(_a):
    st = read_state()
    up = alive(st.get("app_pid"))
    print(json.dumps({**st, "running": up}, indent=2))
    sys.exit(0 if up else 1)


# ---------------------------------------------------------------- actions


def shot(a):
    p = shot_path(a.name)
    r = request("ui.screenshot", {"path": str(p)})
    emit(r)


def xshot(a):
    """X-server-level grab (ImageMagick `import`): use when ui.screenshot itself misbehaves."""
    if not shutil.which("import"):
        die("needs ImageMagick: apt-get install -y imagemagick")
    p = shot_path(a.name)
    st = read_state()
    env = dict(os.environ, DISPLAY=st.get("display", DEFAULT_DISPLAY))
    subprocess.run(["import", "-window", "root", str(p)], env=env, check=True)
    print(json.dumps({"ok": True, "result": {"path": str(p)}}))


def render(a):
    p = shot_path(a.name or f"page{a.page}")
    emit(request("ui.render", {"path": str(p), "page": a.page, "scale": a.scale}))


def cmd(a):
    emit(request("engine.execute", {"command": a.command, "params": params_arg(a.params)}))


def call(a):
    emit(request(a.method, params_arg(a.params)))


def type_(a):
    emit(request("ui.text", {"text": a.text}))


def key(a):
    emit(request("ui.key", {"key": a.key, "cmd": a.cmd, "shift": a.shift, "alt": a.alt}))


def click(a):
    emit(request("ui.click", {"x": a.x, "y": a.y, "button": "right" if a.right else "left", "count": a.count}))


def inspect(a):
    emit(request("document.inspect" if a.what == "doc" else "ui.inspect", {"text": True} if a.what == "doc" else {}))


def commands(a):
    r = request("engine.commands")
    if not r.get("ok"):
        emit(r)
    q = (a.filter or "").lower()
    for c in r["result"]:
        hay = f"{c['id']} {c['label']} {c['location']}".lower()
        if q in hay:
            print(f"{c['id']:<34} {'on ' if c['enabled'] else 'off'} {c['label']}  [{c['location']}]")


def script(a):
    """Send each JSON line ({"method":…,"params":…}) in order; stop at the first failure."""
    for n, line in enumerate(Path(a.file).read_text().splitlines(), 1):
        if not line.strip():
            continue
        msg = json.loads(line)
        r = request(msg["method"], msg.get("params"))
        print(json.dumps({"line": n, **r}))
        if not r.get("ok"):
            sys.exit(1)


def log(_a):
    print(tail(STATE / "app.log", 200))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd_name", required=True)

    s = sub.add_parser("start")
    s.add_argument("--sample", action="store_true", help="open the built-in sample document")
    s.add_argument("--file", help="document to open (.docx .odt .rtf .md .html .txt .json)")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--display", default=DEFAULT_DISPLAY)
    s.add_argument("--screen", default="1920x1200x24", help="Xvfb screen WxHxDEPTH")
    s.add_argument("--timeout", type=float, default=60)
    s.add_argument("--build", action="store_true", help="cargo build --release first")
    s.set_defaults(fn=start)

    sub.add_parser("stop").set_defaults(fn=stop)
    sub.add_parser("status").set_defaults(fn=status)
    sub.add_parser("log").set_defaults(fn=log)
    sub.add_parser("focus").set_defaults(fn=focus)

    s = sub.add_parser("shot"); s.add_argument("name", nargs="?"); s.set_defaults(fn=shot)
    s = sub.add_parser("xshot"); s.add_argument("name", nargs="?"); s.set_defaults(fn=xshot)
    s = sub.add_parser("render"); s.add_argument("name", nargs="?")
    s.add_argument("--page", type=int, default=1); s.add_argument("--scale", type=float, default=1.5); s.set_defaults(fn=render)
    s = sub.add_parser("cmd"); s.add_argument("command"); s.add_argument("params", nargs="?"); s.set_defaults(fn=cmd)
    s = sub.add_parser("call"); s.add_argument("method"); s.add_argument("params", nargs="?"); s.set_defaults(fn=call)
    s = sub.add_parser("type"); s.add_argument("text"); s.set_defaults(fn=type_)
    s = sub.add_parser("key"); s.add_argument("key")
    for m in ("cmd", "shift", "alt"):
        s.add_argument(f"--{m}", action="store_true")
    s.set_defaults(fn=key)
    s = sub.add_parser("click"); s.add_argument("x", type=float); s.add_argument("y", type=float)
    s.add_argument("--right", action="store_true"); s.add_argument("--count", type=int, default=1); s.set_defaults(fn=click)
    s = sub.add_parser("inspect"); s.add_argument("what", nargs="?", choices=["ui", "doc"], default="ui"); s.set_defaults(fn=inspect)
    s = sub.add_parser("commands"); s.add_argument("filter", nargs="?"); s.set_defaults(fn=commands)
    s = sub.add_parser("script"); s.add_argument("file"); s.set_defaults(fn=script)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
