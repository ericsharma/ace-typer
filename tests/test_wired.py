import struct

import pytest

from ace_typer import pabb2 as p
from ace_typer.macro import Timings, box_and_advance
from ace_typer.parse import parse
from ace_typer.wired import WiredController, steps_to_commands, timing_report

from .fakeboard import FakeBoard


def test_crc32c_matches_pa_table_and_check_value():
    # Values from Common/CRC32/pabb_CRC32_Basic.c.
    assert p.CRC_TABLE[1] == 0xF26B8303 and p.CRC_TABLE[255] == 0xAD7D5351
    # Standard CRC-32C check value; PA seeds and does not invert at the end.
    assert p.crc32c(0xFFFFFFFF, b"123456789") ^ 0xFFFFFFFF == 0xE3069283


def test_parser_skips_noise_and_bad_crc_and_joins_split_packets():
    a = p.encode_packet(7, 1, p.RET_VERSION, struct.pack("<I", 42))
    b = p.encode_packet(7, 2, p.ASK_STREAM_DATA, b"\x00\x00hello")
    bad = bytearray(p.encode_packet(7, 3, p.RET_STREAM_DATA))
    bad[4] ^= 0xFF
    wire = b"\x00\x81junk" + a + bytes(bad) + b
    parser = p.PacketParser()
    got = []
    for i in range(0, len(wire), 5):
        got += parser.feed(wire[i:i + 5], 7)
    # "\x81junk" claims a 117-byte packet; nothing is found until the line is idle.
    assert got == []
    got += parser.idle(7)
    assert [(s, o) for s, o, _ in got] == [(1, p.RET_VERSION), (2, p.ASK_STREAM_DATA)]
    assert got[1][2] == b"\x00\x00hello"


def test_reset_packet_uses_the_fixed_crc_seed():
    pkt = p.encode_packet(0x1234, 0, p.ASK_RESET, struct.pack("<I", 0x1234))
    assert p.PacketParser().feed(pkt, 0x9999)  # any session: seed is 0xFFFFFFFF


def test_buttons_body_layout():
    body = p.buttons_body(["A"], 150)
    assert len(body) == 12  # u16 ms + OemController_State0x30_Buttons
    assert body[:2] == struct.pack("<H", 150)
    assert body[2:5] == bytes((0x08, 0, 0))
    assert body[5:11] == bytes((0x00, 0x08, 0x80, 0x00, 0x08, 0x80))
    assert p.buttons_body(["DPAD_DOWN"], 1)[2:5] == bytes((0, 0, 0x01))
    assert p.buttons_body(["DPAD_LEFT"], 1)[2:5] == bytes((0, 0, 0x08))
    assert p.buttons_body([], 1)[2:5] == bytes(3)
    with pytest.raises(ValueError):
        p.buttons_body([], 0)


def test_player_number_from_lights():
    assert p.player_number(0x01) == 1
    assert p.player_number(0x03) == 2
    assert p.player_number(0x10) == 1   # flashing lights sit in the high nibble
    assert p.player_number(0x00) is None


def test_steps_to_commands_merges_waits_and_maps_buttons():
    steps = [("A", 0.15), (None, 1.5), (None, 0.37), ("PAGE", 0.15), ("DOWN", 0.15), (None, 70)]
    assert steps_to_commands(steps) == [
        ("A", 150), (None, 1870), ("A", 150), ("DPAD_DOWN", 150),
        (None, 65535), (None, 70000 - 65535)]


def _expected_board_commands(steps):
    expected = [(bytes(3), 100)]
    for button, ms in steps_to_commands(steps):
        expected.append((p.buttons_body([button] if button else [], ms)[2:5], ms))
    return expected


def _one_box_steps():
    (code,) = parse("Box 1: P R o / F Q m _ [PRo/FQm ]\nBox 2: A [A]")
    steps, _ = box_and_advance(code, 1, Timings())
    return steps


@pytest.mark.parametrize("faults", [
    {},
    {"drop_in": 7, "drop_out": 5, "garbage": True},
    {"capacity": 4},
])
def test_board_runs_exactly_the_planned_presses(faults):
    board = FakeBoard(**faults)
    steps = _one_box_steps()
    c = WiredController(serial_port=board.serial, log=lambda m: None, ack_timeout=0.02)
    try:
        report = c.run(steps, log=lambda m: None)
    finally:
        c.close()
    assert board.commands == _expected_board_commands(steps)
    assert report["ok"] and report["worst_press_ms"] == 0


def test_refuses_when_not_player_one():
    board = FakeBoard(lights=0x03)
    c = WiredController(serial_port=board.serial, log=lambda m: None, ack_timeout=0.02)
    try:
        with pytest.raises(RuntimeError, match="player 2"):
            c.run(_one_box_steps())
    finally:
        c.close()
    assert board.commands == []


def test_refuses_when_switch_has_not_accepted_the_controller():
    board = FakeBoard(flags=0x04, lights=0)
    c = WiredController(serial_port=board.serial, log=lambda m: None, ack_timeout=0.02)
    try:
        with pytest.raises(RuntimeError, match="not accepted"):
            c.run(_one_box_steps())
    finally:
        c.close()
    assert board.commands == []


def test_refuses_wrong_controller_mode():
    board = FakeBoard(mode=0x1000)
    c = WiredController(serial_port=board.serial, log=lambda m: None, ack_timeout=0.02)
    try:
        with pytest.raises(RuntimeError, match="NS1: Wired Pro Controller"):
            c.run(_one_box_steps())
    finally:
        c.close()


def test_timing_report_flags_a_press_held_too_long():
    commands = [(None, 100), ("A", 150), (None, 370), ("DPAD_UP", 150)]
    stamps = [0, 150_000, 520_000, 700_000]  # µs; the D-pad press took 180 ms
    report = timing_report(commands, [(i, s) for i, s in enumerate(stamps)])
    assert not report["ok"]
    assert report["late"] == [(3, "DPAD_UP", 150, 180.0)]
    assert "CHECK THE BOX NAMES" in report["text"]


def test_stop_cancels_the_queue_and_releases_buttons():
    board = FakeBoard()
    c = WiredController(serial_port=board.serial, log=lambda m: None, ack_timeout=0.02)
    try:
        c.stop()
    finally:
        c.close()
    assert board.cancels == 1
    assert board.commands[-1] == (bytes(3), 50)
