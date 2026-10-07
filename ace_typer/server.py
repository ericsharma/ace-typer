"""Web page for typing box codes with the wired board, and live keyboard
control for the Moonlight "Switch" app.

  ./run ace_typer.server [--host 127.0.0.1] [--port 8171] [--board /dev/pa-esp32s3]
                         [--keyboard] [--mpv-socket PATH]

One page plus a small JSON API. Runs on the computer the board's COM port is
plugged into. One board connection serves typing runs, checks and live keys;
it closes after 10 s unused, so Pokémon Automation can have the board.

--keyboard reads Sunshine's virtual keyboard; keys reach the Switch only
while live mode is on (POST /api/live). --mpv-socket draws the key legend and
status on the Switch app's mpv.

No authentication: anyone who can reach the page can type on the Switch.
POSTs must be JSON, so another web page cannot send them from a browser.
"""

import argparse
import contextlib
import json
import signal
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from . import keys, web
from .wired import DEFAULT_PORT, LEAD_IN_MS, PA_UNIT, Stopped, WiredController

IDLE_CLOSE_S = 10


class BoardOwner:
    """The one board connection. Opened on first use; closed after `idle`
    seconds unused and never while a run holds it."""

    def __init__(self, factory, idle=IDLE_CLOSE_S, keep_open=lambda: False):
        self.factory = factory
        self.idle = idle
        self.keep_open = keep_open
        self._lock = threading.RLock()
        self._controller = None
        self._holds = 0
        self._last = 0.0
        threading.Thread(target=self._closer, daemon=True).start()

    def acquire(self):
        with self._lock:
            c = self._controller
            if c is not None and c.link.error:
                self._drop()
            if self._controller is None:
                self._controller = self.factory()
            self._last = time.monotonic()
            return self._controller

    @contextlib.contextmanager
    def hold(self):
        with self._lock:
            c = self.acquire()
            self._holds += 1
        try:
            yield c
        finally:
            with self._lock:
                self._holds -= 1
                self._last = time.monotonic()

    def is_open(self):
        return self._controller is not None

    def close(self):
        """Close unless a run holds it. True when closed (or already closed)."""
        with self._lock:
            if self._holds:
                return False
            self._drop()
            return True

    def discard(self):
        """After a stopped run: that connection's queue stays cancelled."""
        with self._lock:
            self._drop()

    def _drop(self):
        c, self._controller = self._controller, None
        if c is not None:
            c.close()

    def _closer(self):
        while True:
            time.sleep(1)
            with self._lock:
                if (self._controller is not None and not self._holds
                        and not self.keep_open()
                        and time.monotonic() - self._last > self.idle):
                    self._drop()


class Typer:
    """Board checks, Pokémon Automation start/stop, one run at a time, and
    live keys."""

    def __init__(self, board_port=DEFAULT_PORT, systemctl="systemctl", controller_factory=None):
        self.systemctl = systemctl
        self.logs = []
        self.owner = BoardOwner(
            controller_factory or (lambda: WiredController(board_port, log=self.log)),
            keep_open=lambda: self.live.on and self.pa_state() != "active")
        self.live = keys.Live(self.owner, busy=self.busy, log=self.log)
        self._lock = threading.Lock()
        self._thread = None
        self._controller = None
        self._stop_requested = False
        self._pa_cache = (0.0, "unknown")
        self.run = None      # the current or last run, as shown on the page
        self.board = None    # the last board check

    def log(self, message):
        self.logs.append(f"{time.strftime('%H:%M:%S')}  {message}")
        del self.logs[:-200]

    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    # -- Pokémon Automation

    def pa_state(self, max_age=2.0):
        at, state = self._pa_cache
        if time.monotonic() - at < max_age:
            return state
        try:
            out = subprocess.run([self.systemctl, "--user", "is-active", PA_UNIT],
                                 capture_output=True, text=True, timeout=5)
            state = out.stdout.strip() or "unknown"
        except (OSError, subprocess.TimeoutExpired):
            state = "unknown"
        self._pa_cache = (time.monotonic(), state)
        return state

    def pa(self, action):
        if action not in ("start", "stop"):
            return {"ok": False, "error": f"unknown action {action!r}"}
        if action == "start":
            if self.busy():
                return {"ok": False, "error": "typing: stop or wait before starting Pokémon Automation"}
            self.live.release()
            self.owner.close()     # PA must find the serial port free
        self.log(f"{action} Pokémon Automation")
        try:
            out = subprocess.run([self.systemctl, "--user", action, PA_UNIT],
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "error": str(e)}
        if out.returncode != 0:
            return {"ok": False, "error": out.stderr.strip() or f"systemctl {action} failed"}
        return {"ok": True, "pa": self.pa_state(max_age=0)}

    # -- live keys

    def set_live(self, on):
        self.live.set(bool(on))
        if on:
            # Connect now, so the first key press does not wait for it.
            threading.Thread(target=self._warm, daemon=True).start()
        else:
            self.owner.close()     # free the port for PA ("Automation" may be next)
        self.log("live keys " + ("on" if on else "off"))
        return {"ok": True, "live": bool(on)}

    def _warm(self):
        if self.busy() or self.pa_state(max_age=0) == "active":
            return
        try:
            self.owner.acquire()
            self.live.error = None
        except Exception as e:
            self.live.error = str(e)
            self.log(f"keys: {e}")

    def _close_unless_live(self):
        if not self.live.on:
            self.owner.close()

    def overlay_status(self):
        """(status text, ok, show legend) for the Switch app's overlay."""
        if not self.live.on:
            return "", True, False
        legend = self.live.show_legend
        if self.busy():
            box = (self.run or {}).get("box")
            return f"Typing Box {box} — keys paused", True, legend
        if self.pa_state() == "active":
            return "Pokémon Automation has the board — keys off", False, legend
        if self.live.error:
            return f"Keys off: {self.live.error}"[:90], False, legend
        return "Keyboard → Switch", True, legend

    # -- board

    def check(self):
        """Read the board and the Switch link. No input."""
        if self.busy():
            return {"ok": False, "error": "typing: the board is in use"}
        try:
            c = self.owner.acquire()
            info = dict(c.info)
            info.update(c.read_status())
            try:
                c.check_ready()
                info["ok"] = True
            except RuntimeError as e:
                info.update(ok=False, error=str(e))
        except Exception as e:
            self.owner.discard()
            info = {"ok": False, "error": str(e)}
        self._close_unless_live()
        info["time"] = time.time()
        self.board = info
        self.log("board: " + ("ready, player 1" if info["ok"] else info["error"]))
        return info

    # -- typing

    def start(self, req):
        with self._lock:
            if self.busy():
                return {"ok": False, "error": "already typing"}
            first = req.get("first")
            plan = web.plan(req.get("text", ""), code=int(req.get("code") or 1),
                            first=int(first) if first else None,
                            one_box=bool(req.get("one_box")),
                            switch=bool(req.get("switch", True)), params=req.get("params"),
                            fast=bool(req.get("fast")))
            if not plan["ok"]:
                return plan
            if self.pa_state(max_age=0) == "active":
                return {"ok": False, "error": "Pokémon Automation is running and holds "
                                              "the board. Stop it first."}
            segments = plan.pop("segments")
            # Planned time at which each box's steps end, after the lead-in.
            bounds, t = [], LEAD_IN_MS
            for n, steps in segments:
                t += sum(round(s * 1000) for _, s in steps)
                bounds.append((n, t))
            self.run = {"state": "starting", "plan": plan, "box": segments[0][0],
                        "done_ms": 0, "total_ms": t, "started": time.time(),
                        "error": None, "report": None, "_bounds": bounds}
            self._stop_requested = False
            steps = [step for _, seg in segments for step in seg]
            self._thread = threading.Thread(target=self._run, args=(self.run, steps), daemon=True)
            self._thread.start()
        self.log(f"typing Boxes {plan['start_box']}-{plan['end_box']}: "
                 f"{plan['presses']} presses, {plan['seconds']}s, "
                 f"{plan['press_ms']} ms hold / {plan['gap_ms']} ms gap"
                 + (" (fast)" if plan["fast"] else ""))
        return {"ok": True, **plan}

    def _run(self, run, steps):
        self.live.release()        # no key may stay held into the run
        clean = False
        try:
            with self.owner.hold() as c:
                self._controller = c
                if self._stop_requested:
                    raise Stopped()
                run["state"] = "typing"
                report = c.run(steps, log=self.log)
                run["report"] = {"ok": report["ok"], "text": report["text"],
                                 "worst_press_ms": report["worst_press_ms"]}
                run["state"] = "done" if report["ok"] else "timing"
                run["done_ms"] = run["total_ms"]
                clean = True
        except Stopped:
            run["state"] = "stopped"
            self.log("stopped; all buttons released")
        except Exception as e:
            run.update(state="error", error=str(e))
            self.log(f"error: {e}")
            self.log(traceback.format_exc().strip().splitlines()[-1])
        finally:
            self._controller = None
            if clean:
                self._close_unless_live()
            else:
                self.owner.discard()
            run["finished"] = time.time()

    def stop(self):
        if not self.busy():
            return {"ok": False, "error": "not typing"}
        self._stop_requested = True
        c = self._controller
        if c is not None:
            c.stop()
        return {"ok": True}

    def state(self):
        run = None
        if self.run:
            run = {k: v for k, v in self.run.items() if not k.startswith("_")}
            c = self._controller
            if c is not None and run["state"] == "typing":
                done, _ = c.progress()
                done = max(0, done)
                run["done_ms"] = done
                bounds = self.run["_bounds"]
                run["box"] = next((n for n, end in bounds if done < end), bounds[-1][0])
                self.run["box"] = run["box"]
        return {"pa": self.pa_state(), "board": self.board, "run": run,
                "busy": self.busy(), "log": self.logs[-40:],
                "live": {"on": self.live.on, "error": self.live.error,
                         "board_open": self.owner.is_open()}}


def page():
    return resources.files("ace_typer").joinpath("static/index.html").read_bytes()


def make_handler(typer):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, content_type="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, page(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._send(200, typer.state())
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._send(415, {"ok": False, "error": "send JSON"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._send(400, {"ok": False, "error": "bad JSON"})
                return
            routes = {
                "/api/preview": lambda: web.preview(req.get("text", ""),
                                                    switch=bool(req.get("switch", True))),
                "/api/type": lambda: typer.start(req),
                "/api/stop": typer.stop,
                "/api/check": typer.check,
                "/api/pa": lambda: typer.pa(req.get("action")),
                "/api/live": lambda: typer.set_live(req.get("on")),
            }
            route = routes.get(self.path)
            if route is None:
                self._send(404, {"ok": False, "error": "not found"})
                return
            self._send(200, route())

        def log_message(self, fmt, *args):
            pass  # the page polls every second

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Web page for wired ACE box-code typing.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8171)
    ap.add_argument("--board", default=DEFAULT_PORT, help="board serial port")
    ap.add_argument("--systemctl", default="systemctl")
    ap.add_argument("--keyboard", action="store_true",
                    help="read Sunshine's virtual keyboard for live keys")
    ap.add_argument("--mpv-socket", default=None,
                    help="JSON IPC socket of the Switch app's mpv, for the overlay")
    args = ap.parse_args()
    typer = Typer(args.board, systemctl=args.systemctl)
    stop = threading.Event()
    if args.keyboard:
        threading.Thread(target=keys.read_keyboard, args=(typer.live.key, typer.log, stop),
                         daemon=True).start()
    if args.mpv_socket:
        threading.Thread(target=keys.overlay_loop,
                         args=(keys.Overlay(args.mpv_socket), typer.overlay_status, stop),
                         daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(typer))
    server.daemon_threads = True
    print(f"ace-typer web on http://{args.host}:{args.port}", flush=True)

    def terminate(_signum, _frame):
        raise KeyboardInterrupt  # systemd stop: release the buttons first

    signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        typer.stop()
        typer.live.set(False)
        typer.owner.discard()


if __name__ == "__main__":
    main()
