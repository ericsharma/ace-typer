"""Phase 1 calibration runs. Each test starts with the PC open and the cursor
on the box title of the first box it names.

  t1  Box 1 = "TEST", very slow. Checks the button mapping, the menu order,
      START = Plus, the cursor going back to the title, one-box scroll.
  t2  Box 2 = "Ab 9…’-.", Box 3 = "PRo/FQm " (trailing space). Checks all
      three pages, the space key, the auto move to OK, and the model's
      button-column wraps.
  t3  Boxes 4-7 = "C6E9D7DF" at press/gap 100, 67, 50, 33 ms.
"""

import sys

from .keyboard import plan_name, simulate
from .send import Controller, Timings, box_steps

TESTS = {
    "t1": [("TEST", Timings(press=0.10, gap=0.20))],
    "t2": [("Ab 9…’-.", Timings()), ("PRo/FQm ", Timings())],
    "t3": [("C6E9D7DF", Timings(press=p, gap=p)) for p in (0.10, 0.067, 0.05, 0.033)],
}


def main():
    name = sys.argv[1]
    dry = "--dry" in sys.argv
    steps = []
    for text, timings in TESTS[name]:
        assert simulate(plan_name(text)) == text
        steps += box_steps(text, timings)
    if dry:
        for b, s in steps:
            print(b or "wait", s)
        print(f"total {sum(s for _, s in steps):.1f}s")
        return
    c = Controller()
    try:
        c.run(steps)
    finally:
        c.close()


if __name__ == "__main__":
    main()
