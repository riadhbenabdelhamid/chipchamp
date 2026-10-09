"""Content-addressed source snapshots pinned by immutable coverage observations."""
from pathlib import Path
import hashlib
import os

from ..util.hashing import sha256_file
from ..util.jsonio import atomic_write
from .impact import location, MAX_SOURCE_BYTES


def capture_sources(ctx, rec, model):
    from ..tools.paths import PathRefused, ws_path
    snapshots, unavailable = {}, {}
    paths = sorted({location(b.source)[0] for b in model.bins.values() if b.source})
    for source in paths:
        try:
            path, relative = ws_path(ctx, ctx.rel(os.path.join(ctx.ws.root, source)))
            digest = rec.input_snapshot.get("files", {}).get(str(Path(path).resolve()))
            if not digest or sha256_file(path) != digest:
                raise ValueError("source is absent from the job's verified inputs")
            if Path(path).stat().st_size > MAX_SOURCE_BYTES:
                raise ValueError("source exceeds the snapshot limit")
            # Read bytes to preserve CRLF and bind the snapshot to the exact hash.
            with open(path, "rb") as stream:
                raw = stream.read(MAX_SOURCE_BYTES + 1)
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("source changed while recording coverage")
            text = raw.decode("utf-8")
            destination, stored = ws_path(ctx, ctx.rel(ctx.ws.dot / "coverage" / "sources" / (digest + ".txt")), write=True)
            if not Path(destination).is_file() or sha256_file(destination) != digest:
                atomic_write(destination, text)
            snapshots[relative] = {"path": stored, "sha256": digest}
        except (OSError, ValueError, PathRefused) as error:
            unavailable[source] = str(error)
    return snapshots, unavailable


def read_source(ctx, data, relative):
    from ..tools.paths import ws_path
    ws_path(ctx, relative)  # An archived copy must respect the source's current ACL.
    spec = data.get("source_snapshots", {}).get(relative)
    if not spec:
        raise ValueError("observation has no pinned source snapshot (legacy or unavailable source)")
    path = ws_path(ctx, spec["path"])[0]
    with open(path, "rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES or hashlib.sha256(raw).hexdigest() != spec["sha256"]:
        raise ValueError("pinned source snapshot is missing or changed")
    return raw.decode("utf-8")
