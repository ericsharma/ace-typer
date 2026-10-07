"""Type a pasted box code into the PC boxes.

  ./run ace_typer.type_code CODE.txt [--code N] [--no-switch] [--dry]
                                     [--controller wired|nxbt] [--port DEV]

The default controller is the wired ESP32-S3 board (see wired.py); run this
on the machine the board's COM port is plugged into, with Pokémon Automation
stopped. --controller nxbt uses the nxbt web app instead.

Start state: PC "Move Pokémon", cursor on the title of Box 1.
Boxes are named in order 1..last listed box; unlisted and "leave as is"
boxes are skipped with one RIGHT tap. The run ends on the last box's title.
"""

import argparse
import sys

from .macro import box_and_advance, build_steps, preview
from .parse import ParseError, parse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="text file with the pasted code ('-' = stdin)")
    ap.add_argument("--code", type=int, default=1, help="which code to type (1-based)")
    ap.add_argument("--no-switch", action="store_true", help="ignore '(use X for Switch)'")
    ap.add_argument("--dry", action="store_true", help="preview only")
    ap.add_argument("--from", dest="first", type=int, default=1,
                    help="start at this box (cursor on its title)")
    ap.add_argument("--to", dest="last", type=int, default=None, help="stop after this box")
    ap.add_argument("--controller", choices=("wired", "nxbt"), default="wired",
                    help="wired = ESP32-S3 board on --port (default); nxbt = nxbt web app")
    ap.add_argument("--port", default=None, help="wired board serial port (default /dev/pa-esp32s3)")
    ap.add_argument("--box", type=int, default=None,
                    help="type only this box, then move to the next box that has a name "
                         "(cursor must start on this box's title)")
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
    if args.box is not None:
        steps, next_box = box_and_advance(code, args.box)
        print(f"Box {args.box}: [{code.boxes[args.box].name}]; start with the cursor on Box {args.box}'s title")
        print(f"ends on Box {next_box}'s title" if next_box else "last box of the code; ends on this box's title")
    else:
        steps = build_steps(code, first=args.first, last=args.last)
        print(f"boxes {args.first}..{args.last or max(code.boxes)}; start with the cursor on Box {args.first}'s title")
    print(f"{len(steps)} steps, {sum(s for _, s in steps):.0f}s")
    if args.dry:
        return
    if args.controller == "wired":
        from .wired import DEFAULT_PORT, WiredController
        c = WiredController(args.port or DEFAULT_PORT)
    else:
        from .send import Controller
        c = Controller()
    try:
        report = c.run(steps)
    finally:
        c.close()
    if report is not None and not report["ok"]:
        sys.exit("a press was not held as planned; check the box names before triggering ACE")
    print("done; compare every box name with the preview before triggering ACE")


if __name__ == "__main__":
    main()
