"""Waveform rendering (SPEC §8.6 FR-WAVE-06): ASCII timing for terminal reports
and WaveJSON for evidence bundles / IDE consumption."""
from __future__ import annotations

import re

# ---- inline logic-analyzer view (terminal showpiece) -------------------------

_RANK = ((0, re.compile(r"clk|clock")), (1, re.compile(r"rst|reset")),
         (2, re.compile(r"valid")), (3, re.compile(r"ready")))


def _rank(name: str) -> int:
    n = name.lower()
    for r, rx in _RANK:
        if rx.search(n):
            return r
    return 4


def _never_toggles(data, path: str) -> bool:
    """Does this signal hold ONE value for the whole trace?

    Filtering on the value history rather than `var_type` is what makes this
    worth doing: an elaboration parameter (DEPTH, WIDTH) is declared
    `parameter`, but a testbench's `errors`/`seed` counters are plain integers
    that merely never move in a passing run. Both are flat lines; only the
    history tells you so.

    A signal stuck at x/z is NOT filtered — that is a defect, and a picker that
    hid it would be hiding exactly what someone opened the waveform to find.
    """
    code = data.id_for(path)
    if code is None:
        return False
    vals = {v for _, v in (data.changes.get(code) or [])}
    if len(vals) > 1:
        return False
    return not any(c in "xzXZ" for c in "".join(vals))


def pick_signals(data, limit: int = 8) -> list[str]:
    """Auto-select the signals a hardware engineer looks at first: the
    top-most scope's ports, clock/reset first, then handshakes (valid/ready),
    then payload — capped so the view stays one screenful.

    Signals that never toggle are dropped. They render as a flat line carrying
    one value, and on the example SoC three of eight rows went to DEPTH, WIDTH
    and errors — a third of the view spent on constants, pushing out the
    handshake signals the bug was actually visible in.
    """
    paths = [s.path for s in data.signals.values()]
    if not paths:
        return []
    depth = min(p.count(".") for p in paths)
    top = sorted({p for p in paths if p.count(".") == depth},
                 key=lambda p: (_rank(p.rsplit(".", 1)[-1]), p))
    live = [p for p in top if not _never_toggles(data, p)]
    # A trace where nothing moves at all is still worth showing: better a view
    # of flat lines than an empty panel that looks like a rendering failure.
    return (live or top)[:limit]


def _sample_times(store, paths: list[str], cycles: int,
                  around: int | None = None) -> tuple[list[int], str]:
    """One column per clock cycle: sample at each rising edge of the clock
    (the analyzer convention). Without a recognizable clock, fall back to
    uniform time steps.

    `around` centres the window on a time instead of taking the tail. A failure
    is almost never in the last cycles — the testbench keeps running after it
    gives up — so a tail view shows the aftermath and quietly invites the
    reader to draw a conclusion about the wrong moment."""
    data = store.data
    for p in paths:
        if _rank(p.rsplit(".", 1)[-1]) == 0:
            code = data.id_for(p)
            changes = data.changes.get(code, [])
            rises = [t for (prior_t, prior), (t, v) in zip(changes, changes[1:])
                     if prior == "0" and v == "1"]
            if len(rises) >= 2:
                name = p
                if around is None:
                    return rises[-cycles:], name
                # centre on the edge nearest `around`, clamped to the trace
                i = min(range(len(rises)), key=lambda k: abs(rises[k] - around))
                lo = max(0, min(i - cycles // 2, len(rises) - cycles))
                return rises[lo:lo + cycles], name
    end = data.end_time
    if end == 0:
        return [0], "time"
    step = max(1, end // cycles)
    if around is None:
        return list(range(max(0, end - cycles * step), end + 1, step)), "time"
    lo = max(0, min(around - (cycles // 2) * step, end - cycles * step))
    return list(range(lo, min(end, lo + cycles * step) + 1, step)), "time"


def _hex(val: str | None) -> str:
    if val is None:
        return "?"
    s = str(val)
    if set(s.lower()) == {"x"}:
        return "x"
    if set(s.lower()) == {"z"}:
        return "z"
    if any(c in "xzXZ" for c in s):
        return "b" + s.lower()
    try:
        return format(int(s, 2), "x")
    except ValueError:
        return s


_CELL = 2  # terminal chars per clock cycle (2 so bus hex stays readable)


def _bit_row(vals: list) -> str:
    """1-bit trace: bright high, dim low, red X — contrast IS the waveform."""
    out = []
    for v in vals:
        if v == "1":
            out.append(f"[cyan]{'▔' * _CELL}[/]")
        elif v == "0":
            out.append(f"[bright_black]{'▁' * _CELL}[/]")
        elif v == "!":
            out.append(f"[yellow]{'!' * _CELL}[/]")
        elif v in ("z", "Z"):
            out.append(f"[magenta]{'Z' * _CELL}[/]")
        else:
            out.append(f"[red]{('X' if v in ('x', 'X') else '?') * _CELL}[/]")
    return "".join(out)


def _bus_row(vals: list, info=None) -> str:
    """Bus trace: run-length hex segments separated by transition marks —
    `a6══╳7e═════╳12`. A label that does not fit becomes an ellipsis rather
    than a partial numeric value."""
    segs: list[tuple[str, int]] = []
    for v in vals:
        from .raster import value_label
        h = value_label(v, info) if info else _hex(v)
        if segs and segs[-1][0] == h:
            segs[-1] = (h, segs[-1][1] + 1)
        else:
            segs.append((h, 1))
    out = []
    for i, (h, length) in enumerate(segs):
        if h == "!":
            out.append(f"[yellow]{'!' * (length * _CELL)}[/]")
            continue
        avail = length * _CELL - (0 if i == 0 else 1)
        if i:
            out.append("[yellow]╳[/]")
        if avail <= 0:
            continue
        body = h if len(h) <= avail else "…"
        pad = "═" * (avail - len(body))
        color = "red" if "x" in h or h == "?" else "magenta" if "z" in h else "white"
        out.append(f"[{color}]{body}[/][bright_black]{pad}[/]")
    return "".join(out)


def analyzer_view(store, paths: list[str] | None = None, cycles: int = 48,
                  width: int = 100, around: int | None = None) -> list[str]:
    """Render the tail of a wave dump as an inline logic-analyzer view:
    one column per clock cycle, auto-picked top-level signals, rich-markup
    lines ready for a Console. Returns [] when there is nothing to show."""
    data = store.data
    paths = paths or pick_signals(data)
    if not paths:
        return []
    samples, clkname = _sample_times(store, paths, cycles, around)
    if not samples:
        return []
    if clkname != "time":
        # columns ARE this clock's rising edges — its own row would render
        # solid high; the title names it instead, freeing a row for payload
        paths = [p for p in paths if p != clkname]
        if not paths:
            return []
    names = {p: p.rsplit(".", 1)[-1] for p in paths}
    leaves = list(names.values())
    names = {p: p if leaves.count(n) > 1 else n for p, n in names.items()}
    display, aliases = _display_names(list(names.values()), 18)
    names = dict(zip(paths, display))
    name_w = max((len(n) for n in names.values()), default=8)
    wave_w = max(1, min(len(samples), (width - name_w - 6) // _CELL))
    if around is None:
        samples = samples[-wave_w:]
    else:
        # keep the moment of interest CENTRED when the terminal forces a trim;
        # trimming from the left would slide it off the edge it was chosen for
        i = min(range(len(samples)), key=lambda k: abs(samples[k] - around))
        lo = max(0, min(i - wave_w // 2, len(samples) - wave_w))
        samples = samples[lo:lo + wave_w]
    when = "around t=%d" % around if around is not None else "last"
    lines = [f"  [dim]⌁ waves · {when} {len(samples)} {'cycles of' if clkname != 'time' else 'samples in'} "
             f"[/][cyan]{clkname}[/][dim] · t {samples[0]}..{samples[-1]} "
             f"{data.timescale}[/]"]
    for p in paths:
        code = data.id_for(p)
        w = data.signals[code].width if code else 1
        vals = [store.value(p, t) for t in samples]
        # A sampled level must not conceal a pulse between two samples. Mark
        # that interval rather than inventing a pulse width from sparse data.
        times = store._times.get(code, [])
        import bisect
        for i, (start, end) in enumerate(zip(samples, samples[1:])):
            lo, hi = bisect.bisect_right(times, start), bisect.bisect_left(times, end)
            prior = store.value(p, start)
            for _, value in data.changes.get(code, [])[lo:hi]:
                if value != prior:
                    vals[i] = "!"
                    break
                prior = value
        info = {"var_type": data.signals[code].var_type} if code else {}
        row = _bit_row(vals) if w == 1 and info.get("var_type") not in ("real", "realtime") else _bus_row(vals, info)
        nm = names[p][:name_w]
        # VCD names carry range suffixes ("data [31:0]") — escape for markup,
        # padding on the raw length so escapes don't skew the alignment
        pad = " " * (name_w - len(nm))
        lines.append(f"  [dim]{pad}{nm.replace('[', chr(92) + '[')}[/] {row}")
    # a dim ruler every 10 cycles keeps long traces countable
    ruler = "".join(("╵" + " " * (_CELL - 1)) if i % 10 == 0 else " " * _CELL
                    for i in range(len(samples)))
    lines.append("  [dim]Sampled view; ! activity between samples; … label omitted. Use wave.snapshot to zoom.[/]")
    from rich.markup import escape
    lines.extend(f"  [dim]{escape(alias)}: {escape(name)}[/]" for name, alias in aliases.items())
    lines.append(f"  [bright_black]{'':>{name_w}} {ruler}[/]")
    return lines


_TS_FACTOR = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9,
              "ps": 1e-12, "fs": 1e-15}


def fmt_time(t: int, timescale: str = "") -> str:
    """`20664500` @1ps → `20.66µs`: engineering units from the VCD's own
    timescale. Without one, raw units with thousands grouping — never the
    eleven-digit run-on that used to be the header."""
    from decimal import Decimal
    m = re.fullmatch(r"(\d+)\s*([a-z]+)", (timescale or "").strip())
    if not m or m.group(2) not in _TS_FACTOR:
        return f"{t:,}"
    sec = Decimal(str(t)) * int(m.group(1)) * Decimal(str(_TS_FACTOR[m.group(2)]))
    for unit, power in (("s", 0), ("ms", -3), ("µs", -6),
                        ("ns", -9), ("ps", -12), ("fs", -15)):
        div = Decimal(10) ** power
        if abs(sec) >= div:
            v = sec / div
            return format(Decimal(f"{v:.4g}").normalize(), "f") + unit
    return "0"


def _trim_names(names: list) -> tuple:
    """Longest common dotted prefix stripped from every name, returned
    separately — hierarchy belongs in the title once, not in every row."""
    if len(names) < 2:
        return "", list(names)
    split = [n.split(".") for n in names]
    common = []
    for parts in zip(*split):
        if len(set(parts)) == 1:
            common.append(parts[0])
        else:
            break
    if not common:
        return "", list(names)
    k = len(".".join(common)) + 1
    return ".".join(common) + ".", [n[k:] for n in names]


def _display_names(names, maximum):
    used, aliases = set(names), {}
    for name in names:
        if len(name) > maximum:
            index = len(aliases) + 1
            alias = f"sig{index}"
            while alias in used:
                index += 1
                alias = f"sig{index}"
            used.add(alias)
            aliases[name] = alias
    return [aliases.get(n, n) for n in names], aliases


def ascii_timing(snapshot: dict, cell: int = 4, width: int = 0) -> str:
    """Render the whole window on a linear time axis, marking compressed events.

    `cell` is retained for caller compatibility; every character now spans the
    same duration, rather than giving every event an equally wide block.
    """
    import textwrap
    from .raster import time_bins, text_row
    win = snapshot.get("window", [0, 0])
    sigs = snapshot.get("signals", {})
    ts = snapshot.get("timescale", "")
    if not sigs:
        return "(no signals in snapshot)"
    t0, t1 = win
    prefix, short = _trim_names(list(sigs))
    width = width or 100
    if width < 16:
        return "view too narrow"[:max(0, width)]
    max_name = max(4, width // 3)
    names, aliases = _display_names(short, max_name)
    name_w = max(len(n) for n in names)
    bins = time_bins(snapshot, max(1, width - name_w - 2))
    columns = len(next(iter(bins.values())))
    lines = [f"t {fmt_time(t0, ts)} … {fmt_time(t1, ts)}"
             f"  (Δ {fmt_time(t1 - t0, ts)})"
             + (f"  ·  {prefix}*" if prefix else "")]
    for (path, info), name in zip(sigs.items(), names):
        lines.append(f"{name:<{name_w}}  " + text_row(bins[path], info))
    notes = ["Linear time; ! multiple/compressed edges; ? unavailable; … label omitted."]
    if any(info.get("omitted_edges") for info in sigs.values()):
        notes.append("Transition limit reached; unavailable intervals are marked ?.")
    if snapshot.get("missing_signals"):
        notes.append("Unresolved/ambiguous signals: " + ", ".join(snapshot["missing_signals"]))
    notes.extend(f"{alias}: {name}" for name, alias in aliases.items())
    # Keep the ruler last, so callers can consistently identify the time axis.
    for note in notes:
        lines.extend(textwrap.wrap(note, width=width, break_long_words=True))
    ruler = [" "] * (name_w + 2 + columns)
    last_end = -1
    for index in (0, columns // 4, columns // 2, 3 * columns // 4, columns - 1):
        offset = (t1 - t0) * index // columns if index < columns - 1 else t1 - t0
        tick = "╵+" + fmt_time(offset, ts)
        pos = name_w + 2 + index
        if pos <= last_end or pos < name_w + 2:
            continue
        if pos + len(tick) > len(ruler):
            tick = "╵"
        ruler[pos:pos + len(tick)] = tick
        last_end = pos + len(tick)
    lines.append("".join(ruler).rstrip())
    lines[:1] = textwrap.wrap(lines[0], width=width, break_long_words=True)
    return "\n".join(lines)


def _cellstr(val, width: int, cell: int) -> str:
    if val is None:
        return "?" * (cell + 4)
    if width == 1:
        if val in ("1",):
            return "‾" * (cell + 4)
        if val in ("0",):
            return "_" * (cell + 4)
        return (val.upper()) * (cell + 4)
    # vector: show hex
    try:
        h = format(int(str(val), 2), "x")
    except ValueError:
        h = val
    return f"={h:<{cell+2}}="[:cell + 4]


def wavejson(snapshot: dict) -> dict:
    """Uniform-time WaveJSON; mixed bins are explicitly unknown, never aliased.

    Metadata retains transition counts, X/Z values and exact bin duration, since
    WaveDrom itself has no glyph for a pulse narrower than one display column.
    """
    from .raster import time_bins, value_label
    sigs = snapshot.get("signals", {})
    t0, t1 = snapshot.get("window", [0, 0])
    bins = time_bins(snapshot, 64)
    columns = 64 if t1 > t0 else 1
    out_signals = []
    for path, info in sigs.items():
        wave = ""
        data = []
        last = object()
        for cell in bins[path]:
            cur = cell["value"]
            if cell["unknown"] or cell["compressed"] or cur is None:
                ch, label = "x", None
            elif info["width"] == 1 and info.get("var_type") not in ("real", "realtime"):
                ch, label = {"1": "1", "0": "0", "z": "z"}.get(str(cur), "x"), None
            else:
                ch, label = "=", value_label(cur, info)
            state = (ch, label)
            wave += "." if state == last else ch
            if ch == "=" and state != last:
                data.append(label)
            last = state
        sig = {"name": path, "wave": wave}
        if data:
            sig["data"] = data
        out_signals.append(sig)
    return {"signal": out_signals, "head": {"text": f"{t0}..{t1} {snapshot.get('timescale', '')}; uniform time bins"},
            "chipchamp": {"window": [t0, t1], "column_duration": [t1 - t0, columns],
                "timescale": snapshot.get("timescale", ""),
                "missing_signals": snapshot.get("missing_signals", []),
                "cells": {path: [{k: c[k] for k in ("transitions", "compressed", "unknown", "states")}
                                  for c in cells] for path, cells in bins.items()}}}
