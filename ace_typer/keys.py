"""Live keyboard control of the wired board, for the Moonlight "Switch" app.

Sunshine turns the Moonlight client's keyboard into a Linux input device
("Keyboard passthrough"). This reads it directly, maps keys the way Pokémon
Automation's Virtual Console does, and drives the board: a key down holds its
button, a key up releases it at once. An mpv overlay shows the keys and the
status. Only active while live mode is on (the "Switch" app turns it on).
"""

import json
import os
import queue
import socket
import struct
import threading
import time

from . import pabb2

# evdev key codes (linux/input-event-codes.h) -> Switch button, as in PA's
# default Pro Controller keyboard map.
KEYMAP = {
    103: "DPAD_UP", 108: "DPAD_DOWN", 105: "DPAD_LEFT", 106: "DPAD_RIGHT",
    28: "A", 96: "A",                         # Enter, keypad Enter
    42: "B", 54: "B", 29: "B", 97: "B",       # Shift, Ctrl (both sides)
    40: "X",                                  # '
    53: "Y",                                  # /
    16: "L", 18: "R", 27: "R",                # Q, E, ]
    19: "ZL", 43: "ZR",                       # R, \
    13: "PLUS", 78: "PLUS",                   # =, keypad +
    12: "MINUS", 74: "MINUS",                 # -, keypad -
    102: "HOME", 1: "HOME", 35: "HOME",       # Home, Esc, H
    110: "CAPTURE",                           # Insert
}
STICK = {17: (0, 1), 30: (-1, 0), 31: (0, -1), 32: (1, 0)}   # W A S D
KEY_F1 = 59

LEGEND = (
    "Arrows  D-pad      Enter  A      Shift  B      '  X      /  Y",
    "Q  L      E  R      R  ZL      \\  ZR      =  +      -  −",
    "Home / Esc / H  HOME      Insert  Capture      WASD  stick      F1  hide",
)

HOLD_MS = 2000       # a held key's command; refreshed before it runs out
REFRESH_S = 1.0
# Each state is held at least this long before the next is applied (3 frames
# at 60 fps; USB reports every 8 ms). Without it a key down and up that arrive
# together, as a phone's on-screen keyboard sends them, cancel the press
# before the board starts it. Changes queue, never merge, so a fast double tap
# stays two presses.
MIN_STATE_S = 0.05
EVENT = struct.Struct("llHHi")   # struct input_event on 64-bit Linux
EV_KEY = 1


def state_of(pressed):
    """(button names, left stick (x, y)) for a set of pressed key codes."""
    buttons = sorted({KEYMAP[k] for k in pressed if k in KEYMAP})
    dx = sum(STICK[k][0] for k in pressed if k in STICK)
    dy = sum(STICK[k][1] for k in pressed if k in STICK)
    return buttons, (0x800 + dx * 0x7FF, 0x800 + dy * 0x7FF)


NEUTRAL = ([], (0x800, 0x800))
_REFRESH = object()


class Live:
    """Turns key events into board commands. `owner` gives the board.

    Key events only compute the wanted state and queue it; one sender thread
    applies the queue in order, so the board sees every state for at least
    MIN_STATE_S."""

    def __init__(self, owner, busy=lambda: False, log=print):
        self.owner = owner
        self.busy = busy           # True while a typing run uses the board
        self.log = log
        self.on = False
        self.show_legend = True
        self.error = None
        self._pressed = set()
        self._target = NEUTRAL     # last state queued
        self._sent = NEUTRAL       # last state the board got
        self._sent_at = 0.0
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._stop = threading.Event()
        threading.Thread(target=self._sender, daemon=True).start()
        threading.Thread(target=self._refresher, daemon=True).start()

    def set(self, on):
        with self._lock:
            self.on = on
            self._pressed.clear()
            self.error = None
            if on:
                self.show_legend = True
            self._queue_state(NEUTRAL, force=True)
        if not on:
            self._queue.join()

    def key(self, code, value):
        """value: 1 down, 0 up, 2 auto-repeat (ignored)."""
        if value == 2:
            return
        with self._lock:
            if not self.on:
                return
            if code == KEY_F1:
                if value == 1:
                    self.show_legend = not self.show_legend
                return
            if value:
                self._pressed.add(code)
            else:
                self._pressed.discard(code)
            if self.busy():
                return                 # a typing run owns the board
            self._queue_state(state_of(self._pressed))

    def release(self):
        """Let go of every held button, and wait until the board has it
        (before a typing run takes the board)."""
        with self._lock:
            self._pressed.clear()
            self._queue_state(NEUTRAL, force=True)
        self._queue.join()

    def _queue_state(self, state, force=False):
        if state != self._target or force:
            self._target = state
            self._queue.put((state, force))

    def _sender(self):
        while not self._stop.is_set():
            item = self._queue.get()
            try:
                if item is None:
                    return
                if item is _REFRESH:
                    if self.on and self._sent != NEUTRAL and not self.busy():
                        self._send(self._sent)
                    continue
                state, force = item
                if state == self._sent and not force:
                    continue
                if not force and (not self.on or self.busy()):
                    continue
                if state == NEUTRAL and self._sent == NEUTRAL and force:
                    continue           # nothing held: no need to touch the board
                wait = self._sent_at + MIN_STATE_S - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._send(state)
            finally:
                self._queue.task_done()

    def _send(self, state):
        buttons, left = state
        try:
            controller = self.owner.acquire()
            board = controller.board
            if state == NEUTRAL:
                board.clear_queue()
            else:
                board.replace_with(pabb2.MSG_NS1_BUTTONS,
                                   pabb2.buttons_body(buttons, HOLD_MS, left))
            self._sent = state
            self._sent_at = time.monotonic()
            self.error = None
        except Exception as e:  # PA holds the port, board unplugged, ...
            self._sent = NEUTRAL
            if str(e) != self.error:
                self.log(f"keys: {e}")
            self.error = str(e)

    def _refresher(self):
        while not self._stop.wait(REFRESH_S):
            if self.on and self._sent != NEUTRAL and self._queue.empty():
                self._queue.put(_REFRESH)

    def close(self):
        self._stop.set()
        self._queue.put(None)


def find_keyboard(name="Keyboard passthrough", devices=None):
    """/dev/input/eventN of Sunshine's virtual keyboard, or None."""
    try:
        text = devices if devices is not None else open("/proc/bus/input/devices").read()
    except OSError:
        return None
    for block in text.split("\n\n"):
        if f'N: Name="{name}"' in block:
            for line in block.splitlines():
                if line.startswith("H: Handlers="):
                    for h in line.split("=", 1)[1].split():
                        if h.startswith("event"):
                            return f"/dev/input/{h}"
    return None


def read_keyboard(on_key, log=print, stop=None):
    """Feed (code, value) key events to on_key forever. Finds the device again
    when Sunshine restarts and recreates it."""
    stop = stop or threading.Event()
    warned = False
    while not stop.is_set():
        path = find_keyboard()
        if path is None:
            if not warned:
                log("keys: no 'Keyboard passthrough' device yet (Sunshine not running?)")
                warned = True
            stop.wait(3)
            continue
        try:
            with open(path, "rb", buffering=0) as dev:
                log(f"keys: reading {path}")
                warned = False
                while not stop.is_set():
                    data = dev.read(EVENT.size)
                    if len(data) < EVENT.size:
                        break
                    _, _, etype, code, value = EVENT.unpack(data)
                    if etype == EV_KEY:
                        on_key(code, value)
        except OSError as e:
            log(f"keys: {path}: {e}")
        stop.wait(3)


class Overlay:
    """Text drawn by the Switch app's mpv, through its JSON IPC socket."""

    def __init__(self, path):
        self.path = path
        self._last = None

    def _send(self, command):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(self.path)
            s.sendall(json.dumps({"command": command}).encode() + b"\n")

    def show(self, text):
        """Draw ASS text; no-op when unchanged. False when mpv is not there."""
        if text == self._last:
            return True
        if not os.path.exists(self.path):
            self._last = None
            return False
        try:
            self._send({"name": "osd-overlay", "id": 7, "format": "ass-events",
                        "data": text, "res_x": 1920, "res_y": 1080})
        except OSError:
            self._last = None
            return False
        self._last = text
        return True


def ass_escape(text):
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def overlay_text(status, ok, legend):
    """ASS events: a status line top right, the key legend bottom left."""
    colour = "&H9EE6A0&" if ok else "&H7A85FB&"   # BGR: green / red-pink
    out = [f"{{\\an9\\fs26\\1c{colour}\\bord3\\3c&H000000&}}{ass_escape(status)}"]
    if legend:
        lines = "\\N".join(ass_escape(line) for line in LEGEND)
        out.append(f"{{\\an1\\fs22\\1c&HFFFFFF&\\bord3\\3c&H000000&}}{lines}")
    return "\n".join(out)


def overlay_loop(overlay, status, stop, period=0.5):
    """Push `status()` -> (text, ok, legend) to mpv while it changes."""
    while not stop.wait(period):
        text, ok, legend = status()
        overlay.show(overlay_text(text, ok, legend) if text else "")

