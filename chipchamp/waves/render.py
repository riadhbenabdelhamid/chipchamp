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
            rises = [t for t, v in data.changes.get(code, []) if v == "1"]
            if len(rises) >= 2:
                name = p.rsplit(".", 1)[-1]
                if around is None:
                    return rises[-cycles:], name
                # centre on the edge nearest `around`, clamped to the trace
                i = min(range(len(rises)), key=lambda k: abs(rises[k] - around))
                lo = max(0, min(i - cycles // 2, len(rises) - cycles))
                return rises[lo:lo + cycles], name
    end = data.end_time or 1
    step = max(1, end // cycles)
    if around is None:
        return list(range(max(0, end - cycles * step), end + 1, step)), "time"
    lo = max(0, min(around - (cycles // 2) * step, end - cycles * step))
    return list(range(lo, min(end, lo + cycles * step) + 1, step)), "time"


def _hex(val: str | None) -> str:
    if val is None:
        return "?"
    s = str(val)
    if any(c in "xzXZ" for c in s):
        return "x"
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
        else:
            out.append(f"[red]{'╳' * _CELL}[/]")
    return "".join(out)


def _bus_row(vals: list) -> str:
    """Bus trace: run-length hex segments separated by transition marks —
    `a6══╳7e═════╳12` — the classic analyzer bus rendering. Labels truncate
    to their segment's width, exactly like a real analyzer."""
    segs: list[tuple[str, int]] = []
    for v in vals:
        h = _hex(v)
        if segs and segs[-1][0] == h:
            segs[-1] = (h, segs[-1][1] + 1)
        else:
            segs.append((h, 1))
    out = []
    for i, (h, length) in enumerate(segs):
        avail = length * _CELL - (0 if i == 0 else 1)
        if i:
            out.append("[yellow]╳[/]")
        if avail <= 0:
            continue
        body = h[:avail]
        pad = "═" * (avail - len(body))
        color = "red" if h == "x" else "white"
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
        paths = [p for p in paths if p.rsplit(".", 1)[-1] != clkname]
        if not paths:
            return []
    names = {p: p.rsplit(".", 1)[-1] for p in paths}
    name_w = min(max((len(n) for n in names.values()), default=8), 18)
    wave_w = max(8, min(len(samples), (width - name_w - 6) // _CELL))
    if around is None:
        samples = samples[-wave_w:]
    else:
        # keep the moment of interest CENTRED when the terminal forces a trim;
        # trimming from the left would slide it off the edge it was chosen for
        i = min(range(len(samples)), key=lambda k: abs(samples[k] - around))
        lo = max(0, min(i - wave_w // 2, len(samples) - wave_w))
        samples = samples[lo:lo + wave_w]
    when = "around t=%d" % around if around is not None else "last"
    lines = [f"  [dim]⌁ waves · {when} {len(samples)} cycles of "
             f"[/][cyan]{clkname}[/][dim] · t {samples[0]}..{samples[-1]} "
             f"{data.timescale}[/]"]
    for p in paths:
        code = data.id_for(p)
        w = data.signals[code].width if code else 1
        vals = [store.value(p, t) for t in samples]
        row = _bit_row(vals) if w == 1 else _bus_row(vals)
        nm = names[p][:name_w]
        # VCD names carry range suffixes ("data [31:0]") — escape for markup,
        # padding on the raw length so escapes don't skew the alignment
        pad = " " * (name_w - len(nm))
        lines.append(f"  [dim]{pad}{nm.replace('[', chr(92) + '[')}[/] {row}")
    # a dim ruler every 10 cycles keeps long traces countable
    ruler = "".join(("╵" + " " * (_CELL - 1)) if i % 10 == 0 else " " * _CELL
                    for i in range(len(samples)))
    lines.append(f"  [bright_black]{'':>{name_w}} {ruler}[/]")
    return lines


_TS_FACTOR = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9,
              "ps": 1e-12, "fs": 1e-15}


def fmt_time(t: int, timescale: str = "") -> str:
    """`20664500` @1ps → `20.66µs`: engineering units from the VCD's own
    timescale. Without one, raw units with thousands grouping — never the
    eleven-digit run-on that used to be the header."""
    import re as _re
    m = _re.match(r"(\d+)\s*([a-z]+)", (timescale or "").strip())
    if not m or m.group(2) not in _TS_FACTOR:
        return f"{t:,}"
    sec = t * int(m.group(1)) * _TS_FACTOR[m.group(2)]
    for unit, div in (("s", 1.0), ("ms", 1e-3), ("µs", 1e-6),
                      ("ns", 1e-9), ("ps", 1e-15 * 1000)):
        if abs(sec) >= div:
            v = sec / div
            return f"{v:.4g}{unit}"
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


def ascii_timing(snapshot: dict, cell: int = 4, width: int = 0) -> str:
    """Render a snapshot() result as an ASCII timing diagram.

    Title carries the window in real time units and the common hierarchy
    prefix; rows carry trimmed names; a tick ruler underneath gives every
    third column's offset from the window start."""
    win = snapshot.get("window", [0, 0])
    sigs = snapshot.get("signals", {})
    ts = snapshot.get("timescale", "")
    if not sigs:
        return "(no signals in snapshot)"
    t0, t1 = win
    all_edges = sorted({t for s in sigs.values() for t, _ in s["edges"]} | {t0, t1})
    prefix, short = _trim_names(list(sigs))
    name_w = max((len(n) for n in short), default=8)
    cw = cell + 4
    max_cols = max(4, ((width or 100) - name_w - 2) // cw)
    cols = all_edges[:max_cols]
    if t1 not in cols and len(cols) < max_cols:
        cols.append(t1)
    lines = [f"t {fmt_time(t0, ts)} … {fmt_time(t1, ts)}"
             f"  (Δ {fmt_time(t1 - t0, ts)})"
             + (f"  ·  {prefix}*" if prefix else "")]
    for (path, info), name in zip(sigs.items(), short):
        w = info["width"]
        row = f"{name:<{name_w}}  "
        cur = info.get("value_at_start")
        edge_map = dict(info["edges"])
        for t in cols:
            if t in edge_map:
                cur = edge_map[t]
            row += _cellstr(cur, w, cell)
        lines.append(row.rstrip())
    ruler = [" "] * (name_w + 2 + cw * len(cols))
    for i, t in enumerate(cols):
        if i % 3 == 0:
            tick = "╵+" + fmt_time(t - t0, ts)
            pos = name_w + 2 + i * cw
            for j, ch in enumerate(tick):
                if pos + j < len(ruler):
                    ruler[pos + j] = ch
    lines.append("".join(ruler).rstrip())
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
    """Minimal WaveJSON (wavedrom) for a snapshot — used in evidence bundles."""
    sigs = snapshot.get("signals", {})
    t0, t1 = snapshot.get("window", [0, 0])
    all_edges = sorted({t for s in sigs.values() for t, _ in s["edges"]} | {t0, t1})
    cols = all_edges[:64]
    out_signals = []
    for path, info in sigs.items():
        wave = ""
        data = []
        cur = info.get("value_at_start")
        edge_map = dict(info["edges"])
        last = None
        for t in cols:
            if t in edge_map:
                cur = edge_map[t]
            if info["width"] == 1:
                ch = {"1": "1", "0": "0"}.get(str(cur), "x")
                wave += ch if ch != (last or "") else "."
                last = ch
            else:
                if cur != last:
                    wave += "="
                    try:
                        data.append(format(int(str(cur), 2), "x"))
                    except ValueError:
                        data.append(str(cur))
                    last = cur
                else:
                    wave += "."
        sig = {"name": path.split(".")[-1], "wave": wave}
        if data:
            sig["data"] = data
        out_signals.append(sig)
    return {"signal": out_signals, "head": {"text": f"{t0}..{t1}"}}
