import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ace_typer.server import Typer, make_handler
from ace_typer.wired import WiredController

from .fakeboard import FakeBoard

FIX = Path(__file__).parent / "fixtures"
CODE = (FIX / "codegenerator_first_ace.txt").read_text()


@pytest.fixture
def systemctl(tmp_path):
    """A fake systemctl that reports Pokémon Automation as `state`."""
    state = tmp_path / "pa-state"
    state.write_text("inactive")
    script = tmp_path / "systemctl"
    script.write_text(f"#!/bin/sh\ncat {state}\n")
    script.chmod(0o755)
    return script, state


@pytest.fixture
def site(systemctl):
    script, state = systemctl
    boards = []

    def factory(**board_options):
        def make():
            board = FakeBoard(**board_options)
            boards.append(board)
            return WiredController(serial_port=board.serial, log=lambda m: None, ack_timeout=0.02)
        return make

    typer = Typer(systemctl=str(script), controller_factory=factory())
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(typer))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    class Site:
        pass

    s = Site()
    s.typer, s.boards, s.state_file, s.base, s.factory = typer, boards, state, base, factory
    yield s
    server.shutdown()


def post(base, path, body=None, content_type="application/json"):
    req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": content_type}, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def get(base, path):
    with urllib.request.urlopen(base + path) as r:
        return r.read()


def wait_idle(base, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = json.loads(get(base, "/api/state"))
        if not st["busy"]:
            return st
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_page_and_preview(site):
    assert b"<title>ACE Typer</title>" in get(site.base, "/")
    p = post(site.base, "/api/preview", {"text": CODE})
    assert p["ok"] and p["codes"][0]["raw_verified"]


def test_post_must_be_json(site):
    with pytest.raises(urllib.error.HTTPError) as e:
        post(site.base, "/api/stop", content_type="text/plain")
    assert e.value.code == 415


def test_type_one_box_then_report(site):
    r = post(site.base, "/api/type", {"text": CODE, "first": 1, "one_box": True})
    assert r["ok"] and r["start_box"] == 1 and r["next_box"] == 2
    st = wait_idle(site.base)
    run = st["run"]
    assert run["state"] == "done", run
    assert run["report"]["ok"] and run["done_ms"] == run["total_ms"]
    (board,) = site.boards
    presses = [c for c in board.commands if c[0] != bytes(3)]
    assert len(presses) == r["presses"]


def test_refuses_while_pokemon_automation_runs(site):
    site.state_file.write_text("active")
    r = post(site.base, "/api/type", {"text": CODE, "one_box": True})
    assert not r["ok"] and "Stop it first" in r["error"]
    assert site.boards == []


def test_stop_mid_run_queues_no_press_after_the_cancel(site):
    site.typer.owner.factory = site.factory(realtime=0.002)  # 150 ms press -> 0.3 s
    r = post(site.base, "/api/type", {"text": CODE, "first": 1, "one_box": False})
    assert r["ok"]
    time.sleep(1.0)
    assert post(site.base, "/api/stop")["ok"]
    st = wait_idle(site.base)
    assert st["run"]["state"] == "stopped"
    (board,) = site.boards
    (at,) = board.cancelled_at
    # After the cancel the board ran only the buttons-up command.
    assert board.commands[at:] == [(bytes(3), 50)]
    assert 0 < at < r["presses"]


def test_check_reports_player(site):
    info = post(site.base, "/api/check")
    assert info["ok"] and info["player"] == 1 and info["mode"] == 0x1100


def test_fast_run_holds_100_ms(site):
    r = post(site.base, "/api/type", {"text": CODE, "first": 1, "one_box": True, "fast": True})
    assert r["ok"] and r["fast"] and (r["press_ms"], r["gap_ms"]) == (100, 150)
    st = wait_idle(site.base)
    assert st["run"]["state"] == "done", st["run"]
    (board,) = site.boards
    presses = [ms for buttons, ms in board.commands if buttons != bytes(3)]
    assert len(presses) == r["presses"] and set(presses) == {100}


def _wait(cond, timeout=3):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)
    return cond()


def test_live_on_connects_and_off_frees_the_board(site):
    assert post(site.base, "/api/live", {"on": True})["ok"]
    assert _wait(lambda: site.typer.owner.is_open())
    st = json.loads(get(site.base, "/api/state"))
    assert st["live"]["on"] and st["live"]["board_open"]
    assert site.typer.overlay_status() == ("Keyboard → Switch", True, True)
    post(site.base, "/api/live", {"on": False})
    assert not site.typer.owner.is_open()
    assert site.typer.overlay_status()[0] == ""


def test_keys_reach_the_board_and_pause_during_a_run(site):
    post(site.base, "/api/live", {"on": True})
    assert _wait(lambda: site.typer.owner.is_open())
    site.typer.live.key(28, 1)   # Enter = A
    site.typer.live.key(28, 0)
    board = site.boards[0]
    assert _wait(lambda: ("cancel",) in board.events)
    assert any(e[0] == "cmd" and e[1][0] == 0x08 for e in board.events)
    # A run uses the same connection; keys pressed meanwhile are ignored.
    board.realtime = 0.002   # 150 ms press -> 0.3 s, so the run is still going below
    r = post(site.base, "/api/type", {"text": CODE, "first": 1, "one_box": True})
    assert r["ok"]
    assert _wait(lambda: site.typer.busy())
    assert site.typer.overlay_status()[0].startswith("Typing Box 1")
    before = len(board.commands)
    site.typer.live.key(103, 1)  # Up: ignored
    time.sleep(0.2)
    ups = [c for c in board.commands[before:] if c[0] == bytes((0, 0, 0x02)) and c[1] == 2000]
    assert ups == []
    site.typer.stop()
    wait_idle(site.base)
    assert len(site.boards) == 1   # the run did not open a second connection


def test_pa_running_shows_on_the_overlay(site):
    post(site.base, "/api/live", {"on": True})
    site.state_file.write_text("active")
    site.typer._pa_cache = (0.0, "unknown")
    text, ok, _ = site.typer.overlay_status()
    assert not ok and "Pokémon Automation has the board" in text


def test_starting_pa_frees_the_board(site):
    post(site.base, "/api/live", {"on": True})
    assert _wait(lambda: site.typer.owner.is_open())
    r = post(site.base, "/api/pa", {"action": "start"})
    assert r["ok"]
    assert not site.typer.owner.is_open()
