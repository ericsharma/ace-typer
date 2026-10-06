"""Drive an nxbt controller through the nxbt web app's socket.

Runs are sent as one nxbt macro (patched 'macro_async' event), so nxbt times
every press inside the controller process.
"""

import copy
import json
import signal
import sys
import threading
import time

import socketio


NEUTRAL = {
    "L_STICK": {"PRESSED": False, "X_VALUE": 0, "Y_VALUE": 0,
                "LS_UP": False, "LS_LEFT": False, "LS_RIGHT": False, "LS_DOWN": False},
    "R_STICK": {"PRESSED": False, "X_VALUE": 0, "Y_VALUE": 0,
                "RS_UP": False, "RS_LEFT": False, "RS_RIGHT": False, "RS_DOWN": False},
    "DPAD_UP": False, "DPAD_LEFT": False, "DPAD_RIGHT": False, "DPAD_DOWN": False,
    "L": False, "ZL": False, "R": False, "ZR": False,
    "JCL_SR": False, "JCL_SL": False, "JCR_SR": False, "JCR_SL": False,
    "PLUS": False, "MINUS": False, "HOME": False, "CAPTURE": False,
    "Y": False, "X": False, "B": False, "A": False,
}

from .macro import BUTTON, Timings, box_steps, steps_to_macro  # noqa: F401 (re-export)


class Controller:
    def __init__(self, url="http://127.0.0.1:8170"):
        self.sio = socketio.Client()
        self._state = None
        self._got = threading.Event()

        @self.sio.on("state")
        def on_state(data):
            self._state = data
            self._got.set()

        self.sio.connect(url)
        self.index = self._find_connected()

    def _find_connected(self):
        self._got.clear()
        self.sio.emit("state")
        if not self._got.wait(5):
            raise RuntimeError("no state reply from the nxbt web app")
        connected = [int(k) for k, v in self._state.items() if v.get("state") == "connected"]
        if not connected:
            raise RuntimeError(f"no connected controller: {self._state}")
        return max(connected)

    def _send(self, packet):
        self.sio.emit("input", json.dumps([self.index, packet]))

    def _stop_and_exit(self, signum, _frame):
        # Stop the macro inside nxbt and release every button, then exit.
        self.stop()
        sys.exit(128 + signum)

    def stop(self):
        try:
            self.sio.call("macro_clear", self.index, timeout=5)
        finally:
            self._send(NEUTRAL)

    def run(self, steps, log=print):
        """Run steps as one nxbt macro. nxbt times every press inside the
        controller process; sending press/release as separate socket
        messages lost presses whenever the web app stalled."""
        macro = steps_to_macro(steps)
        total = sum(s for _, s in steps)
        log(f"controller {self.index}: {len(steps)} steps, {total:.1f}s (as one nxbt macro)")
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, self._stop_and_exit)
        self._send(NEUTRAL)  # a non-idle direct input would pause the macro
        macro_id = self.sio.call("macro_async", json.dumps([self.index, macro]), timeout=10)
        start = time.monotonic()
        while not self.sio.call("macro_done", json.dumps([self.index, macro_id]), timeout=10):
            if time.monotonic() - start > total + 120:
                self.stop()
                raise RuntimeError("macro did not finish in time; stopped it")
            time.sleep(1)
        log(f"macro finished in {time.monotonic() - start:.1f}s")

    def close(self):
        self.sio.disconnect()
