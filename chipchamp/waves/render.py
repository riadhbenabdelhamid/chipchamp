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


def pick_signals(data, limit: int = 8) -> list[str]:
    """Auto-select the signals a hardware engineer looks at first: the
    top-most scope's ports, clock/reset first, then handshakes (valid/ready),
    then payload — capped so the view stays one screenful."""
    paths = [s.path for s in data.signals.values()]
    if not paths:
        return []
    depth = min(p.count(".") for p in paths)
    top = sorted({p for p in paths if p.count(".") == depth},
                 key=lambda p: (_rank(p.rsplit(".", 1)[-1]), p))
    return top[:limit]


def _sample_times(store, paths: list[str], cycles: int) -> tuple[list[int], str]:
    """One column per clock cycle: sample at each rising edge of the clock
    (the analyzer convention). Without a recognizable clock, fall back to
    uniform time steps across the tail of the dump."""
    data = store.data
    for p in paths:
        if _rank(p.rsplit(".", 1)[-1]) == 0:
            code = data.id_for(p)
            rises = [t for t, v in data.changes.get(code, []) if v == "1"]
            if len(rises) >= 2:
                return rises[-cycles:], p.rsplit(".", 1)[-1]
    end = data.end_time or 1
    step = max(1, end // cycles)
    return list(range(max(0, end - cycles * step), end + 1, step)), "time"


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
                  width: int = 100) -> list[str]:
    """Render the tail of a wave dump as an inline logic-analyzer view:
    one column per clock cycle, auto-picked top-level signals, rich-markup
    lines ready for a Console. Returns [] when there is nothing to show."""
    data = store.data
    paths = paths or pick_signals(data)
    if not paths:
        return []
    samples, clkname = _sample_times(store, paths, cycles)
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
    samples = samples[-wave_w:]
    lines = [f"  [dim]⌁ waves · last {len(samples)} cycles of "
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


def ascii_timing(snapshot: dict, cell: int = 4) -> str:
    """Render a snapshot() result as an ASCII timing diagram."""
    win = snapshot.get("window", [0, 0])
    sigs = snapshot.get("signals", {})
    if not sigs:
        return "(no signals in snapshot)"
    t0, t1 = win
    # Build a sampled grid over the window at change points (bounded columns).
    all_edges = sorted({t for s in sigs.values() for t, _ in s["edges"]} | {t0, t1})
    # cap columns
    cols = all_edges[:40]
    if t1 not in cols:
        cols.append(t1)
    lines = []
    name_w = max((len(n) for n in sigs), default=8)
    header = " " * (name_w + 2) + "".join(f"{t:<{cell+4}}" for t in cols[:12])
    lines.append(header.rstrip())
    for path, info in sigs.items():
        width = info["width"]
        row = f"{path:<{name_w}}  "
        cur = info.get("value_at_start")
        edge_map = dict(info["edges"])
        for t in cols[:12]:
            if t in edge_map:
                cur = edge_map[t]
            row += _cellstr(cur, width, cell)
        lines.append(row.rstrip())
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
