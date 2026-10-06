"""Drive an nxbt controller through the nxbt web app's socket ('input' event).

Each button is a direct-input packet held for `press` seconds, then a neutral
packet. The web app keeps the last packet, so timing lives here and the
server never blocks.
"""

import copy
import json
import signal
import sys
import threading
import time
from dataclasses import dataclass

import socketio

from .keyboard import plan_name

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

# Game action -> Switch button (GBA SELECT = Minus, START = Plus).
BUTTON = {
    "UP": "DPAD_UP", "DOWN": "DPAD_DOWN", "LEFT": "DPAD_LEFT", "RIGHT": "DPAD_RIGHT",
    "A": "A", "B": "B", "PAGE": "A", "SELECT": "MINUS", "START": "PLUS",
}


@dataclass
class Timings:
    # Hold well under the naming screen's key-repeat delay (16 frames =
    # 267 ms, naming_screen.c): 250 ms plus Bluetooth jitter repeated D-pad
    # presses. The 250 ms gap keeps presses from merging (0.10/0.10 lost some).
    press: float = 0.15        # button held
    gap: float = 0.25          # release after a button
    menu_open: float = 1.5     # box title A -> JUMP/WALLPAPER/NAME/CANCEL menu
    naming_open: float = 2.5   # NAME -> naming screen faded in
    page_swap: float = 1.0     # SELECT -> next page usable
    full_to_ok: float = 1.0    # 8th character -> cursor moved to OK
    confirm_return: float = 3.5  # OK -> back on the PC box screen
    scroll: float = 2.5        # RIGHT on box title -> next box shown


def box_steps(name, t: Timings, next_box=True):
    """(button or None, seconds) steps that name the current box and move on.
    Starts and ends with the cursor on the box title."""
    steps = [("A", t.press), (None, t.menu_open),
             ("DOWN", t.press), (None, t.gap), ("DOWN", t.press), (None, t.gap),
             ("A", t.press), (None, t.naming_open)]
    actions = plan_name(name)
    for i, a in enumerate(actions):
        if a == "WAIT_FULL":
            steps.append((None, t.full_to_ok))
            continue
        steps.append((a, t.press))
        last = i == len(actions) - 1
        if last:
            steps.append((None, t.confirm_return))
        elif a in ("PAGE", "SELECT"):
            steps.append((None, t.page_swap))
        else:
            steps.append((None, t.gap))
    if next_box:
        steps += [("RIGHT", t.press), (None, t.scroll)]
    return steps


def steps_to_macro(steps):
    """nxbt macro text: 'BUTTON 0.15s' holds a button, '0.25s' waits."""
    lines = []
    wait = 0.0
    for button, seconds in steps:
        if button is None:
            wait += seconds
            continue
        if wait:
            lines.append(f"{wait:.3f}s")
            wait = 0.0
        lines.append(f"{BUTTON[button]} {seconds:.3f}s")
    if wait:
        lines.append(f"{wait:.3f}s")
    return "\n".join(lines)


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
