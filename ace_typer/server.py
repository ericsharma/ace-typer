"""Web page for typing box codes with the wired board.

  ./run ace_typer.server [--host 127.0.0.1] [--port 8171] [--board /dev/pa-esp32s3]

One page plus a small JSON API. Runs on the computer the board's COM port is
plugged into. The board is opened for each run (and for a check) and closed
afterwards, so Pokémon Automation can use it between runs. One run at a time.

No authentication: anyone who can reach the page can type on the Switch.
POSTs must be JSON, so another web page cannot send them from a browser.
"""

import argparse
import json
import signal
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from . import web
from .wired import DEFAULT_PORT, LEAD_IN_MS, PA_UNIT, Stopped, WiredController


class Typer:
    """Board checks, Pokémon Automation start/stop, and one run at a time."""

    def __init__(self, board_port=DEFAULT_PORT, systemctl="systemctl", controller_factory=None):
        self.systemctl = systemctl
        self.factory = controller_factory or (lambda: WiredController(board_port, log=self.log))
        self._lock = threading.Lock()
        self._thread = None
        self._controller = None
        self._stop_requested = False
        self.run = None      # the current or last run, as shown on the page
        self.board = None    # the last board check
        self.logs = []

    def log(self, message):
        self.logs.append(f"{time.strftime('%H:%M:%S')}  {message}")
        del self.logs[:-200]

    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    # -- Pokémon Automation

    def pa_state(self):
        try:
            out = subprocess.run([self.systemctl, "--user", "is-active", PA_UNIT],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        return out.stdout.strip() or "unknown"

    def pa(self, action):
        if action not in ("start", "stop"):
            return {"ok": False, "error": f"unknown action {action!r}"}
        if action == "start" and self.busy():
            return {"ok": False, "error": "typing: stop or wait before starting Pokémon Automation"}
        self.log(f"{action} Pokémon Automation")
        try:
            out = subprocess.run([self.systemctl, "--user", action, PA_UNIT],
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "error": str(e)}
        if out.returncode != 0:
            return {"ok": False, "error": out.stderr.strip() or f"systemctl {action} failed"}
        return {"ok": True, "pa": self.pa_state()}

    # -- board

    def check(self):
        """Connect, read the board and the Switch link, disconnect. No input."""
        if self.busy():
            return {"ok": False, "error": "typing: the board is in use"}
        try:
            c = self.factory()
        except Exception as e:
            self.board = {"ok": False, "error": str(e), "time": time.time()}
            return self.board
        try:
            info = dict(c.info)
            try:
                c.check_ready()
                info["ok"] = True
            except RuntimeError as e:
                info.update(ok=False, error=str(e))
        finally:
            c.close()
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
            if self.pa_state() == "active":
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
        try:
            c = self.factory()
        except Exception as e:
            run.update(state="error", error=str(e))
            self.log(f"error: {e}")
            return
        self._controller = c
        try:
            if self._stop_requested:
                raise Stopped()
            run["state"] = "typing"
            report = c.run(steps, log=self.log)
            run["report"] = {"ok": report["ok"], "text": report["text"],
                             "worst_press_ms": report["worst_press_ms"]}
            run["state"] = "done" if report["ok"] else "timing"
            run["done_ms"] = run["total_ms"]
        except Stopped:
            run["state"] = "stopped"
            self.log("stopped; all buttons released")
        except Exception as e:
            run.update(state="error", error=str(e))
            self.log(f"error: {e}")
            self.log(traceback.format_exc().strip().splitlines()[-1])
        finally:
            self._controller = None
            c.close()
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
        return {"pa": self.pa_state(), "board": self.board, "run": run,
                "busy": self.busy(), "log": self.logs[-40:]}


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
    args = ap.parse_args()
    typer = Typer(args.board, systemctl=args.systemctl)
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
        typer.stop()


if __name__ == "__main__":
    main()
