"""Content-addressed job reuse (SPEC §8.5, FR-JOB-01's other half).

Every job already records an ``inputs_hash`` so ``chipchamp repro`` can prove a
replay ran against the same inputs. That fingerprint answers a second question
for free, which nothing was asking: *has this exact job already been run?*

Simulation is the most expensive thing in the building — licensed seats, CPU
hours, and an agent's wall-clock patience. Re-running a lint over bytes that
have not changed since the last lint is pure waste, and an agent iterating on
one file re-submits its neighbours constantly.

The key is deliberately conservative. It covers the input manifest's *contents*
(hash_manifest hashes bytes, not names), the exact argv of every step, the seed,
the defines, the env modules, the adapter AND its version string. Anything that
could change the answer is in the key, so a hit means the tool would have
recomputed the same result — which is the same claim ``repro`` already makes.

The governing rule is that **a cache hit must leave the disk indistinguishable
from a re-run**. A job's verdict is not its only output: a synthesis writes a
mapped netlist, a coverage run writes a database, and the next step reads those
files by path. Skipping the run skips the files, and a downstream tool then
reads nothing and reports zero — which is worse than a slow answer, because it
looks like a measurement.

So reuse is confined to kinds whose *product is the verdict itself*
(:data:`CACHEABLE_KINDS`), and even there every artifact the record names, and
every artifact the new plan expects, must still exist on disk. Lint is the job
an agent repeats most while iterating, so this keeps the bulk of the value while
giving up the cases that cannot be made safe.

Four things are never served from cache, because each would trade a real answer
for a stale one:

- **error / timeout records.** Those describe the environment on a bad day (a
  killed process, a full disk, a license queue), not a property of the inputs.
  Retrying is the correct response; caching would make a fluke permanent.
- **jobs with an empty input manifest.** With nothing hashed, the key would
  collapse onto argv alone and two genuinely different runs would collide.
- **a record whose job directory has been deleted.** The logs are the evidence;
  a record that cannot produce them is not evidence.
- **a record whose artifacts have been deleted.** Same reason, one level out:
  the waves a failing sim dumped are what the next step will open.

A hit is always visible — the result carries ``cached`` and the reused job id —
because a silent cache is indistinguishable from a lie about work performed.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from .. import brand
from ..util.hashing import hash_manifest, sha256_text
from ..util.jsonio import atomic_write, dump_json, load_json

# statuses safe to reuse: a real verdict about the inputs, not about the day
REUSABLE = {"passed", "failed"}

# kinds whose product IS the verdict (plus artifacts we can check for). Synth,
# STA, PnR, LEC, formal and coverage all write files that later steps read from
# paths the *fresh* plan names, and a skipped run never writes them — so they
# stay out until a run can be replayed rather than merely remembered.
CACHEABLE_KINDS = {"lint", "elab", "cdc", "format", "sim"}


def cache_key(plan, adapter, *, inputs_hash: str, seed=None,
              defines: Optional[dict] = None,
              env_modules: Optional[list[str]] = None) -> str:
    """Fingerprint of everything that could change this job's answer.

    argv is included verbatim *and* the files it names are content-hashed: a
    generated testbench under work/ is usually not in the target's manifest, so
    without that second pass an edited TB would silently reuse the old verdict.

    But argv names **outputs** too (``iverilog -o …tb.vvp``), and hashing those
    makes the key depend on the job's own product — which the job rewrites on
    every run, so the key never stabilises and the entry is never reachable.
    That is exactly why `sim` looked cacheable in the unit tests and never hit
    in a real workspace. The plan already declares its outputs, so they are
    excluded here; only genuine inputs are hashed.
    """
    argv = [tok for st in plan.steps for tok in st.argv]
    outputs = {os.path.abspath(p)
               for p in (getattr(plan, "artifacts", None) or {}).values() if p}
    referenced = [t for t in argv
                  if os.path.sep in t and os.path.abspath(t) not in outputs
                  and Path(t).is_file()]
    parts = [
        f"kind={plan.kind}",
        f"adapter={adapter.name}",
        f"version={adapter.version()}",
        f"inputs={inputs_hash}",
        f"seed={seed}",
        f"defines={sorted((defines or {}).items())}",
        f"modules={sorted(env_modules or [])}",
        f"argv={argv}",
        f"referenced={hash_manifest(referenced) if referenced else '-'}",
    ]
    return sha256_text("\n".join(parts))


class JobCache:
    """key -> job id, in one small JSON file beside the run store.

    Not an LRU and deliberately not bounded: entries are tiny, and a hit on a
    six-month-old lint is exactly as valid as one from this morning — validity
    is decided by the key, never by age.
    """

    def __init__(self, store_dir: str | Path, enabled: bool = True):
        self.path = Path(store_dir) / "cache.json"
        self.enabled = bool(enabled) and not brand.env("NO_JOB_CACHE")
        self._lock = threading.Lock()
        self._index: Optional[dict] = None

    def _load(self) -> dict:
        if self._index is None:
            try:
                data = load_json(self.path)
                self._index = data if isinstance(data, dict) else {}
            except Exception:
                self._index = {}   # unreadable index = cold cache, never fatal
        return self._index

    def lookup(self, key: str) -> Optional[str]:
        if not self.enabled:
            return None
        with self._lock:
            return self._load().get(key)

    def remember(self, key: str, job_id: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            idx = self._load()
            idx[key] = job_id
            try:
                atomic_write(self.path, dump_json(idx, sort_keys=True))
            except OSError:
                pass   # a read-only store must not fail the job that just ran

    def forget_all(self) -> int:
        """Drop every entry (`chipchamp jobs cache --clear`)."""
        with self._lock:
            n = len(self._load())
            self._index = {}
            try:
                atomic_write(self.path, dump_json({}))
            except OSError:
                pass
            return n


def result_from_record(rec) -> Optional["object"]:
    """Rebuild the adapter's ToolResult from a persisted record.

    The record's `result` is a denormalized copy written precisely so a reader
    never has to re-parse the logs — this is that reader.
    """
    from ..adapters.base import NormalizedDiagnostic, ToolResult
    r = rec.result or {}
    if not r:
        return None
    diags = []
    for d in r.get("diagnostics", []):
        try:
            diags.append(NormalizedDiagnostic(
                **{k: v for k, v in d.items()
                   if k in NormalizedDiagnostic.__dataclass_fields__}))
        except (TypeError, ValueError):
            continue
    return ToolResult(ok=bool(r.get("ok")), kind=rec.kind, adapter=rec.adapter,
                      summary=r.get("summary", ""), status=r.get("status", ""),
                      diagnostics=diags, metrics=dict(r.get("metrics") or {}),
                      artifacts=dict(rec.artifacts or {}))


def reusable(rec, job_dir: Path, plan=None) -> bool:
    """Is this record safe to serve instead of re-running the tool?

    `plan`, when given, is the job that was ABOUT to run: its declared artifact
    paths are what the caller will go on to open, so if they are not already on
    disk the run must actually happen.
    """
    if not (rec and rec.status in REUSABLE and rec.result and rec.input_files
            and rec.kind in CACHEABLE_KINDS and job_dir.is_dir()):
        return False
    for p in (rec.artifacts or {}).values():
        if p and not Path(p).exists():
            return False
    for p in (getattr(plan, "artifacts", None) or {}).values():
        if p and not Path(p).exists():
            return False
    return True
