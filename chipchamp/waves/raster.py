"""Uniform time bins that retain transitions instead of sampling away pulses."""
from __future__ import annotations


def value_label(value, info):
    if value is None:
        return "?"
    text = str(value)
    if info.get("var_type") in ("real", "realtime", "string"):
        return text
    if set(text.lower()) == {"x"}:
        return "x"
    if set(text.lower()) == {"z"}:
        return "z"
    if any(c in text.lower() for c in "xz"):
        return "b" + text.lower()
    try:
        return format(int(text, 2), "x")
    except ValueError:
        return text


def time_bins(snapshot, columns):
    """Each cell covers an equal interval; the final cell includes the end tick.

    Integer arithmetic preserves edge positions even for traces beyond 2**53.
    Multiple events at one tick are retained, including a pulse that returns
    to its original value. Unknown/truncated intervals must not look stable.
    """
    t0, t1 = snapshot.get("window", [0, 0])
    if t1 < t0:
        raise ValueError("waveform window end precedes its start")
    columns = max(1, columns) if t1 > t0 else 1
    span = t1 - t0
    result = {}
    for path, info in snapshot.get("signals", {}).items():
        cells = [{"events": []} for _ in range(columns)]
        for t, value in info.get("edges", []):
            if t0 <= t <= t1:
                col = min(columns - 1, (t - t0) * columns // span) if span else 0
                cells[col]["events"].append((t, value))
        current = info.get("value_before_start", info.get("value_at_start"))
        cutoff = info.get("truncated_at")
        trace_end = snapshot.get("trace_end", t1)
        for i, cell in enumerate(cells):
            count, interior = 0, False
            start_value = current
            values = []
            for t, value in cell["events"]:
                if current is not None and value != current:
                    count += 1
                    interior |= (t - t0) * columns != i * span
                current = value
                values.append(value)
            if start_value is not None:
                values.append(start_value)
            # At a bin boundary a transition can be drawn exactly; an event
            # inside it is quantized, and two or more cannot look like a level.
            unknown = ((cutoff is not None and ((cutoff - t0) * columns < (i + 1) * span or
                                               (i == columns - 1 and cutoff <= t1)))
                       or (trace_end - t0) * columns < (i + 1) * span)
            if start_value is None:
                unknown |= not cell["events"] or (cell["events"][0][0] - t0) * columns > i * span
            cell.update(value=current, transitions=count, interior=interior,
                        start_value=start_value, unknown=unknown,
                        compressed=count > 1 or interior,
                        states=sorted({str(v).lower() for v in values}))
        result[path] = cells
    return result


def text_row(cells, info):
    """One terminal character per bin; incomplete numeric labels use an ellipsis."""
    if info["width"] == 1 and info.get("var_type") not in ("real", "realtime"):
        out = []
        for cell in cells:
            if cell["unknown"] or cell["value"] is None:
                ch = "?"
            elif cell["transitions"] > 1:
                ch = "!"
            elif cell["transitions"]:
                ch = "╱" if cell["value"] == "1" else "╲" if cell["value"] == "0" else str(cell["value"]).upper()
            else:
                ch = {"0": "▁", "1": "▔", "x": "X", "z": "Z"}.get(str(cell["value"]), "?")
            out.append(ch)
        return "".join(out)
    out, i = [], 0
    while i < len(cells):
        cell = cells[i]
        if cell["unknown"] or cell["value"] is None:
            out.append("?")
            i += 1
            continue
        if cell["compressed"]:
            out.append("!")
            i += 1
            continue
        end = i + 1
        while end < len(cells) and not (cells[end]["unknown"] or cells[end]["compressed"] or
                                       cells[end]["transitions"] or cells[end]["value"] != cell["value"]):
            end += 1
        prefix = "╳" if cell["transitions"] else ""
        label = value_label(cell["value"], info)
        room = end - i - len(prefix)
        if room == 0:
            out.append(prefix)
        else:
            body = label if len(label) <= room else "…"
            out.append(prefix + body + "═" * (room - len(body)))
        i = end
    return "".join(out)
