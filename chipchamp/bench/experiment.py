"""Experiment records (SPEC §17.2): the instrument that makes a run autopsyable.

Every claim about the loop — "it finishes a long task", "the cache saved a
resim", "the router handed off before it stalled" — is an empirical claim about
one run, and a run leaves no trace once the REPL scrolls past it. An Experiment
is that trace: one JSON file per run under ``.chipchamp/experiments/``, holding
what was asked, which model answered, what it spent, where its context peaked
and how it ended.

Recording is *passive*. The recorder wraps a loop's event callback and never
feeds anything back, so an experiment measures the same loop the user gets
rather than an instrumented variant of it. That matters here more than usual:
the whole point is to compare runs across models and across waves, and a probe
that changed the run would make every comparison a comparison of probes.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from ..util.ids import deterministic_id
from ..util.jsonio import atomic_write, dump_json, load_json

# how a run ended, most-conclusive first — `outcome` is the one field an
# autopsy always reads, so the vocabulary is small and ordered on purpose
OUTCOMES = ["completed", "answered", "capped", "stalled", "refused",
            "timeout", "error"]


@dataclass
class Experiment:
    """One recorded agent run. JSON-safe end to end (it is the file format)."""

    id: str
    label: str = ""
    task: str = ""
    model: str = ""
    started: float = 0.0
    ended: float = 0.0
    outcome: str = "running"
    metrics: dict = field(default_factory=dict)
    timeline: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def wall_s(self) -> float:
        return max(0.0, (self.ended or time.time()) - self.started)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["wall_s"] = round(self.wall_s, 2)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Experiment":
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


class ExperimentRecorder:
    """Wraps an :class:`AgentLoop`'s event stream and writes an Experiment.

    Attach BEFORE the run and call :meth:`finish` with the loop's return dict::

        rec = ExperimentRecorder(ws.dot, label="wave1-cache", task=prompt)
        rec.attach(loop)
        out = loop.run(prompt)
        rec.finish(out)

    The event callback is chained, not replaced, so the REPL's own rendering
    keeps working while a run is recorded.
    """

    # timeline entries worth keeping verbatim: the rare, decisive ones. Tool
    # calls are counted rather than logged — a 40-step run makes hundreds, and
    # an autopsy that has to scroll is an autopsy nobody reads.
    _TIMELINE = {"model_switch", "error", "security", "oracle_suspect",
                 "tool_calls_recovered", "budget_block", "compacted",
                 "tools_loaded"}

    def __init__(self, dot: str | Path, label: str = "", task: str = "",
                 model: str = "", store: str = "experiments"):
        self.dir = Path(dot) / store
        stamp = f"{time.time():.6f}"
        self.exp = Experiment(
            id=deterministic_id("E", label or "run", task[:200], stamp),
            label=label, task=(task or "")[:2000], model=model,
            started=time.time())
        self._tools: dict[str, list[int]] = {}   # name -> [calls, failed]
        self._jobs: list[dict] = []
        self._tokens_in = self._tokens_out = 0
        self._calls = 0
        self._chars_max = self._msgs_max = 0
        self._switches: list[dict] = []
        self._compactions = 0
        self._freed = 0
        self._reported_done = False
        self._prev: Optional[callable] = None

    # ---- wiring -------------------------------------------------------------

    def attach(self, loop) -> "ExperimentRecorder":
        """Chain onto the loop's callback. Idempotent per loop."""
        self._prev = loop.on_event
        if not self.exp.model:
            self.exp.model = getattr(loop.gateway, "ref", "")

        def chained(kind: str, data: dict) -> None:
            try:
                self.observe(kind, data or {})
            except Exception:
                pass  # a broken probe must never break the run it measures
            if self._prev:
                self._prev(kind, data)

        loop.on_event = chained
        return self

    def observe(self, kind: str, data: dict) -> None:
        if kind == "thinking_start":
            self._calls += 1
            # the loop reports the transcript it is about to send; the peak is
            # the number that says whether a long run can survive its context
            self._chars_max = max(self._chars_max, int(data.get("chars") or 0))
            self._msgs_max = max(self._msgs_max, int(data.get("messages") or 0))
        elif kind == "thinking_done":
            self._tokens_in += int(data.get("input_tokens") or 0)
            self._tokens_out += int(data.get("output_tokens") or 0)
        elif kind == "tool_call":
            slot = self._tools.setdefault(data.get("name", "?"), [0, 0])
            slot[0] += 1
        elif kind == "tool_result":
            name = data.get("name", "?")
            res = data.get("result")
            bad = isinstance(res, dict) and (res.get("error") or res.get("denied"))
            if bad:
                self._tools.setdefault(name, [0, 0])[1] += 1
            if name == "report.done" and isinstance(res, dict) \
                    and res.get("accepted"):
                self._reported_done = True
        elif kind == "job_done":
            res = data.get("result")
            res = res if isinstance(res, dict) else {}
            self._jobs.append({"tool": data.get("name", "?"),
                               "job": res.get("job", ""),
                               "status": res.get("status", "?"),
                               "cached": bool(res.get("cached"))})
        elif kind == "model_switch":
            self._switches.append({k: data.get(k) for k in ("from", "to", "reason")})
        elif kind == "compacted":
            self._compactions += 1
            self._freed += int(data.get("freed") or 0)
        if kind in self._TIMELINE:
            self.exp.timeline.append({"t": round(self.exp.wall_s, 2),
                                      "kind": kind, "data": _small(data)})

    def note(self, msg: str) -> None:
        """A free-text observation from the harness (not the loop)."""
        self.exp.notes.append(msg)

    # ---- closing ------------------------------------------------------------

    def finish(self, result: Optional[dict] = None, *, max_steps: int = 0,
               save: bool = True) -> Experiment:
        result = result or {}
        self.exp.ended = time.time()
        self.exp.model = result.get("model") or self.exp.model
        self.exp.outcome = self._classify(result, max_steps)
        jobs_pass = sum(1 for j in self._jobs if j["status"] == "passed")
        self.exp.metrics = {
            "steps": int(result.get("steps") or 0),
            "wall_s": round(self.exp.wall_s, 2),
            "model_calls": self._calls,
            "tokens_in": self._tokens_in,
            "tokens_out": self._tokens_out,
            "tokens_total": self._tokens_in + self._tokens_out,
            "context_chars_max": self._chars_max,
            "context_messages_max": self._msgs_max,
            "tool_calls": sum(v[0] for v in self._tools.values()),
            "tool_failures": sum(v[1] for v in self._tools.values()),
            "tools": {k: {"calls": v[0], "failed": v[1]}
                      for k, v in sorted(self._tools.items())},
            "jobs": len(self._jobs),
            "jobs_passed": jobs_pass,
            "jobs_cached": sum(1 for j in self._jobs if j["cached"]),
            "job_detail": self._jobs[:60],
            "switches": self._switches,
            "compactions": self._compactions,
            "context_chars_freed": self._freed,
            "pending_jobs": result.get("pending_jobs", []),
            "models_used": result.get("models_used", []),
            "recovered_calls": int(result.get("recovered_calls") or 0),
            "empty_turns": int(result.get("empty_turns") or 0),
            "length_stops": int(result.get("length_stops") or 0),
            "reported_done": self._reported_done,
            "answer_chars": len(result.get("text") or ""),
        }
        if save:
            self.save()
        return self.exp

    def _classify(self, result: dict, max_steps: int) -> str:
        """One word for how the run ended. `completed` is reserved for a run
        that got `report.done` accepted — the platform's own definition of done
        — so an autopsy can never confuse a confident paragraph with evidence."""
        if result.get("timed_out"):
            return "timeout"
        if result.get("error"):
            return "error"
        if self._reported_done:
            return "completed"
        reasons = {s.get("reason") for s in self._switches}
        if "refusal" in reasons and not result.get("text"):
            return "refused"
        steps = int(result.get("steps") or 0)
        if max_steps and steps >= max_steps:
            return "capped"
        if "stall" in reasons or "giveup" in reasons:
            return "stalled"
        return "answered" if result.get("text") else "stalled"

    def save(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir / f"{self.exp.id}.json"
        atomic_write(p, dump_json(self.exp.to_dict()))
        return p


def _small(data: dict, limit: int = 300) -> dict:
    """Timeline payloads are for reading, not replay — clip them."""
    out = {}
    for k, v in (data or {}).items():
        if isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        else:
            s = str(v)
            out[k] = s if len(s) <= limit else s[:limit] + "…"
    return out


# ---- reading them back ------------------------------------------------------

def load_experiments(dot: str | Path, store: str = "experiments",
                     limit: int = 0) -> list[Experiment]:
    """Newest-first. A corrupt file is skipped, never fatal — a half-written
    record from a killed run must not blind the autopsy to the other twenty."""
    d = Path(dot) / store
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("E-*.json"), key=lambda x: x.stat().st_mtime,
                    reverse=True):
        try:
            out.append(Experiment.from_dict(load_json(p)))
        except Exception:
            continue
        if limit and len(out) >= limit:
            break
    return out


def autopsy(exps: list[Experiment]) -> dict:
    """Aggregate a set of runs into the comparison an autopsy actually needs:
    per-model outcome tallies, cost, and which tools failed most.

    Deliberately reports `n` alongside every rate — with three runs per model a
    ratio is an anecdote, and a table that hides that invites over-reading."""
    by_model: dict[str, dict] = {}
    tool_fail: dict[str, int] = {}
    for e in exps:
        m = by_model.setdefault(e.model or "?", {
            "runs": 0, "outcomes": {}, "steps": 0, "wall_s": 0.0,
            "tokens": 0, "jobs": 0, "jobs_passed": 0, "switches": 0,
            "context_chars_max": 0})
        m["runs"] += 1
        m["outcomes"][e.outcome] = m["outcomes"].get(e.outcome, 0) + 1
        mt = e.metrics or {}
        m["steps"] += int(mt.get("steps") or 0)
        m["wall_s"] += float(mt.get("wall_s") or 0)
        m["tokens"] += int(mt.get("tokens_total") or 0)
        m["jobs"] += int(mt.get("jobs") or 0)
        m["jobs_passed"] += int(mt.get("jobs_passed") or 0)
        m["switches"] += len(mt.get("switches") or [])
        m["context_chars_max"] = max(m["context_chars_max"],
                                     int(mt.get("context_chars_max") or 0))
        for name, st in (mt.get("tools") or {}).items():
            if st.get("failed"):
                tool_fail[name] = tool_fail.get(name, 0) + st["failed"]
    for m in by_model.values():
        n = max(1, m["runs"])
        m["avg_steps"] = round(m["steps"] / n, 1)
        m["avg_wall_s"] = round(m["wall_s"] / n, 1)
        m["avg_tokens"] = int(m["tokens"] / n)
        m["completion_rate"] = round(
            m["outcomes"].get("completed", 0) / n, 2)
    return {"runs": len(exps), "by_model": by_model,
            "tool_failures": dict(sorted(tool_fail.items(),
                                         key=lambda kv: -kv[1])[:12])}
