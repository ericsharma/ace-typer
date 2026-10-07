import re
from pathlib import Path

import pytest

from ace_typer.keyboard import plan_name, simulate
from ace_typer.parse import ParseError, fill, parse

FIX = Path(__file__).parent / "fixtures"


def names(code):
    return {n: b.name for n, b in code.boxes.items()}


# ── golden: character codes ────────────────────────────────────────────────

def test_box14_exit_pomeg_keeps_period_and_comma():
    (code,) = parse("Box 14: U . o _ _ , o a [U.o  ,oa]")
    assert code.kind == "chars"
    assert names(code) == {14: "U.o  ,oa"}


def test_box14_exit_gist_keeps_ellipsis():
    (code,) = parse("```Box 14: U … o _ _ … o a [U…o  …oa]```")
    assert names(code) == {14: "U…o  …oa"}


def test_trailing_spaces_and_short_names():
    (code,) = parse(
        "Box  2: P R o / F Q m _\t[PRo/FQm ]\n"
        "Box  4: E _ F R m       [E FRm]\n"
        "Box 11: _ _ … ” ! n _ _\t[  …”!n  ]\n")
    assert names(code) == {2: "PRo/FQm ", 4: "E FRm", 11: "  …”!n  "}


def test_spaced_and_bracket_forms_must_agree():
    with pytest.raises(ParseError, match="does not match"):
        parse("Box 1: A B C [ABD]")


def test_leave_as_is_and_ranges():
    (code,) = parse("Box 1: A [A]\nBoxes 8 to 13: (leave as is)\nBox 14: (leave as is)")
    assert names(code) == {1: "A", **{n: None for n in range(8, 15)}}
    assert code.plan()[:2] == [(1, "A"), (2, None)]


def test_lookalikes_are_reported_not_silent():
    (code,) = parse("Box 9: – R ! s [–R!s]")
    box = code.boxes[9]
    assert box.name == "-R!s"
    assert any("en dash" in w for w in box.warnings)


def test_ambiguous_quote_is_an_error():
    with pytest.raises(ParseError, match="ambiguous"):
        parse('Box 1: A " B [A"B]')


def test_non_english_character_is_an_error():
    with pytest.raises(ParseError, match="not on the English"):
        parse("Box 1: A » B [A»B]")


def test_all_space_name_is_an_error():
    with pytest.raises(ParseError, match="all-space"):
        parse("Box 1: _ _ [  ]")


def test_too_long_is_an_error():
    with pytest.raises(ParseError):
        parse("Box 1: A B C D E F G H I [ABCDEFGHI]")


def test_trailing_note_after_bracket_is_kept_as_warning():
    (code,) = parse("Box 5: l ” Q o c … ? q [l”Qoc…?q] (change 'l' to ' ' for BX lr)")
    assert code.boxes[5].name == "l”Qoc…?q"
    assert any("change 'l'" in w for w in code.boxes[5].warnings)


def test_sleipnir_notation():
    (code,) = parse("Box 3: A lefty' righty' ... B")
    assert code.boxes[3].name == "A‘’…B"


# ── golden: hex writer codes ───────────────────────────────────────────────

def test_hex_formats_and_ranges():
    (code,) = parse(
        "Box 3: c6e9d7df\n"
        "BOX  6: 96 2F A0 E3\n"
        "Boxes 7-9 00000000\n"
        "Boxes 10-14: 00000000\n")
    assert code.kind == "hex"
    assert code.boxes[6].name == "962FA0E3"
    assert code.boxes[3].name == "C6E9D7DF"
    assert all(code.boxes[n].name == "00000000" for n in (7, 8, 9, 10, 14))
    assert any("not listed" in w for w in code.warnings)
    assert any("Box 14" in w for w in code.warnings)


def test_switch_variant_default_and_off():
    text = "Box 3: E01C0302        (use DC1C0302 for Switch)"
    assert parse(text)[0].boxes[3].name == "DC1C0302"
    assert parse(text, switch=False)[0].boxes[3].name == "E01C0302"


def test_switch_english_variant_with_letter_o_typo():
    text = ("Box 8: 00 50 00 03   (Note use 50 4F 00 03 for non English European GBA "
            "versions. Use DO420003 For Switch English and 80420003 For Switch European)")
    box = parse(text)[0].boxes[8]
    assert box.name == "D0420003"
    assert any("letter O" in w for w in box.warnings)


def test_placeholders_need_filling():
    (code,) = parse("Box 1: 6AB6****     **** = Hex value of the pokemon\n"
                    "Box 2: **0000B7     ** = Lv in Hex")
    assert [b.number for b in code.needs_params()] == [1, 2]
    assert fill(code.boxes[1], {"****": "0197"}) == "6AB60197"
    with pytest.raises(ParseError):
        fill(code.boxes[2], {"**": "G1"})


def test_code_headers_split_codes():
    codes = parse("### CODE 1 ###\nBox 1: A [A]\n### CODE 2 ###\nBox 1: B [B]")
    assert [c.title for c in codes] == ["Code 1", "Code 2"]


def test_box_number_going_down_starts_new_code():
    codes = parse("Box 1: 00000000\nBox 2: 00000000\nBox 1: 11111111")
    assert len(codes) == 2


def test_mixed_formats_is_an_error():
    with pytest.raises(ParseError, match="mixes"):
        parse("Box 1: A [A]\nBox 2: 00000000")


# ── sweeps over the real sources ───────────────────────────────────────────

ALLOWED_FAILURES = re.compile(
    r"not on the English|placeholder|no box lines|wallpaper instruction")


def test_pomeg_hexwriter_box6_spaced_and_bracket_disagree():
    # pomeg frlg-hex-writer shows "_ F o _ _ _ _" but "[ Fo]". A trailing
    # space (byte 00) and an empty cell (FF) are different machine code,
    # so this must be refused, not guessed.
    with pytest.raises(ParseError, match="does not match"):
        parse("Box  6: _ F o _ _ _ _   [ Fo]")


def _check_typeable(codes):
    for code in codes:
        for box in code.boxes.values():
            if box.name is not None:
                assert simulate(plan_name(box.name)) == box.name


def _pomeg_blocks():
    text = (FIX / "pomeg_blocks.txt").read_text()
    return [b.split("\n", 1) for b in text.split("##### ")[1:]]


KNOWN_SOURCE_ERRATA = ["Box  6: _ F o _ _ _ _   [ Fo]"]


@pytest.mark.parametrize("src,body", _pomeg_blocks())
def test_pomeg_block(src, body):
    try:
        codes = parse(body)
    except ParseError as e:
        if "does not match" in str(e):
            assert any(k in body for k in KNOWN_SOURCE_ERRATA), f"{src}: {e}"
            return
        assert ALLOWED_FAILURES.search(str(e)), f"{src}: {e}"
        return
    _check_typeable(codes)


GIST = FIX / "local" / "theocatic_gist.md"   # not redistributed (no license)
needs_gist = pytest.mark.skipif(not GIST.exists(), reason="local-only fixture")


def _gist_blocks():
    if not GIST.exists():
        return []
    parts = GIST.read_text().split("```")
    return [p for p in parts[1::2] if re.search(r"(?im)^\W*box\s*\d", p)]


@needs_gist
@pytest.mark.parametrize("body", _gist_blocks())
def test_gist_block(body):
    try:
        codes = parse(body)
    except ParseError as e:
        assert ALLOWED_FAILURES.search(str(e)), str(e)
        return
    _check_typeable(codes)


@needs_gist
def test_gist_hexwriter_six_codes():
    gist = GIST.read_text()
    block = gist[gist.index("### CODE 1 ###"):gist.index("### CODE 6 ###") + 600]
    codes = parse(block)
    assert [c.title for c in codes] == [f"Code {i}" for i in range(1, 7)]
    assert all(c.kind == "chars" for c in codes)
    assert codes[5].boxes[10].name == "LRnyFRn "
    assert codes[0].boxes[11].name == "  ♀Fwq  "


# ── CodeGenerator raw-data verification ─────────────────────────────────────

def test_codegenerator_raw_data_verifies_every_box():
    (code,) = parse((FIX / "codegenerator_first_ace.txt").read_text())
    assert len(code.raw) == 99
    assert code.boxes[4].name == "A0O?n"      # zero, then capital O
    assert code.boxes[9].name == "-R!s"       # en dash typed as hyphen 0xAE
    assert any("byte for byte" in w for w in code.warnings)
    _check_typeable([code])


def test_codegenerator_raw_data_mismatch_is_refused():
    text = (FIX / "codegenerator_first_ace.txt").read_text()
    # Box 4 as letter O instead of zero: raw data must catch it.
    bad = text.replace("Box  4: A 0 O ? n         [A0O?n]", "Box  4: A O O ? n         [AOO?n]")
    with pytest.raises(ParseError, match="Box 4"):
        parse(bad)


# ── Switch FAQ checks (warnings only) ───────────────────────────────────────

def test_switch_warns_trailing_spaces_in_box_4_8_12():
    (code,) = parse("Box 4: E _ F R m _ _ _ [E FRm   ]\nBox 5: E _ F R m _ _ _ [E FRm   ]")
    assert code.boxes[4].name == "E FRm   "          # never changed
    assert any("last three characters" in w for w in code.boxes[4].warnings)
    assert not any("last three" in w for w in code.boxes[5].warnings)


def test_switch_warns_old_exit_codes():
    (code,) = parse("Box 10: _ F o H I C o r [ FoHICor]\nBox 11: B n [Bn]")
    assert any("Box 10 [ FoHIoor], Box 11 [xn]" in w for w in code.boxes[10].warnings)
    (code,) = parse("Box 11: … o _ _ _ _ _ _ […o      ]")
    assert any("[.o]" in w for w in code.boxes[11].warnings)


def test_no_switch_checks_when_switch_is_off():
    (code,) = parse("Box 4: E _ F R m _ _ _ [E FRm   ]", switch=False)
    assert not code.boxes[4].warnings


def test_codegenerator_switch_output_has_no_switch_warnings():
    (code,) = parse((FIX / "codegenerator_first_ace.txt").read_text())
    assert not any("pomeg FR/LG FAQ" in w for b in code.boxes.values() for w in b.warnings)
