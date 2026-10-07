"""Type through the wired ESP32-S3 controller (PA's PABotBase2 firmware).

  ./run ace_typer.wired [--port /dev/pa-esp32s3]   # check the board; sends no input

The board must run Pokémon Automation's PABotBase2 firmware in
"NS1: Wired Pro Controller" mode (PA sets both), plugged into the Switch dock.
Pokémon Automation must be stopped while this types: only one program can use
the serial port.

Every press and gap is a command in the board's queue. The board runs each
one for its exact duration and reports the time it finished, so this checks
afterwards that every press was held as planned.
"""

import argparse
import signal
import struct
import subprocess
import sys
import threading
import time

from . import pabb2
from .macro import BUTTON

DEFAULT_PORT = "/dev/pa-esp32s3"
PA_UNIT = "pokemon-automation"
# Board timestamps in CQ_COMMAND_FINISHED count microseconds.
TICKS_PER_MS = 1000
# A press measured further than this from its plan is reported.
TOLERANCE_MS = 10
LEAD_IN_MS = 100   # neutral before the first press, so it is measured too
# The board queues 255 commands. Fewer means that if this process dies
# without a stop, the board runs at most a few seconds more of input.
QUEUE_LIMIT = 16


def steps_to_commands(steps):
    """[(button name or None, milliseconds)], waits merged, long ones split."""
    commands = []
    for action, seconds in steps:
        ms = round(seconds * 1000)
        if ms <= 0:
            raise ValueError(f"step {action} has no duration")
        if action is None:
            if commands and commands[-1][0] is None:
                ms += commands.pop()[1]
            commands.append((None, ms))
        else:
            commands.append((BUTTON[action], ms))
    out = []
    for button, ms in commands:
        while ms > 0xFFFF:
            out.append((button, 0xFFFF))
            ms -= 0xFFFF
        out.append((button, ms))
    return out


def pa_running():
    try:
        return subprocess.run(["systemctl", "--user", "is-active", "--quiet", PA_UNIT],
                              check=False).returncode == 0
    except FileNotFoundError:
        return False


class WiredController:
    def __init__(self, port=DEFAULT_PORT, log=print, serial_port=None, **link_options):
        self.log = log
        if serial_port is None:
            if pa_running():
                raise RuntimeError(
                    "Pokémon Automation is running and holds the board. Stop it first:\n"
                    f"  systemctl --user stop {PA_UNIT}")
            serial_port = open_serial(port)
        self.serial = serial_port
        self.link = pabb2.Link(serial_port, log=log, **link_options)
        self.board = pabb2.Board(self.link, log=log)
        self.link.connect()
        self.info = self.read_info()
        self.capacity = min(self.info["queue"], QUEUE_LIMIT)
        self._stop_done = threading.Event()

    def read_info(self):
        b = self.board
        protocol = b.query_u32(pabb2.MSG_PROTOCOL_VERSION)
        if protocol // 100 != pabb2.MESSAGE_PROTOCOL // 100:
            raise pabb2.ProtocolError(
                f"message protocol {protocol} does not match {pabb2.MESSAGE_PROTOCOL}; "
                "flash the board with the firmware this code was written for")
        info = {
            "protocol": protocol,
            "firmware": b.query_u32(pabb2.MSG_FIRMWARE_VERSION),
            "name": b.query_data(pabb2.MSG_DEVICE_NAME).decode("ascii", "replace"),
            "queue": max(4, min(b.query_u32(pabb2.MSG_CQ_CAPACITY), 255)),
            "mode": b.query_u32(pabb2.MSG_READ_CONTROLLER_MODE),
        }
        info.update(self.read_status())
        return info

    def read_status(self):
        _, data = self.board.request(pabb2.MSG_REQUEST_STATUS)
        status = {"ready": False, "paired": False, "player": None}
        if len(data) >= 6:
            _, flags, lights = struct.unpack_from("<IBB", data)
            status["ready"] = bool(flags & 2)
            status["paired"] = bool(flags & 4)
            status["player"] = pabb2.player_number(lights) if flags & 2 else None
        return status

    def check_ready(self):
        """Refuse to type unless the Switch reads this controller as player 1."""
        if self.info["mode"] != pabb2.CID_NS1_WIRED_PRO_CONTROLLER:
            raise RuntimeError(
                f"the board is in controller mode 0x{self.info['mode']:x}, not "
                '"NS1: Wired Pro Controller". Select that mode once in PA.')
        status = self.read_status()
        if not status["ready"] or status["player"] is None:
            raise RuntimeError(
                "the Switch has not accepted the controller yet (no player lights). "
                "Its first button press would only wake it and be lost.")
        if status["player"] != 1:
            raise RuntimeError(
                f"the controller is player {status['player']}. FireRed reads player 1 "
                "only: disconnect the other controllers, then reconnect this one.")

    def run(self, steps, log=None):
        """Type `steps`. Returns the timing report. Raises Stopped if stop()
        ran meanwhile (from a signal or another thread)."""
        log = log or self.log
        self.check_ready()
        commands = [(None, LEAD_IN_MS)] + steps_to_commands(steps)
        if commands[-1][0] is not None:
            commands.append((None, LEAD_IN_MS))
        total = sum(ms for _, ms in commands) / 1000
        presses = sum(1 for b, _ in commands if b)
        log(f"wired controller (player 1): {presses} presses, {total:.1f}s, "
            f"board queue {self.capacity}")
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, self._stop_and_exit)
        self.commands = commands
        self._finished_before = len(self.board.finished)
        start = time.monotonic()
        next_report = start + 10
        try:
            for button, ms in commands:
                self.board.command(pabb2.MSG_NS1_BUTTONS,
                                   pabb2.buttons_body([button] if button else [], ms),
                                   self.capacity)
                if time.monotonic() >= next_report:
                    log(f"  {self.progress()[0] / 1000:.0f}s of {total:.0f}s done")
                    next_report += 10
        except pabb2.Cancelled:
            # stop() runs in another thread; let it release the buttons
            # before the caller closes the port.
            self._stop_done.wait(timeout=5)
            raise Stopped() from None
        if not self.board.wait_all(timeout=total + 30):
            self.stop()
            raise RuntimeError("the board did not finish in time; stopped it")
        if self.board.cancelled:
            self._stop_done.wait(timeout=5)
            raise Stopped()
        log(f"board finished in {time.monotonic() - start:.1f}s")
        report = timing_report(commands, self.board.finished[self._finished_before:])
        log(report["text"])
        return report

    def progress(self):
        """(planned ms of the commands the board has finished, planned ms in total)."""
        commands = getattr(self, "commands", None)
        if not commands:
            return 0, 0
        done = len(self.board.finished) - self._finished_before
        return (sum(ms for _, ms in commands[:done]), sum(ms for _, ms in commands))

    def stop(self):
        """Clear the board's queue and release every button. Safe from any thread."""
        try:
            self.board.cancel_commands()
            self.board.release(pabb2.MSG_NS1_BUTTONS, pabb2.buttons_body([], 50))
            self.board.wait_all(timeout=2)
        except pabb2.ProtocolError as e:
            self.log(f"stop: {e}")
        finally:
            self._stop_done.set()

    def _stop_and_exit(self, signum, _frame):
        self.stop()
        sys.exit(128 + signum)

    def close(self):
        self.link.close()
        self.serial.close()


class Stopped(Exception):
    """The run was stopped before it finished."""


def timing_report(commands, finished):
    """Compare each command's board-measured duration with its plan.

    A command's duration is the time between its finish and the previous
    command's finish, so the lead-in (first command) is not measured."""
    stamps = [stamp for _, stamp in finished]
    worst_press = worst_gap = 0.0
    late = []
    for i in range(1, len(commands)):
        button, planned = commands[i]
        actual = ((stamps[i] - stamps[i - 1]) & 0xFFFFFFFF) / TICKS_PER_MS
        error = actual - planned
        if button:
            worst_press = max(worst_press, abs(error))
            if abs(error) > TOLERANCE_MS:
                late.append((i, button, planned, actual))
        else:
            worst_gap = max(worst_gap, abs(error))
    text = (f"timing: every press within {worst_press:.1f} ms of plan, "
            f"waits within {worst_gap:.1f} ms")
    if late:
        text += "\n" + "\n".join(
            f"  ! command {i} {b}: {a:.1f} ms held, {p} ms planned" for i, b, p, a in late)
        text += "\n  CHECK THE BOX NAMES: a press was not held as planned."
    return {"ok": not late, "worst_press_ms": worst_press, "worst_gap_ms": worst_gap,
            "late": late, "text": text}


def open_serial(port):
    import serial

    s = serial.Serial()
    s.port = port
    s.baudrate = pabb2.Link.BAUD_RATES[0]
    s.timeout = 0.05
    s.write_timeout = 2
    # The ESP32 dev board resets at DTR=0, RTS=1. Opening raises both lines;
    # then only RTS drops, the steady state PA uses (DTR=1, RTS=0).
    s.dtr = True
    s.rts = False
    s.exclusive = True
    s.open()
    return s


def main():
    ap = argparse.ArgumentParser(description="Check the wired controller board. Sends no input.")
    ap.add_argument("--port", default=DEFAULT_PORT)
    args = ap.parse_args()
    c = WiredController(args.port)
    try:
        i = c.info
        print(f"board: {i['name']}, firmware {i['firmware']}, protocol {i['protocol']}")
        print(f"mode: 0x{i['mode']:x}"
              + (" (NS1: Wired Pro Controller)" if i["mode"] == pabb2.CID_NS1_WIRED_PRO_CONTROLLER else ""))
        print(f"command queue: {i['queue']}")
        print(f"switch: paired={i['paired']} ready={i['ready']} player={i['player']}")
        try:
            c.check_ready()
            print("ready to type")
        except RuntimeError as e:
            print(f"NOT ready: {e}")
            sys.exit(1)
    finally:
        c.close()


if __name__ == "__main__":
    main()
