"""Type a pasted box code into the PC boxes.

  ./run ace_typer.type_code CODE.txt [--code N] [--no-switch] [--dry]

Start state: PC "Move Pokémon", cursor on the title of Box 1.
Boxes are named in order 1..last listed box; unlisted and "leave as is"
boxes are skipped with one RIGHT tap. The run ends on the last box's title.
"""

import argparse
import sys

from .keyboard import plan_name, simulate
from .parse import ParseError, parse
from .send import Controller, Timings, box_steps


def build_steps(code, timings=Timings(), first=1, last=None):
    """Steps for boxes first..last; the cursor must start on Box `first`'s title."""
    plan = [(n, name) for n, name in code.plan()
            if n >= first and (last is None or n <= last)]
    if not plan:
        raise ValueError(f"no boxes in range {first}..{last}")
    steps = []
    for i, (n, name) in enumerate(plan):
        is_last = i == len(plan) - 1
        if name is None:
            if not is_last:
                steps += [("RIGHT", timings.press), (None, timings.scroll)]
            continue
        actions = plan_name(name)
        if simulate(actions) != name:
            raise AssertionError(f"box {n}: planner/simulator disagree for {name!r}")
        steps += box_steps(name, timings, next_box=not is_last)
    return steps


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="text file with the pasted code ('-' = stdin)")
    ap.add_argument("--code", type=int, default=1, help="which code to type (1-based)")
    ap.add_argument("--no-switch", action="store_true", help="ignore '(use X for Switch)'")
    ap.add_argument("--dry", action="store_true", help="preview only")
    ap.add_argument("--from", dest="first", type=int, default=1,
                    help="start at this box (cursor on its title)")
    ap.add_argument("--to", dest="last", type=int, default=None, help="stop after this box")
    args = ap.parse_args()

    text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
    try:
        codes = parse(text, switch=not args.no_switch)
    except ParseError as e:
        sys.exit(f"cannot parse: {e}")
    print(f"{len(codes)} code(s) found")
    code = codes[args.code - 1]
    print(preview(code))
    if code.needs_params():
        sys.exit("placeholders are not filled: " +
                 ", ".join(f"Box {b.number} {b.placeholders}" for b in code.needs_params()))
    steps = build_steps(code, first=args.first, last=args.last)
    print(f"boxes {args.first}..{args.last or max(code.boxes)}; start with the cursor on Box {args.first}'s title")
    print(f"{len(steps)} steps, {sum(s for _, s in steps):.0f}s")
    if args.dry:
        return
    c = Controller()
    try:
        c.run(steps)
    finally:
        c.close()
    print("done; compare every box name with the preview before triggering ACE")


if __name__ == "__main__":
    main()
