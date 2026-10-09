"""Standalone CPython/cocotb dependency observer (copied into the build directory).

This is provenance instrumentation, not a sandbox. Unsupported native I/O,
subprocesses and network activity make a run ineligible for resume.
"""
import atexit
import _thread
import hashlib
import importlib
import importlib.util
import json
import os
import stat
import sys
import sysconfig
import threading


def digest(path):
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return "UNSUPPORTED"  # Never drain a device, socket or FIFO to hash it.
        with open(path, "rb") as stream:
            checksum = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
            return checksum.hexdigest()
    except OSError as error:
        return "MISSING" if error.errno in (2, 20) else "ERROR:" + str(error.errno)
    except ValueError:
        return "UNSUPPORTED"


def environment():
    excluded = json.loads(os.environ.get("CHIPCHAMP_TRACE_OUTPUT_DIRS", "[]"))
    names, metadata, complete = set(), {}, True
    for entry in sys.path:
        root = os.path.realpath(entry or os.getcwd())
        if any(root == p or root.startswith(p + os.sep) for p in excluded):
            continue
        if os.path.isfile(root):
            metadata[root] = digest(root)
            complete &= len(metadata[root]) == 64
            continue
        if not os.path.isdir(root):
            metadata[root] = "MISSING"
            continue
        errors = []
        for base, dirs, files in os.walk(root, onerror=errors.append):
            dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git", ".chipchamp")
                              and not any(os.path.realpath(os.path.join(base, d)) == p for p in excluded))
            if any(os.path.islink(os.path.join(base, d)) for d in dirs):
                complete = False
            for name in files:
                path = os.path.realpath(os.path.join(base, name))
                if name.endswith((".py", ".so", ".pyd", ".zip", ".pth")):
                    checksum = digest(path)
                    names.add((path, checksum))
                    complete &= len(checksum) == 64
                if name in ("METADATA", "PKG-INFO") or name.endswith(".pth"):
                    metadata[path] = digest(path)
                    complete &= len(metadata[path]) == 64
        complete &= not errors
    metadata[os.path.realpath(sys.executable)] = digest(sys.executable)
    complete &= len(metadata[os.path.realpath(sys.executable)]) == 64
    library = os.path.join(sysconfig.get_config_var("LIBDIR") or "", sysconfig.get_config_var("LDLIBRARY") or "")
    if os.path.isfile(library):
        metadata[os.path.realpath(library)] = digest(library)
        complete &= len(metadata[os.path.realpath(library)]) == 64
    try:
        supported = importlib.util.find_spec("pygpi.entry") is not None
    except (ImportError, ValueError):
        supported = False
    return {"version": sys.version, "search_paths": sys.path,
            "catalog": hashlib.sha256(json.dumps(sorted(names)).encode()).hexdigest(),
            "metadata": metadata, "complete": bool(complete and supported)}


def start(argv):
    original = os.environ.pop("CHIPCHAMP_ORIGINAL_PYGPI_USERS", "")
    if original:
        os.environ["PYGPI_USERS"] = original
    else:
        os.environ.pop("PYGPI_USERS", None)
    destination = os.environ["CHIPCHAMP_PYTHON_TRACE"]
    outputs = json.loads(os.environ["CHIPCHAMP_TRACE_OUTPUT_DIRS"])
    reads, directories, reasons = {}, {}, set()
    local = threading.local()
    finished = False
    framework = {"cocotb", "pygpi", "cocotb_tools", "pytest", "_pytest", "pluggy", "_distutils_hack", "py"}
    stdlib = os.path.realpath(sysconfig.get_path("stdlib")) + os.sep
    untraced = {os.stat, os.lstat, os.access, os.readlink}

    def profile(frame, event, arg):
        # CPython's audit events omit stat/existence queries. C-call profiling
        # catches those even through pathlib, aliases or third-party helpers.
        # Import resolution is covered by the pre/post Python environment catalog.
        if finished or getattr(local, "busy", False) or event != "c_call":
            return
        if arg is _thread.start_new_thread:
            reasons.add("Python threads have untraced runtime inputs")
            return
        if arg not in untraced:
            return
        module = frame.f_globals.get("__name__", "")
        origin = module + ":" + frame.f_code.co_name
        if module.startswith(("importlib.", "_frozen_importlib")):
            return
        while frame:
            module = frame.f_globals.get("__name__", "")
            filename = frame.f_code.co_filename
            if module == __name__ or module.startswith(("importlib.", "_frozen_importlib")):
                return
            standard = filename.startswith(stdlib) and "/site-packages/" not in filename and "/dist-packages/" not in filename
            if module.split(".")[0] not in framework and not standard and not filename.startswith("<frozen "):
                reasons.add("Python stat/existence checks have untraced inputs: " + module + " via " + origin)
                return
            frame = frame.f_back

    def is_output(path):
        return path == destination or any(path == d or path.startswith(d + os.sep) for d in outputs)

    def remember(path):
        if not isinstance(path, (str, bytes, os.PathLike)):
            reasons.add("read through an untracked file descriptor")
            return
        path = os.path.abspath(os.fsdecode(path))
        if is_output(os.path.realpath(path)) or path == os.devnull:
            return
        value = digest(path)
        if value == "UNSUPPORTED" or value.startswith("ERROR:"):
            reasons.add("Python input is not a readable regular file: " + path)
        if path in reads and reads[path] != value:
            reasons.add("a Python input changed during execution")
        reads[path] = value

    def audit(event, args):
        if finished or getattr(local, "busy", False):
            return
        local.busy = True
        try:
            if event == "open":
                path, mode, flags = args
                if not (flags & os.O_WRONLY):
                    remember(path)
            elif event in ("os.listdir", "os.scandir"):
                path = os.path.abspath(os.fsdecode(args[0] or os.getcwd()))
                if not is_output(os.path.realpath(path)):
                    try:
                        listing = sorted(os.listdir(path))
                    except OSError as error:
                        listing = None
                        if error.errno not in (2, 20):
                            reasons.add("Python directory is unreadable: " + path)
                    if path in directories and directories[path] != listing:
                        reasons.add("a Python input directory changed during execution")
                    directories[path] = listing
            elif event.startswith(("subprocess.", "socket.", "ctypes.")) or event in ("os.system", "os.exec", "os.fork", "os.posix_spawn", "os.chdir"):
                reasons.add("untraced native/subprocess/network activity: " + event)
            elif event == "import" and args[1] and args[1].endswith((".so", ".pyd")):
                if "lib-dynload" not in args[1] and not args[0].startswith(("cocotb.", "pygpi.")):
                    reasons.add("native extension has untraced runtime inputs: " + args[0])
            elif event == "sys.setprofile":
                reasons.add("Python dependency profiling was replaced")
        except (OSError, TypeError, ValueError):
            reasons.add("unresolved Python runtime dependency")
        finally:
            local.busy = False

    def finish():
        nonlocal finished
        if finished:
            return
        local.busy = True
        for name, module in list(sys.modules.items()):
            path = getattr(module, "__file__", None)
            if not path:
                continue
            remember(path)
            if path.endswith(".pyc"):
                try:
                    remember(importlib.util.source_from_cache(path))
                except ValueError:
                    pass
            if path.endswith((".so", ".pyd")) and "lib-dynload" not in path and not name.startswith(("cocotb.", "pygpi.")):
                reasons.add("native extension has untraced runtime inputs: " + name)
        if len(threading.enumerate()) > 1:
            reasons.add("Python background threads are still active")
        finished = True
        with open(destination, "w", encoding="utf-8") as stream:
            json.dump({"schema_version": 1, "finished": True, "files": reads,
                       "directories": directories, "reasons": sorted(reasons),
                       "complete": not reasons}, stream)

    sys.setprofile(profile)
    sys.addaudithook(audit)
    atexit.register(finish)
    importlib.import_module("pygpi.entry").load_entry(argv)
    try:
        # This runs after cocotb writes its results, even for simulators that
        # do not finalize the embedded interpreter on normal exit.
        importlib.import_module("cocotb._init")._register_shutdown_callback(finish)
    except AttributeError:
        reasons.add("cocotb shutdown tracing is unsupported")


if __name__ == "__main__":
    print(json.dumps(environment()))
