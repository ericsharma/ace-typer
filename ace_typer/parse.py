"""Parse pasted FireRed/LeafGreen box-name codes into box names.

Two formats are accepted:

* Character codes (pomeg, CodeGenerator, Theocatic):
      Box  2: P R o / F Q m _ [PRo/FQm ]
  One token per character, "_" is a space, a short name has fewer tokens.
  The [...] part is the same name; when present it must match exactly.

* Hex Writer codes (Theocatic):
      Box 3: C6E9D7DF          BOX 6: 96 2F A0 E3
      Boxes 10-14: 00000000    Box 3: E01C0302  (use DC1C0302 for Switch)
  8 hex characters per box. *, &, %, $ are placeholders that must be filled.

Lines that are not box lines (titles, notes, fences) are ignored. "Code N"
headers, or a box number that goes back down, start a new code.

Nothing is "corrected" silently: every normalisation is reported as a
warning, and anything that cannot be typed exactly is an error.
"""

import re
from dataclasses import dataclass, field

from .keyboard import BOX_NAME_BYTES, CHARSET, MAX_CHARS, encode_box_name

BOX_COUNT = 14
PLACEHOLDER_CHARS = "*&%$"

# Characters people paste that stand for a keyboard character.
# value: (replacement, warning)
LOOKALIKES = {
    "–": ("-", "en dash '–' typed as the hyphen '-' (the only dash on the keyboard)"),
    "—": ("-", "em dash '—' typed as the hyphen '-' (the only dash on the keyboard)"),
    "‐": ("-", "hyphen '‐' (U+2010) typed as '-'"),
    "'": ("’", "straight ' typed as ’ (the game encodes ' as ’); check the code did not mean ‘"),
}
# Characters that look like keyboard characters but are ambiguous or absent.
AMBIGUOUS = {
    '"': "straight \" is ambiguous: use “ (left) or ” (right)",
    "‥": "‥ (two-dot ellipsis) is not on the English keyboard; did the code mean … ?",
    "`": "` is not on the keyboard; did the code mean ‘ ?",
    "*": "'*' marks a placeholder: this code is a template; replace * with the characters its notes give",
}

SLEIPNIR = [
    (re.compile(r"lefty\s*'", re.I), "‘"),
    (re.compile(r"righty\s*'", re.I), "’"),
    (re.compile(r'lefty\s*"', re.I), "“"),
    (re.compile(r'righty\s*"', re.I), "”"),
]

BOX_LINE = re.compile(
    r"^\s*box(?:es)?\s*(\d{1,2})(?:\s*(?:-|–|to)\s*(\d{1,2}))?\s*:?(.*)$", re.I)
RAW_HEADER = re.compile(r"^\s*raw data\b", re.I)
HEX_PAIRS = re.compile(r"^\s*(?:[0-9A-Fa-f]{2}\s+)*[0-9A-Fa-f]{2}\s*$")
CODE_HEADER = re.compile(r"^\s*#*\s*code\s*(\d+)\b", re.I)
LEAVE = re.compile(r"^\s*\(?\s*leave\s+(?:it\s+)?as\s+is\s*\)?\s*$", re.I)
BRACKET = re.compile(r"^(.*?)\s*\[(.*)\]\s*(\(.*\))?\s*$")
SWITCH_ALT = re.compile(
    r"use\s+((?:[0-9A-Fa-fO]{2}\s?){3}[0-9A-Fa-fO]{2})\s+for\s+switch(\s+english)?(\s+european)?",
    re.I)


class ParseError(ValueError):
    pass


@dataclass
class Box:
    number: int
    name: str | None            # None = leave this box as is
    line: str                   # source line, for the preview
    warnings: list[str] = field(default_factory=list)
    placeholders: str = ""      # hex template with placeholders, if any
    note: str = ""              # text after the value (placeholder help)


@dataclass
class Code:
    title: str
    kind: str                   # "chars" or "hex"
    boxes: dict[int, Box] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    raw: bytes | None = None    # CodeGenerator "Raw data", boxes from Box 1

    def needs_params(self):
        return [b for b in self.boxes.values() if b.placeholders]

    def plan(self):
        """(box number, name or None) for boxes 1..last listed, in order."""
        last = max(self.boxes)
        return [(n, self.boxes[n].name if n in self.boxes else None)
                for n in range(1, last + 1)]


def _sleipnir(value, warnings):
    for pattern, repl in SLEIPNIR:
        if pattern.search(value):
            warnings.append(f"Sleipnir notation {pattern.pattern!r} read as {repl}")
            value = pattern.sub(repl, value)
    return value


def _char_name(value, line_no, warnings):
    """Character-code value -> exact name."""
    m = BRACKET.match(value)
    tokens_part, bracket = (m.group(1), m.group(2)) if m else (value, None)
    if m and m.group(3):
        warnings.append(f"code note: {m.group(3)}")
    tokens = tokens_part.split()
    out = []
    for tok in tokens:
        if tok == "...":
            warnings.append("'...' read as the ellipsis character …")
            tok = "…"
        if len(tok) != 1:
            raise ParseError(f"line {line_no}: token {tok!r} is not one character")
        out.append(" " if tok == "_" else tok)
    name = "".join(out)
    if bracket is not None:
        if not tokens:
            name = bracket
            warnings.append("only the [...] form was given; trailing spaces cannot be cross-checked")
        elif bracket != name:
            raise ParseError(
                f"line {line_no}: spaced form {name!r} does not match bracket form {bracket!r}")
    else:
        warnings.append("no [...] form to cross-check against")
    return name


def _clean_chars(name, line_no, warnings):
    fixed = []
    for ch in name:
        if ch in AMBIGUOUS:
            raise ParseError(f"line {line_no}: {AMBIGUOUS[ch]}")
        if ch in LOOKALIKES:
            repl, why = LOOKALIKES[ch]
            warnings.append(why)
            ch = repl
        if ch != " " and ch not in CHARSET:
            raise ParseError(f"line {line_no}: {ch!r} is not on the English box-name keyboard")
        fixed.append(ch)
    name = "".join(fixed)
    if len(name) > MAX_CHARS:
        raise ParseError(f"line {line_no}: {name!r} has {len(name)} characters (max {MAX_CHARS})")
    if not name.strip(" "):
        raise ParseError(f"line {line_no}: an empty or all-space box name is not saved by the game")
    return name


def _hex_value(value, line_no, switch, warnings):
    """Hex-writer value -> (8-char template, note). None if not hex."""
    alts = SWITCH_ALT.findall(value)
    s = value.strip()
    collected = []
    i = 0
    while i < len(s) and len(collected) < 8:
        c = s[i]
        if c in "0123456789abcdefABCDEF" + PLACEHOLDER_CHARS:
            collected.append(c.upper())
        elif c not in " \t":
            break
        i += 1
    if len(collected) != 8:
        return None
    rest = s[i:].strip()
    if rest and rest[0] in "0123456789abcdefABCDEF":
        raise ParseError(f"line {line_no}: more than 8 hex characters in {value.strip()!r}")
    template = "".join(collected)
    if alts and switch:
        english = [a for a in alts if a[1] and not a[2]]
        plain = [a for a in alts if not a[1] and not a[2]]
        pick = (english or plain or [None])[0]
        if pick:
            raw = pick[0].replace(" ", "")
            if "O" in raw.upper():
                warnings.append(f"Switch value {raw!r} contains the letter O; read as zero")
                raw = raw.upper().replace("O", "0")
            warnings.append(f"Switch variant used: {template} -> {raw.upper()}")
            template = raw.upper()
    elif alts:
        warnings.append("a Switch variant exists for this box; the non-Switch value was used")
    return template, rest


def parse(text, switch=True):
    """Parse pasted text into a list of Codes. `switch` picks the
    "(use X for Switch)" variants."""
    codes = []
    current = None
    pending_title = None

    def new_code(title):
        nonlocal current
        current = Code(title=title, kind="")
        codes.append(current)

    raw_target = None   # code that the following "Raw data" lines belong to
    for line_no, raw in enumerate(text.splitlines(), 1):
        # Markdown wrappers: inline code, quotes, list bullets.
        line = raw.strip().strip("`").lstrip(">-* ").strip()
        if RAW_HEADER.match(line):
            if current is None:
                raise ParseError(f"line {line_no}: raw data before any box line")
            raw_target = current
            raw_target.raw = b""
            continue
        if raw_target is not None:
            if HEX_PAIRS.match(line):
                raw_target.raw += bytes.fromhex(line)
                continue
            raw_target = None
        h = CODE_HEADER.match(line)
        if h:
            pending_title = f"Code {h.group(1)}"
            current = None
            continue
        m = BOX_LINE.match(line)
        if not m:
            continue
        first = int(m.group(1))
        last = int(m.group(2) or first)
        value = m.group(3)
        if not (1 <= first <= last <= BOX_COUNT):
            raise ParseError(f"line {line_no}: box range {first}-{last} is outside 1-{BOX_COUNT}")
        if current is None or (current.boxes and first <= max(current.boxes)):
            new_code(pending_title or f"Code {len(codes) + 1}")
            pending_title = None

        warnings = []
        if LEAVE.match(value) or not value.strip():
            for n in range(first, last + 1):
                current.boxes[n] = Box(n, None, line.strip(), warnings)
            continue

        if "→" in value:
            raise ParseError(
                f"line {line_no}: {line.strip()!r} looks like a wallpaper instruction, "
                "not a box name; set wallpapers by hand and remove this line")
        value = _sleipnir(value, warnings)
        kind, name, note, template = None, None, "", ""
        tokens = BRACKET.match(value).group(1).split() if BRACKET.match(value) else value.split()
        is_chars = BRACKET.match(value) is not None or (tokens and all(len(t) == 1 or t == "..." for t in tokens))
        if not is_chars:
            hx = _hex_value(value, line_no, switch, warnings)
            if hx is None:
                raise ParseError(f"line {line_no}: cannot read {value.strip()!r} as a box name or 8 hex characters")
            template, note = hx
            kind = "hex"
            name = None if any(c in PLACEHOLDER_CHARS for c in template) else template
        else:
            kind = "chars"
            name = _clean_chars(_char_name(value, line_no, warnings), line_no, warnings)

        if current.kind and current.kind != kind:
            raise ParseError(f"line {line_no}: mixes character and hex boxes in one code")
        current.kind = kind
        for n in range(first, last + 1):
            if n in current.boxes:
                raise ParseError(f"line {line_no}: box {n} is listed twice")
            box = Box(n, name, line.strip(), list(warnings), note=note)
            if kind == "hex" and name is None:
                box.placeholders = template
            current.boxes[n] = box

    codes = [c for c in codes if c.boxes]
    if not codes:
        raise ParseError("no box lines found")
    for c in codes:
        if c.raw is not None:
            verify_raw(c)
        listed = set(c.boxes)
        if c.kind == "hex":
            missing = [n for n in range(1, BOX_COUNT + 1) if n not in listed]
            if missing:
                c.warnings.append(
                    f"boxes {missing} are not listed; the Hex Writer will read their current names")
        if 14 in c.boxes and c.boxes[14].name is not None:
            c.warnings.append("this code renames Box 14, which replaces a Box 14 exit code")
    return codes


def fill(box: Box, values: dict[str, str]):
    """Fill a hex placeholder template. `values` maps each placeholder run
    (e.g. "****", "**", "&&") in order of appearance to hex digits."""
    out = box.placeholders
    for run in re.findall(r"[*&%$]+", box.placeholders):
        v = values.get(run)
        if v is None or not re.fullmatch(r"[0-9A-Fa-f]+", v) or len(v) != len(run):
            raise ParseError(f"box {box.number}: {run} needs {len(run)} hex digits")
        out = out.replace(run, v.upper(), 1)
    box.name = out
    box.placeholders = ""
    return out


def verify_raw(code):
    """Check every box name against CodeGenerator's raw bytes: box n is
    bytes 9*(n-1) .. 9*n-1. Raises ParseError on any difference."""
    raw = code.raw
    covered = len(raw) // BOX_NAME_BYTES
    if not covered:
        raise ParseError("raw data is shorter than one box name")
    for n in range(1, covered + 1):
        expected = raw[(n - 1) * BOX_NAME_BYTES:n * BOX_NAME_BYTES]
        box = code.boxes.get(n)
        if box is None or box.name is None:
            if expected != bytes([0xFF]) * BOX_NAME_BYTES:
                raise ParseError(f"raw data has bytes for Box {n}, but the code does not list Box {n}")
            continue
        got = encode_box_name(box.name)
        if got != expected:
            raise ParseError(
                f"Box {n} [{box.name}] encodes to {got.hex(' ').upper()}, "
                f"raw data says {expected.hex(' ').upper()}")
    tail = raw[covered * BOX_NAME_BYTES:]
    code.warnings.append(
        f"all {covered} box names match CodeGenerator's raw data byte for byte"
        + (f" ({len(tail)} trailing raw bytes not part of a full box name)" if tail else ""))
