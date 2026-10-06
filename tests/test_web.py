from pathlib import Path

from ace_typer import web

FIX = Path(__file__).parent / "fixtures"


def test_preview_codegenerator_output():
    p = web.preview((FIX / "codegenerator_first_ace.txt").read_text())
    assert p["ok"]
    (code,) = p["codes"]
    assert code["raw_verified"]
    assert [r["name"] for r in code["boxes"]][:2] == ["/?UnFE3n", "AAAa“9q"]
    assert code["first_box"] == 1 and code["last_box"] == 11


def test_preview_error_is_returned_not_raised():
    p = web.preview("Box 1: A B C [ABD]")
    assert not p["ok"] and "does not match" in p["error"]


def test_compile_full_and_from_box():
    text = (FIX / "codegenerator_first_ace.txt").read_text()
    full = web.compile(text)
    assert full["ok"] and full["start_box"] == 1 and full["end_box"] == 11
    assert full["macro"].startswith("A 0.150s")
    part = web.compile(text, first=5)
    assert part["ok"] and part["start_box"] == 5 and set(part["names"]) == {str(n) for n in range(5, 12)}
    assert part["seconds"] < full["seconds"]


def test_compile_needs_params():
    text = "Box 1: 6AB6****  **** = species\nBox 2: 00000000"
    assert not web.compile(text)["ok"]
    ok = web.compile(text, params={"1": {"****": "0197"}})
    assert ok["ok"] and ok["names"]["1"] == "6AB60197"
