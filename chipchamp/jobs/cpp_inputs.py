"""Ask the actual C++ preprocessor for headers, including system headers."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from .env import scrub_env
from ..util.hashing import sha256_file


def compiler_inputs(spec, workdir):
    files, complete = {}, True
    generated = [Path(d).resolve() for d in spec.get("generated_dirs", [])]
    commands = {}
    if spec.get("make"):
        try:
            # Use the generated makefile's exact compilation flags, including
            # optimization macros and environment-supplied CXXFLAGS/CPPFLAGS.
            probe = subprocess.run([*spec["make"], "-n", "-B"], cwd=workdir, env=scrub_env(),
                                   capture_output=True, text=True, timeout=30)
            for line in probe.stdout.splitlines():
                if not any(source in line for source in spec["sources"]):
                    continue
                args = shlex.split(line)
                if not args or "-c" not in args or os.path.realpath(args[0]) != os.path.realpath(spec["compiler"]):
                    continue
                for source in spec["sources"]:
                    if source in args:
                        flags = []
                        tokens = iter(args[1:])
                        for token in tokens:
                            if token in ("-o", "-MF", "-MT", "-MQ"):
                                next(tokens, None)
                            elif token not in ("-c", "-MMD", "-MD", "-MP", source):
                                flags.append(token)
                        commands[source] = flags
        except (OSError, ValueError, subprocess.SubprocessError):
            return {}, False
    for source in spec["sources"]:
        try:
            if spec.get("make") and source not in commands:
                complete = False
                continue
            command = [spec["compiler"], *commands.get(source, spec.get("flags", [])), "-M", "-MT", "chipchamp", source]
            result = subprocess.run(command, cwd=spec.get("build_dir", workdir), env=scrub_env(),
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                complete = False
                continue
            text = result.stdout.replace("\\\n", " ")
            for line in text.splitlines():
                if ":" not in line:
                    continue
                for name in shlex.split(line.split(":", 1)[1]):
                    path = Path(os.path.realpath(os.path.join(spec.get("build_dir", workdir), name)))
                    if any(path.is_relative_to(directory) for directory in generated):
                        continue  # regenerated from the HDL and pinned toolchain
                    files[str(path)] = sha256_file(path)
        except (OSError, ValueError, subprocess.SubprocessError):
            complete = False
    try:
        files[os.path.realpath(spec["compiler"])] = sha256_file(spec["compiler"])
    except OSError:
        complete = False
    return files, complete
