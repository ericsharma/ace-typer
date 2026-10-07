"""Turn parsed box codes into nxbt macros. No network code here, so the
nxbt web app can import it."""

from dataclasses import dataclass

from .keyboard import plan_name, simulate

# Planner action -> Switch button.
BUTTON = {
    "UP": "DPAD_UP", "DOWN": "DPAD_DOWN", "LEFT": "DPAD_LEFT", "RIGHT": "DPAD_RIGHT",
    "A": "A", "PAGE": "A",
}


@dataclass
class Timings:
    # Hold well under the naming screen's key-repeat delay (16 frames =
    # 267 ms, naming_screen.c): 250 ms plus Bluetooth jitter repeated D-pad
    # presses. The gap keeps presses from merging (0.10/0.10 lost some;
    # 0.15/0.25 still lost some, so the gap was raised to make each
    # press+gap cycle 30% longer: 400 ms -> 520 ms).
    press: float = 0.15        # button held
    gap: float = 0.37          # release after a button
    menu_open: float = 1.5     # box title A -> JUMP/WALLPAPER/NAME/CANCEL menu
    naming_open: float = 2.5   # NAME -> naming screen faded in
    page_swap: float = 1.0     # PAGE -> next page usable
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
        elif a == "PAGE":
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


def box_segments(code, timings=Timings(), first=1, last=None):
    """[(box number, steps)] for boxes first..last; the cursor must start on
    Box `first`'s title. A skipped box is one RIGHT tap; the last box does not
    scroll on."""
    plan = [(n, name) for n, name in code.plan()
            if n >= first and (last is None or n <= last)]
    if not plan:
        raise ValueError(f"no boxes in range {first}..{last}")
    segments = []
    for i, (n, name) in enumerate(plan):
        is_last = i == len(plan) - 1
        if name is None:
            if not is_last:
                segments.append((n, [("RIGHT", timings.press), (None, timings.scroll)]))
            continue
        actions = plan_name(name)
        if simulate(actions) != name:
            raise AssertionError(f"box {n}: planner/simulator disagree for {name!r}")
        segments.append((n, box_steps(name, timings, next_box=not is_last)))
    return segments


def build_steps(code, timings=Timings(), first=1, last=None):
    """Steps for boxes first..last; the cursor must start on Box `first`'s title."""
    return [step for _, steps in box_segments(code, timings, first, last) for step in steps]


def box_and_advance(code, n, timings=Timings()):
    """Steps that name Box n, then scroll right to the next box that has a
    name to type. Returns (steps, next box number or None)."""
    box = code.boxes.get(n)
    if box is None or box.name is None:
        raise ValueError(f"Box {n} has no name to type in this code")
    actions = plan_name(box.name)
    if simulate(actions) != box.name:
        raise AssertionError(f"box {n}: planner/simulator disagree for {box.name!r}")
    later = [m for m, name in code.plan() if m > n and name is not None]
    if not later:
        return box_steps(box.name, timings, next_box=False), None
    steps = box_steps(box.name, timings, next_box=True)
    for _ in range(later[0] - n - 1):
        steps += [("RIGHT", timings.press), (None, timings.scroll)]
    return steps, later[0]


def preview(code):
    lines = [f"{code.title} ({code.kind})"]
    for n in range(1, max(code.boxes) + 1):
        box = code.boxes.get(n)
        if box is None or box.name is None:
            shown = "(unchanged)" if box is None else "(leave as is)"
        else:
            shown = f"[{box.name}]"
        lines.append(f"  Box {n:2}: {shown}")
        for w in (box.warnings if box else []):
            lines.append(f"           ! {w}")
    for w in code.warnings:
        lines.append(f"  ! {w}")
    return "\n".join(lines)
