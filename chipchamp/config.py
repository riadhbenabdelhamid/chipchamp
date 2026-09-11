"""Workspace + configuration (SPEC §12).

The central object every surface (CLI, SDK, agent) builds on. Loads
``.chipchamp/config.toml`` (+ ``tests.yaml``, ``policy.yaml``, ``DESIGN.md``),
resolves each target's sources from filelists/globs, and constructs the design
database, adapter registry, job runner, policy engine and coverage service. All
config is versioned in-repo; nothing here reaches the network.
"""
from __future__ import annotations

import glob as _glob
import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .adapters import AdapterRegistry
from .coverage import CoverageService
from .design import DesignDB
from .filelist import FileSet, resolve_sources
from .jobs import JobRunner
from .policy import Budget, ContextACL, PolicyEngine

def _yaml_or_none(p: Path):
    """Parse a dot-file, tolerating syntax errors (-> None). The dot-file
    loaders run inside ws.invalidate() — fired by every fs.write/fs.edit — and
    at Workspace construction, so a malformed hand-written file must degrade,
    never crash the session (see _load_tests)."""
    try:
        return yaml.safe_load(p.read_text())
    except yaml.YAMLError:
        return None


# ---- working-root resolution (launch from any repo) --------------------------

_XDG = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
_USER_CONFIG_DIR = _XDG / "chipchamp"
_PINNED_ROOT_FILE = _USER_CONFIG_DIR / "root"


def _pinned_root_files():
    """The pin file under every brand era's config dir, current first — so a
    root pinned before a rename keeps resolving.

    Anchored on `_PINNED_ROOT_FILE` (what `pin_root` WRITES) rather than
    recomputed from the environment, so the read path can never drift from the
    write path — and so a caller that redirects the pin file redirects both."""
    from .brand import all_names
    cur = Path(_PINNED_ROOT_FILE)
    return [cur] + [cur.parent.parent / n / cur.name for n in all_names()[1:]]


def find_project_root(start: str) -> str:
    """Walk up from `start`: the nearest ancestor with a ``.chipchamp/`` dir is the
    project root; else the enclosing git top-level; else `start` itself."""
    from .brand import dot_dir_names
    p = Path(start).resolve()
    for anc in [p, *p.parents]:
        if any((anc / d).is_dir() for d in dot_dir_names()):
            return str(anc)
    try:
        out = subprocess.run(["git", "-C", str(p), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return str(Path(out.stdout.strip()).resolve())
    except (OSError, subprocess.SubprocessError):
        pass
    return str(p)


def pinned_root() -> Optional[str]:
    for f in _pinned_root_files():
        if not f.exists():
            continue
        val = f.read_text().strip()
        if val and Path(val).is_dir():
            return str(Path(val).resolve())
    return None


def pin_root(path: Optional[str] = None) -> str:
    root = str(Path(path or os.getcwd()).resolve())
    _USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _PINNED_ROOT_FILE.write_text(root)
    return root


def unpin_root() -> bool:
    if _PINNED_ROOT_FILE.exists():
        _PINNED_ROOT_FILE.unlink()
        return True
    return False


def resolve_root(explicit: Optional[str] = None) -> tuple[str, str]:
    """Resolve the working repo and how it was chosen. Precedence (user-set wins):
    --root flag > CHIPCHAMP_ROOT env > pinned default > auto-detected repo root."""
    if explicit:
        return str(Path(explicit).resolve()), "--root flag"
    from .brand import env_name
    from .brand import env as benv
    env = benv("ROOT")
    if env:
        return str(Path(env).resolve()), env_name("ROOT")
    pin = pinned_root()
    if pin:
        return pin, "pinned default"
    cwd = os.getcwd()
    root = find_project_root(cwd)
    from .brand import dot_dir, dot_dir_names
    if any((Path(root) / d).is_dir() for d in dot_dir_names()):
        how = f"project root ({dot_dir(root).name}/)"
    elif root != str(Path(cwd).resolve()):
        how = "git repo root"
    else:
        how = "current directory"
    return root, how


# ---- no-config source inference ---------------------------------------------

_RTL_DIRS = ["rtl", "hdl", "src", "design", "verilog", "sv", "hw", "vsrc"]
_RTL_EXTS = ("*.sv", "*.svh", "*.v", "*.vh", "*.vhd", "*.vhdl")
# Directories that are never the design-under-test. Sweeping bundled examples,
# fixtures and packaged collateral into an inferred target made every sim/synth
# compile uncompilable UVM (e.g. examples/**/verif/uvm needs uvm_macros.svh) —
# the launch-in-my-repo trap. They stay out of inference.
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", ".chipchamp", "build",
              "obj_dir", "sim_build", "cocotb_build", "__pycache__", ".tox",
              "third_party", "vendor", "examples", "example", "fixtures",
              "test", "tests", "packs", "docs", "doc", "share", ".github",
              "dist", "site-packages"}

WORK_DIRNAME = "work"  # <root>/work — the agent's scratch/design directory


def _glob_rtl(dirs) -> list[str]:
    """HDL under the given directories, skipping noise dirs, deduped/sorted."""
    found: list[str] = []
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for ext in _RTL_EXTS:
            for f in _glob.glob(str(d / "**" / ext), recursive=True):
                if not any(part in _SKIP_DIRS for part in Path(f).parts):
                    found.append(f)
    return sorted(set(found))


def is_testbench(path: str) -> bool:
    """Heuristic: a simulation-only file (testbench / verification collateral),
    which must never be fed to synthesis — a TB in the yosys read set is always
    a mistake (`void'()`, class-based UVM → syntax errors).

    Only verification-DEDICATED directories count (`tb`, `verif`, `uvm`) — NOT
    generic names like `sim`/`test`/`tests`, which routinely hold synthesizable
    RTL. Plus the conventional testbench basenames (`tb_*`, `test_*`, `*_tb`,
    `*_test`, `*_tb_top`)."""
    p = Path(path)
    parts = {q.lower() for q in p.parts}
    if parts & {"tb", "verif", "uvm"}:
        return True
    stem = p.stem.lower()
    return (stem.startswith(("tb_", "test_")) or
            stem.endswith(("_tb", "_test", "_tb_top")))


def rtl_only(sources) -> list[str]:
    """Synthesizable sources — drop testbench/verification files."""
    return [s for s in sources if not is_testbench(s)]


def _infer_roots(root: str) -> list[Path]:
    """Directories an inferred target globs LIVE (re-read each access, so files
    the agent writes mid-session are picked up): conventional RTL dirs at the
    repo root, plus the agent work dir. Never a blind recursive sweep of the
    whole tree. Dirs are listed whether or not they exist YET — _glob_rtl skips
    missing ones — so an `rtl/` created mid-session joins the target on the
    next cache bust instead of staying invisible until relaunch (existence was
    previously frozen at load time, which stranded files fetched into a fresh
    root-level dir outside the target)."""
    base = Path(root)
    return [base / d for d in _RTL_DIRS] + [base / WORK_DIRNAME]


def _infer_sources(root: str) -> FileSet:
    """Fallback for odd layouts (HDL neither at a conventional root dir nor in
    the work dir): a filtered recursive sweep that still excludes examples/
    vendored/fixtures via _SKIP_DIRS."""
    return FileSet(sources=_glob_rtl([root]))


@dataclass
class Target:
    name: str
    top: str
    fileset: FileSet
    defines: dict = field(default_factory=dict)
    live_roots: list = field(default_factory=list)  # re-globbed, cached
    _sources_cache: Optional[list] = field(default=None, compare=False,
                                           repr=False)
    _incdirs_cache: Optional[list] = field(default=None, compare=False,
                                           repr=False)

    @property
    def sources(self) -> list[str]:
        srcs = list(self.fileset.sources)
        if self.live_roots:
            # cache the recursive glob; `Workspace.invalidate` busts it on any
            # write, so a file the agent adds mid-session is still picked up
            # without re-globbing the tree on every `.sources` access.
            if self._sources_cache is None:
                self._sources_cache = _glob_rtl(self.live_roots)
            srcs = sorted(set(srcs) | set(self._sources_cache))
        return srcs

    @property
    def incdirs(self) -> list[str]:
        """Include search path: the fileset's own incdirs plus, for an inferred
        target, every directory under the live roots that holds a `.svh`/`.vh`
        header — so a component fetched into the work dir (which drops its
        `include "registers.svh"` alongside it) resolves without manual config."""
        inc = list(self.fileset.incdirs)
        if self.live_roots:
            if self._incdirs_cache is None:
                self._incdirs_cache = sorted({
                    str(Path(f).parent) for f in _glob_rtl(self.live_roots)
                    if f.endswith((".svh", ".vh"))})
            inc += self._incdirs_cache
        return list(dict.fromkeys(inc))

    def invalidate(self) -> None:
        self._sources_cache = None
        self._incdirs_cache = None


# config role -> registry role
_TOOL_ROLE = {"sim_inner": "sim", "sim_signoff": "sim_signoff", "lint": "lint",
              "synth": "synth", "sta": "sta", "formal": "formal", "lec": "lec",
              "cdc": "cdc", "coverage": "coverage"}


class Workspace:
    def __init__(self, root: str = "."):
        self.root = str(Path(root).resolve())
        self.config: dict = {}
        self.targets: dict[str, Target] = {}
        self.default_target: str = ""
        self.design_md: str = ""
        self.tests: list[dict] = []
        self.policy_cfg: dict = {}
        self._db_cache: dict[str, DesignDB] = {}
        self._load()

    # ---- loading ------------------------------------------------------------

    @property
    def dot(self) -> Path:
        # brand-aware with legacy fallback: a workspace created under an
        # earlier brand name
        # keeps its jobs/sessions/config after the rename
        from .brand import dot_dir
        return Path(dot_dir(self.root))

    @property
    def work_dir(self) -> Path:
        """The agent's scratch/design directory — new designs, generated TBs
        and sim artifacts go here instead of scattering across the repo.
        `<root>/work` by default; override with [project] work_dir."""
        wd = str(self.config.get("project", {}).get("work_dir", WORK_DIRNAME))
        p = Path(wd)
        return p if p.is_absolute() else Path(self.root) / p

    def ensure_work_dir(self) -> Path:
        d = self.work_dir
        d.mkdir(parents=True, exist_ok=True)
        return d

    def rel(self, path) -> str:
        """`path` relative to the workspace root (for display)."""
        try:
            return os.path.relpath(str(path), self.root)
        except ValueError:  # different drive on Windows
            return str(path)

    def invalidate(self, target_name: Optional[str] = None) -> None:
        """After a write that may add/remove HDL files: drop the design-DB
        cache AND the target's live source-glob cache so both re-read disk.
        Also re-read tests.yaml so a test the agent just authored (e.g. a new
        verilator_cpp system-sim entry) is runnable in the same session."""
        tn = target_name or self.default_target
        self._db_cache.pop(tn, None)
        t = self.targets.get(tn)
        if t is not None:
            t.invalidate()
        self._load_tests()

    def _load(self) -> None:
        cfg_path = self.dot / "config.toml"
        if cfg_path.exists():
            with open(cfg_path, "rb") as fh:
                self.config = tomllib.load(fh)
        # source-of-truth map (SPEC §12.5): which files are generated, from what
        self.generated: list[dict] = self.config.get("generated", [])
        self._load_targets()
        self._load_design_md()
        self._load_tests()
        self._load_policy()

    def generated_globs(self) -> list[str]:
        return [g for entry in self.generated for g in entry.get("outputs", [])]

    def _load_targets(self) -> None:
        tsec = self.config.get("targets", {})
        if not tsec:
            # no config: infer a single target named after the repo directory.
            # It globs conventional RTL dirs + the work dir LIVE, so a design the
            # agent writes this session is simulable without re-launch. Only if
            # that yields nothing (an unconventional layout) do we fall back to a
            # filtered recursive sweep — which still excludes examples/vendored.
            roots = _infer_roots(self.root)
            name = Path(self.root).name or "default"
            t = Target(name, top="", fileset=FileSet(), live_roots=roots)
            if not t.sources:
                t.fileset = _infer_sources(self.root)
            self.targets[name] = t
            self.default_target = name
            return
        for name, spec in tsec.items():
            fs = resolve_sources(
                self.root, filelists=spec.get("filelists"),
                globs=spec.get("globs"), sources=spec.get("sources"),
                fusesoc_core=spec.get("fusesoc_core"), bender=spec.get("bender"))
            self.targets[name] = Target(
                name=name, top=spec.get("top", ""), fileset=fs,
                defines={**fs.defines, **spec.get("defines", {})})
        self.default_target = self.config.get("project", {}).get(
            "default_target", next(iter(self.targets)))

    def _load_design_md(self) -> None:
        names = self.config.get("project", {}).get("design_md", ["DESIGN.md"])
        chunks = []
        for n in names:
            p = Path(self.root) / n
            if p.exists():
                chunks.append(f"# {n}\n" + p.read_text(errors="replace"))
        self.design_md = "\n\n".join(chunks)

    def _load_tests(self) -> None:
        p = self.dot / "tests.yaml"
        if not p.exists():
            self.tests = []          # a deleted manifest must not leave stale entries
            return
        # Canonical shape is {tests: [...]}, but a model may hand-write a bare
        # top-level list, a null 'tests:' value, scalar entries, or invalid
        # YAML. Tolerate ALL of it rather than raising — this loader runs
        # inside ws.invalidate() (every fs.write/fs.edit) and at Workspace
        # construction, so a crash here poisons the agent loop and even blocks
        # session startup. Malformed content degrades to "no tests".
        data = _yaml_or_none(p)
        if isinstance(data, dict):
            entries = data.get("tests") or []   # 'tests:' with a null value
        elif isinstance(data, list):
            entries = data
        else:
            entries = []
        if not isinstance(entries, list):
            entries = []
        self.tests = [t for t in entries if isinstance(t, dict)]

    def _load_policy(self) -> None:
        p = self.dot / "policy.yaml"
        if p.exists():
            data = _yaml_or_none(p)     # same crash class as _load_tests
            self.policy_cfg = data if isinstance(data, dict) else {}

    # ---- constructed services ----------------------------------------------

    def registry(self) -> AdapterRegistry:
        selection = {}
        tools = self.config.get("tools", {})
        for k, v in tools.items():
            role = _TOOL_ROLE.get(k)
            if role:
                selection[role] = v[0] if isinstance(v, list) else v
        return AdapterRegistry(selection)

    def runner(self) -> JobRunner:
        """`[jobs] store` points the run store somewhere shared, so a team hits
        ONE job cache instead of each engineer paying for the same simulation.
        Default stays workspace-local: sharing a store is a deliberate act with
        real consequences (one person's flaky run becomes everyone's cached
        verdict), never something a workspace falls into."""
        from .jobs.farm import FarmConfig
        # Memoized: a second JobRunner on the same store is a second id
        # authority — if the store's seq.txt is momentarily absent (an
        # experiment harness blinding the archive), the newcomer restarts
        # numbering at J-0001 and its records collide with history.
        if getattr(self, "_runner", None) is not None:
            return self._runner
        shared = (self.config.get("jobs", {}) or {}).get("store", "")
        store = os.path.expanduser(shared) if shared else str(self.dot / "runs")
        licenses = self.config.get("licenses", {})
        env_modules = self.config.get("tools", {}).get("env_modules", {})
        self._runner = JobRunner(store, self.registry(),
                                 env_modules=env_modules, licenses=licenses,
                                 farm=FarmConfig.from_config(self.config),
                                 cache=self.config.get("jobs", {}).get("cache", True))
        return self._runner

    def context_acl(self) -> ContextACL:
        ctx = self.config.get("context", {})
        return ContextACL(deny=ctx.get("deny", []), allow=ctx.get("allow", []))

    def budget(self) -> Budget:
        b = self.config.get("budgets", {}).get("default", {})
        return Budget(
            v3_submissions=b.get("v3_submissions_per_task", 3),
            license_hours=b.get("license_hours", 24.0),
            cpu_hours=b.get("cpu_hours", 200.0),
            model_tokens=b.get("model_tokens", 2_000_000))

    def policy_engine(self) -> PolicyEngine:
        auto = self.policy_cfg.get("autonomy", {})
        default = auto.get("default", "L1")
        rules = {k: v for k, v in auto.items() if k != "default"}
        from .brand import dot_dir_names
        protected = self.policy_cfg.get("protected", [".chipchamp/policy.yaml", "waivers/**"])
        # a workspace's policy must be protected under EVERY brand era's
        # dot-dir name — a list written under an earlier name must also
        # shield ".chipchamp/..." and vice versa, or a rename opens a hole
        extra = []
        for pat in protected:
            for d in dot_dir_names():
                for other in dot_dir_names():
                    if pat.startswith(other + "/"):
                        extra.append(d + pat[len(other):])
        protected = sorted(set(protected) | set(extra))
        return PolicyEngine(acl=self.context_acl(), autonomy_rules=rules,
                            default_autonomy=default, budget=self.budget(),
                            protected_paths=protected,
                            generated=self.generated,
                            # a workspace that declares [riscv] is a core: RTL
                            # edits then owe ISA evidence (classify → rung R)
                            riscv_core=bool(self.config.get("riscv")))

    def target(self, name: Optional[str] = None) -> Target:
        return self.targets[name or self.default_target]

    # ---- design database ----------------------------------------------------

    def db(self, target: Optional[str] = None, rebuild: bool = False) -> DesignDB:
        tname = target or self.default_target
        if tname in self._db_cache and not rebuild:
            return self._db_cache[tname]
        db_path = self.dot / f"db-{tname}.json"
        tgt = self.target(tname)
        if db_path.exists() and not rebuild:
            db = DesignDB.load(str(db_path))
            # A database carried over from another checkout (a cloned or seeded
            # workspace) still names files that exist — at the OLD root — so
            # stale_files() alone would call it fresh. The root moving is stale.
            same_root = os.path.realpath(str(db.root)) == os.path.realpath(str(self.root))
            if same_root and not db.stale_files():
                self._db_cache[tname] = db
                return db
        db = DesignDB(root=self.root)
        acl = self.context_acl()
        for src in tgt.sources:
            rel = os.path.relpath(src, self.root)
            if not acl.is_allowed(rel):
                continue  # ACL: PDK/vendor sources never enter the index by default
            db.add_file(src, defines=tgt.defines, incdirs=tgt.incdirs)
        overrides = self._domain_overrides()
        db.build(overrides)
        db.save(str(db_path))
        self._db_cache[tname] = db
        return db

    def _domain_overrides(self) -> dict:
        p = self.dot / "domains.yaml"
        if p.exists():
            return (yaml.safe_load(p.read_text()) or {}).get("modules", {})
        return {}

    def coverage_baseline(self) -> Optional[CoverageService]:
        p = self.dot / "coverage" / "baseline.json"
        return CoverageService.load(str(p)) if p.exists() else None

    def smoke_tests(self) -> list[dict]:
        return [t for t in self.tests if "smoke" in (t.get("tags") or [])]

    def tests_with_tag(self, tag: str) -> list[dict]:
        return [t for t in self.tests if tag in (t.get("tags") or [])]

    # ---- skills (SPEC §15.3) -------------------------------------------------

    @property
    def skills_cfg(self) -> dict:
        return self.config.get("skills", {}) or {}

    def skills_dirs(self):
        from . import skills as _sk
        return _sk.skill_dirs(self)

    def skills(self):
        from . import skills as _sk
        return _sk.discover(self)

    def skills_index_mode(self) -> str:
        """semantic (embedding-selected, the default) | compact | full | off.
        CHIPCHAMP_SKILLS_INDEX overrides config (CI/tests pin 'compact' to
        stay network-free; users can force a mode per shell)."""
        import os as _os

        from . import skills as _sk
        from .brand import env as _benv
        mode = str(_benv("SKILLS_INDEX", "")
                   or self.skills_cfg.get("index", "semantic")).lower()
        return mode if mode in _sk.INDEX_MODES else "semantic"

    # ---- IP library ----------------------------------------------------------

    @property
    def library_cfg(self) -> dict:
        return self.config.get("library", {}) or {}

    def library_index_mode(self) -> str:
        from . import library as _lib
        mode = str(self.library_cfg.get("index", "compact")).lower()
        return mode if mode in _lib.INDEX_MODES else "compact"
