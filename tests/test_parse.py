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


def _gist_blocks():
    parts = (FIX / "theocatic_gist.md").read_text().split("```")
    return [p for p in parts[1::2] if re.search(r"(?im)^\W*box\s*\d", p)]


@pytest.mark.parametrize("body", _gist_blocks())
def test_gist_block(body):
    try:
        codes = parse(body)
    except ParseError as e:
        assert ALLOWED_FAILURES.search(str(e)), str(e)
        return
    _check_typeable(codes)


def test_gist_hexwriter_six_codes():
    gist = (FIX / "theocatic_gist.md").read_text()
    block = gist[gist.index("### CODE 1 ###"):gist.index("### CODE 6 ###") + 600]
    codes = parse(block)
    assert [c.title for c in codes] == [f"Code {i}" for i in range(1, 7)]
    assert all(c.kind == "chars" for c in codes)
    assert codes[5].boxes[10].name == "LRnyFRn "
    assert codes[0].boxes[11].name == "  ♀Fwq  "
