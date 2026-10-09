"""One resolver for every model-supplied path.

A workspace path arrives as text the model wrote. Before any tool opens it:
it must be workspace-relative (no absolute paths), it must stay inside the
root once normalised (no `../`), and the policy engine must allow both its
lexical name and resolved target. Three tools had their own partial
versions of this and five had none; a model that wrote `./waivers/x.yaml`
walked straight past a protected-path glob that only knew `waivers/**`.

Usage:
    try:
        ap, rel = ws_path(ctx, path, write=True)
    except PathRefused as e:
        return e.payload          # a teaching dict, ready to return
"""
from __future__ import annotations

import os

from ..policy.engine import canonical


class PathRefused(Exception):
    """Carries the error dict a tool should return verbatim."""

    def __init__(self, payload: dict):
        super().__init__(payload.get("error", "path refused"))
        self.payload = payload


def ws_path(ctx, path: str, *, write: bool = False,
            allow_generated: bool = False) -> tuple[str, str]:
    """-> (absolute, canonical-relative). Raises PathRefused with a teaching.

    Reads consult can_read; writes consult can_write. Both the requested name
    and resolved target must pass, so symlinks cannot bypass an ACL or a
    protected/generated-path rule."""
    path = str(path or "")
    if os.path.isabs(path):
        raise PathRefused({"error": f"absolute path not allowed: '{path}'. File "
                                    f"paths are workspace-relative — e.g. "
                                    f"work/rtl/<name>.sv."})
    rel = canonical(path)
    root = os.path.realpath(ctx.ws.root)
    ap = os.path.realpath(os.path.join(root, rel))
    if rel == ".." or rel.startswith("../") or (
            ap != root and not ap.startswith(root + os.sep)):
        raise PathRefused({"error": f"path escapes the workspace: '{path}'. Use "
                                    f"a workspace-relative path under the "
                                    f"project root."})
    resolved_rel = os.path.relpath(ap, root)
    resolved_rel = "" if resolved_rel == "." else resolved_rel
    # Check both names: a public alias must not expose a protected target,
    # and a protected alias must not acquire the target's broader permissions.
    for candidate in {rel, resolved_rel}:
        if write:
            ok, why = ctx.policy.can_write(candidate, allow_generated=allow_generated)
            if not ok:
                raise PathRefused({"error": f"write denied: {why}"})
        elif not ctx.policy.can_read(candidate):
            raise PathRefused({"error": f"read denied: {ctx.policy.acl.reason(candidate)}"})
    return ap, resolved_rel


def safe_name(name: str) -> str | None:
    """A single path component the model chose (a board name, a job id):
    no separators, not `.`/`..`, not empty. None when it is not safe."""
    name = str(name or "")
    if not name or name in (".", "..") or "/" in name or "\\" in name \
            or os.path.basename(name) != name:
        return None
    return name
