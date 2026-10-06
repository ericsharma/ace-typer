"""JSON-friendly entry points for a web UI (used by the nxbt web app).

preview(text)  -> what the parser understood, box by box, with warnings
compile(text)  -> the nxbt macro that types it, with its length

Both return plain dicts and never raise for bad input: errors come back as
{"ok": False, "error": "..."} so the page can show them.
"""

from .macro import Timings, build_steps, steps_to_macro
from .parse import BOX_COUNT, ParseError, fill, parse


def _box_rows(code):
    rows = []
    for n in range(1, max(code.boxes) + 1):
        box = code.boxes.get(n)
        if box is None:
            rows.append({"box": n, "action": "unchanged", "name": None, "warnings": []})
        elif box.placeholders:
            rows.append({"box": n, "action": "fill", "name": None,
                         "template": box.placeholders, "note": box.note,
                         "warnings": box.warnings})
        elif box.name is None:
            rows.append({"box": n, "action": "leave", "name": None, "warnings": box.warnings})
        else:
            rows.append({"box": n, "action": "type", "name": box.name, "warnings": box.warnings})
    return rows


def preview(text, switch=True):
    try:
        codes = parse(text, switch=switch)
    except ParseError as e:
        return {"ok": False, "error": str(e)}
    out = []
    for code in codes:
        typed = [n for n, b in code.boxes.items() if b.name is not None]
        out.append({
            "title": code.title,
            "kind": code.kind,
            "boxes": _box_rows(code),
            "warnings": code.warnings,
            "raw_verified": code.raw is not None,
            "first_box": min(typed) if typed else None,
            "last_box": max(code.boxes),
        })
    return {"ok": True, "codes": out}


def compile(text, code=1, first=None, switch=True, params=None):
    """Macro for code number `code` (1-based), from box `first` (default:
    its first box with a name) to its last box. `params` fills hex
    placeholders: {"<box>": {"****": "0197", ...}}."""
    try:
        codes = parse(text, switch=switch)
        if not 1 <= code <= len(codes):
            return {"ok": False, "error": f"there is no code {code} (found {len(codes)})"}
        c = codes[code - 1]
        for box in c.needs_params():
            values = (params or {}).get(str(box.number))
            if not values:
                return {"ok": False, "error": f"Box {box.number} needs values for {box.placeholders}"}
            fill(box, values)
        typed = sorted(n for n, b in c.boxes.items() if b.name is not None)
        if not typed:
            return {"ok": False, "error": "this code has no box names to type"}
        start = first or typed[0]
        if not 1 <= start <= BOX_COUNT:
            return {"ok": False, "error": f"start box {start} is outside 1-{BOX_COUNT}"}
        timings = Timings()
        steps = build_steps(c, timings, first=start)
    except (ParseError, ValueError, AssertionError) as e:
        return {"ok": False, "error": str(e)}
    seconds = sum(s for _, s in steps)
    return {
        "ok": True,
        "macro": steps_to_macro(steps),
        "seconds": round(seconds, 1),
        "presses": sum(1 for b, _ in steps if b is not None),
        "start_box": start,
        "end_box": max(c.boxes),  # skipped boxes are scrolled through
        "names": {str(n): c.boxes[n].name for n in typed if n >= start},
    }
