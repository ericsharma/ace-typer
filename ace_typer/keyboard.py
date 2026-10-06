"""Model of the FireRed/LeafGreen (English) naming screen, ported from
pret/pokefirered src/naming_screen.c, plus a planner and a simulator.

State = (page, x, y, saved)
  page  : 0 UPPER, 1 lower, 2 OTHERS (SELECT cycles 0 -> 1 -> 2 -> 0)
  x, y  : cursor; x == column count of the page is the button column
          (y 0 PAGE, 1 BACK, 2 OK)
  saved : tButtonId of Task_HandleInput (row memory for the button column)
"""

import heapq
from dataclasses import dataclass

UPPER, LOWER, OTHERS = 0, 1, 2
PAGE_NAMES = ("UPPER", "lower", "OTHERS")

# None = end-of-string cell; selecting it would insert EOS, never a target.
GRID = {
    UPPER: ["ABCDEF .", "GHIJKL ,", "MNOPQRS", "TUVWXYZ"],
    LOWER: ["abcdef .", "ghijkl ,", "mnopqrs", "tuvwxyz"],
    OTHERS: ["01234", "56789", "!?♂♀/-", "…“”‘’"],
}
COLS = {UPPER: 8, LOWER: 8, OTHERS: 6}
ROWS = 4
BUTTON_COUNT = 3
BUTTON_BACK, BUTTON_OK = 1, 2
KEY_ROW_TO_BUTTON_ROW = (0, 1, 1, 2)
BUTTON_ROW_TO_KEY_ROW = (0, 0, 3)

MAX_CHARS = 8
INITIAL = (UPPER, 0, 0, 0)

CHARSET = {c for rows in GRID.values() for row in rows for c in row}


def char_at(page, x, y):
    if x >= COLS[page]:
        return None
    row = GRID[page][y]
    return row[x] if x < len(row) else None


def dpad(state, dx, dy):
    """HandleDpadMovement()."""
    page, x, y, saved = state
    n = COLS[page]
    prev_x = x
    x += dx
    y += dy
    if x < 0:
        x = n
    if x > n:
        x = 0
    if dx != 0:
        if x == n:
            saved = y
            y = KEY_ROW_TO_BUTTON_ROW[y]
        elif prev_x == n:
            y = saved if y == BUTTON_COUNT // 2 else BUTTON_ROW_TO_KEY_ROW[y]
    if x == n:
        if y < 0:
            y = BUTTON_COUNT - 1
        if y >= BUTTON_COUNT:
            y = 0
        if y == 0:
            saved = BUTTON_BACK
        elif y == BUTTON_COUNT - 1:
            saved = BUTTON_OK
    else:
        if y < 0:
            y = ROWS - 1
        if y >= ROWS:
            y = 0
    return (page, x, y, saved)


def select(state):
    """SwapKeyboardPage() + MainState_WaitPageSwap()."""
    page, x, y, saved = state
    on_last = x == COLS[page]
    page = (page + 1) % 3
    if on_last:
        x = COLS[page]
    elif x >= COLS[page]:
        x = COLS[page] - 1
    return (page, x, y, saved)


def start(state):
    """MoveCursorToOKButton()."""
    page, _, _, saved = state
    return (page, COLS[page], 2, saved)


MOVES = {
    "UP": lambda s: dpad(s, 0, -1),
    "DOWN": lambda s: dpad(s, 0, 1),
    "LEFT": lambda s: dpad(s, -1, 0),
    "RIGHT": lambda s: dpad(s, 1, 0),
    "PAGE": select,     # A on the on-screen PAGE button; same as SELECT
    "SELECT": select,
    "START": start,
}


def on_button(state, row):
    page, x, y, _ = state
    return x == COLS[page] and y == row


@dataclass(frozen=True)
class Costs:
    """Relative cost of each action. Page swaps carry their animation.

    use_meta=False plans with the D-pad and A only (on-screen PAGE and OK):
    on the Switch rerelease, Plus did not act as GBA START (calibration t1).
    """

    move: float = 1.0
    page_swap: float = 4.0
    use_meta: bool = False

    def allowed(self, action, state):
        if action == "PAGE":
            return on_button(state, 0)
        if action in ("SELECT", "START"):
            return self.use_meta
        return True

    def of(self, action):
        return self.page_swap if action in ("PAGE", "SELECT") else self.move


def _search(state, is_goal, costs):
    dist = {state: 0.0}
    prev = {}
    heap = [(0.0, 0, state)]
    tie = 0
    while heap:
        d, _, s = heapq.heappop(heap)
        if d > dist[s]:
            continue
        if is_goal(s):
            end = s
            actions = []
            while s in prev:
                s, a = prev[s]
                actions.append(a)
            return actions[::-1], end
        for a, f in MOVES.items():
            if not costs.allowed(a, s):
                continue
            ns = f(s)
            nd = d + costs.of(a)
            if nd < dist.get(ns, float("inf")):
                dist[ns] = nd
                prev[ns] = (s, a)
                tie += 1
                heapq.heappush(heap, (nd, tie, ns))
    return None


def path_to_char(state, target, costs=Costs()):
    """Cheapest action list from state to a cursor on `target`, plus end state."""
    if target not in CHARSET:
        raise ValueError(f"character {target!r} is not on the keyboard")
    found = _search(state, lambda s: char_at(s[0], s[1], s[2]) == target, costs)
    if found is None:
        raise ValueError(f"unreachable character {target!r}")
    return found


def plan_name(name, costs=Costs()):
    """Actions that type `name` from a freshly opened box-naming screen and
    confirm it: UP/DOWN/LEFT/RIGHT, PAGE (A on the PAGE button), A, and
    SELECT/START only with use_meta. "WAIT_FULL" follows an 8th character
    (the game moves the cursor to OK)."""
    if len(name) > MAX_CHARS:
        raise ValueError(f"{name!r} is longer than {MAX_CHARS} characters")
    if not name.strip(" "):
        raise ValueError("an empty or all-space name is not saved by the game")
    state = INITIAL
    out = []
    for ch in name:
        actions, state = path_to_char(state, ch, costs)
        out.extend(actions)
        out.append("A")
    if len(name) == MAX_CHARS:
        out += ["WAIT_FULL", "A"]
    else:
        actions, state = _search(state, lambda s: on_button(s, 2), costs)
        out += actions + ["A"]
    return out


def simulate(actions):
    """Replay actions through the model; return the confirmed text."""
    state = INITIAL
    buf = []
    for a in actions:
        if a in MOVES:
            state = MOVES[a](state)
        elif a == "WAIT_FULL":
            if len(buf) != MAX_CHARS:
                raise AssertionError("WAIT_FULL with a buffer that is not full")
            state = start(state)
        elif a == "A":
            page, x, y, _ = state
            if on_button(state, 0):
                raise AssertionError("bare A on PAGE; plan it as PAGE")
            if x == COLS[page]:
                if y == 2:
                    return "".join(buf)
                raise AssertionError(f"A on button row {y}")
            ch = char_at(page, x, y)
            if ch is None:
                raise AssertionError(f"A on an empty cell at {state}")
            if len(buf) >= MAX_CHARS:
                raise AssertionError("A on a full buffer")
            buf.append(ch)
        else:
            raise AssertionError(f"unknown action {a}")
    raise AssertionError("name was never confirmed")
