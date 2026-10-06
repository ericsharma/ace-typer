from ace_typer.parse import parse
from ace_typer.macro import Timings
from ace_typer.macro import box_and_advance


def test_one_box_then_skip_unchanged_boxes():
    (code,) = parse("Box 1: A [A]\nBoxes 2 to 3: (leave as is)\nBox 4: B [B]")
    t = Timings()
    steps, nxt = box_and_advance(code, 1, t)
    assert nxt == 4
    # after naming Box 1: one RIGHT to Box 2, then two more to reach Box 4
    rights = [b for b, _ in steps[-6:] if b == "RIGHT"]
    assert len(rights) == 3


def test_last_box_does_not_scroll():
    (code,) = parse("Box 1: A [A]\nBox 2: B [B]")
    steps, nxt = box_and_advance(code, 2)
    assert nxt is None
    assert steps[-1][0] is None and steps[-2][0] == "A"


def test_steps_to_macro_merges_waits():
    from ace_typer.macro import steps_to_macro
    steps = [("A", 0.15), (None, 0.25), (None, 1.0), ("PAGE", 0.15), (None, 1.0)]
    assert steps_to_macro(steps).splitlines() == [
        "A 0.150s", "1.250s", "A 0.150s", "1.000s"]
