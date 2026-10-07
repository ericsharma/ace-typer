import json
import socket
import struct
import threading
import time

import pytest

from ace_typer import keys, pabb2 as p
from ace_typer.server import BoardOwner
from ace_typer.wired import WiredController

from .fakeboard import FakeBoard

ENTER, DOWN, RIGHT, SHIFT, F1, W, D, ESC, Q = 28, 108, 106, 42, 59, 17, 32, 1, 16


def report(buttons, left=(0x800, 0x800)):
    return p.buttons_body(buttons, 1, left)[2:11]


def test_state_of_maps_like_pa():
    assert keys.state_of({ENTER, SHIFT, DOWN}) == (["A", "B", "DPAD_DOWN"], (0x800, 0x800))
    assert keys.state_of({ESC}) == (["HOME"], (0x800, 0x800))
    assert keys.state_of({Q}) == (["L"], (0x800, 0x800))
    assert keys.state_of({W, D}) == ([], (0xFFF, 0xFFF))
    assert keys.state_of({F1, 999}) == ([], (0x800, 0x800))   # unmapped keys send nothing


def test_stick_bytes():
    assert p.stick_bytes(0x800, 0x800) == bytes((0x00, 0x08, 0x80))
    assert p.stick_bytes(0xFFF, 0x001) == bytes((0xFF, 0x1F, 0x00))


@pytest.fixture
def rig():
    boards = []

    def make():
        board = FakeBoard()
        boards.append(board)
        return WiredController(serial_port=board.serial, log=lambda m: None, ack_timeout=0.02)

    owner = BoardOwner(make)
    busy = {"on": False}
    live = keys.Live(owner, busy=lambda: busy["on"], log=lambda m: None)
    yield live, owner, boards, busy
    live.close()
    owner.discard()


def first(boards, timeout=2):
    deadline = time.monotonic() + timeout
    while not boards and time.monotonic() < deadline:
        time.sleep(0.01)
    (board,) = boards
    return board


def settle(board, n, timeout=2):
    deadline = time.monotonic() + timeout
    while len(board.events) < n and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    return board.events


def test_keys_do_nothing_while_live_is_off(rig):
    live, owner, boards, _ = rig
    live.key(ENTER, 1)
    live.key(ENTER, 0)
    assert boards == []


def test_press_holds_release_cancels(rig):
    live, owner, boards, _ = rig
    live.set(True)
    live.key(ENTER, 1)
    live.key(ENTER, 2)          # auto-repeat: ignored
    live.key(ENTER, 0)
    board = first(boards)
    events = settle(board, 3)
    assert events == [("replace",), ("cmd", report(["A"]), keys.HOLD_MS), ("cancel",)]


def test_chord_replaces_without_a_neutral_gap(rig):
    live, owner, boards, _ = rig
    live.set(True)
    live.key(DOWN, 1)
    live.key(ENTER, 1)
    live.key(ENTER, 0)
    live.key(DOWN, 0)
    board = first(boards)
    events = settle(board, 7)
    assert events == [
        ("replace",), ("cmd", report(["DPAD_DOWN"]), keys.HOLD_MS),
        ("replace",), ("cmd", report(["A", "DPAD_DOWN"]), keys.HOLD_MS),
        ("replace",), ("cmd", report(["DPAD_DOWN"]), keys.HOLD_MS),
        ("cancel",),
    ]


def test_f1_toggles_the_legend_and_sends_nothing(rig):
    live, owner, boards, _ = rig
    live.set(True)
    assert live.show_legend
    live.key(F1, 1)
    live.key(F1, 0)
    assert not live.show_legend
    assert boards == []


def test_keys_pause_during_a_run(rig):
    live, owner, boards, busy = rig
    live.set(True)
    busy["on"] = True
    live.key(ENTER, 1)
    assert boards == []


def test_held_key_is_refreshed(rig, monkeypatch):
    monkeypatch.setattr(keys, "REFRESH_S", 0.05)
    live, owner, boards, _ = rig
    live2 = keys.Live(owner, log=lambda m: None)   # picks up the short refresh
    live2.set(True)
    live2.key(RIGHT, 1)
    time.sleep(0.4)
    live2.key(RIGHT, 0)
    live2.close()
    board = first(boards)
    holds = [e for e in settle(board, 4) if e[0] == "cmd"]
    assert len(holds) >= 3 and all(e[1] == report(["DPAD_RIGHT"]) for e in holds)
    assert board.events[-1] == ("cancel",)


def test_turning_live_off_releases_held_buttons(rig):
    live, owner, boards, _ = rig
    live.set(True)
    live.key(SHIFT, 1)
    board = first(boards)
    settle(board, 2)
    live.set(False)
    assert settle(board, 3)[-1] == ("cancel",)


def test_find_keyboard_in_proc_devices():
    text = ('I: Bus=0000\nN: Name="Mouse passthrough"\nH: Handlers=mouse2 event20\n\n'
            'I: Bus=0000\nN: Name="Keyboard passthrough"\nH: Handlers=sysrq kbd event22 leds\n')
    assert keys.find_keyboard(devices=text) == "/dev/input/event22"
    assert keys.find_keyboard(devices="N: Name=\"x\"\n") is None


def test_read_keyboard_feeds_key_events(tmp_path, monkeypatch):
    dev = tmp_path / "event22"
    events = [(0, 0, 4, 4, 0x28), (0, 0, keys.EV_KEY, ENTER, 1), (0, 0, 0, 0, 0),
              (0, 0, keys.EV_KEY, ENTER, 0)]
    dev.write_bytes(b"".join(keys.EVENT.pack(*e) for e in events))
    monkeypatch.setattr(keys, "find_keyboard", lambda: str(dev))
    got, stop = [], threading.Event()

    def on_key(code, value):
        got.append((code, value))
        if len(got) == 2:
            stop.set()

    keys.read_keyboard(on_key, log=lambda m: None, stop=stop)
    assert got == [(ENTER, 1), (ENTER, 0)]


def test_overlay_text_escapes_and_shows_legend():
    text = keys.overlay_text("Keys off: {bad} \\ path", False, True)
    assert "\\{bad\\}" in text and "\\\\ path" in text
    assert "Arrows  D-pad" in text
    assert "Arrows" not in keys.overlay_text("Keyboard → Switch", True, False)


def test_overlay_sends_osd_overlay_to_mpv(tmp_path):
    path = str(tmp_path / "mpv.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    got = []

    def accept():
        conn, _ = srv.accept()
        got.append(json.loads(conn.makefile().readline()))
        conn.close()

    t = threading.Thread(target=accept)
    t.start()
    overlay = keys.Overlay(path)
    assert overlay.show("hello")
    t.join(2)
    assert got[0]["command"]["name"] == "osd-overlay"
    assert got[0]["command"]["data"] == "hello"
    assert overlay.show("hello")          # unchanged: nothing sent
    srv.close()
    assert not keys.Overlay(str(tmp_path / "missing.sock")).show("x")
