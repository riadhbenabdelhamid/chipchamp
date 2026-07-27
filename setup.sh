#!/usr/bin/env bash
# Bootstrap Chipchamp into a local virtualenv (avoids PEP 668 / "externally-managed
# environment" errors from installing against the system Python). Idempotent.
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
PY="${PYTHON:-python3}"

echo "==> Chipchamp setup"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: $PY not found. Install Python 3.11+ (e.g. 'sudo apt install python3-full')." >&2
  exit 1
fi

if [ ! -d "$VENV" ]; then
  echo "==> creating virtualenv at $VENV"
  # --system-site-packages lets us reuse a system click/rich/pyyaml if present,
  # but pip will install them into the venv otherwise.
  "$PY" -m venv --system-site-packages "$VENV" 2>/dev/null || "$PY" -m venv "$VENV"
fi

# shellcheck disable=SC1091
. "$VENV/bin/activate"
echo "==> upgrading pip"
python -m pip install --quiet --upgrade pip
echo "==> installing chipchamp (editable, with slang + TUI + dev extras)"
python -m pip install --quiet -e ".[dev,tui,slang]" || \
  python -m pip install --quiet -e ".[dev,tui]" || \
  python -m pip install --quiet -e ".[dev]" || \
  python -m pip install --quiet -e .

echo
echo "==> installed:"
python -c "import chipchamp; print('    chipchamp', chipchamp.__version__)"
echo "    chipchamp CLI -> $(command -v chipchamp)"

# EDA toolchain hint
echo
echo "==> EDA tools on PATH:"
missing=0
for t in verilator iverilog yosys verible-verilog-lint sby eqy ghdl; do
  if command -v "$t" >/dev/null 2>&1; then
    printf "    \033[32m✓\033[0m %s\n" "$t"
  else
    printf "    \033[33m–\033[0m %s (not found)\n" "$t"
    missing=1
  fi
done
if [ "$missing" = 1 ]; then
  echo
  echo "    Some EDA tools are missing. If you have oss-cad-suite / verible installed,"
  echo "    add them to PATH, e.g.:"
  echo "      export PATH=\"\$HOME/oss-cad-suite/bin:\$HOME/verible/bin:\$PATH\""
fi

echo
echo "==> done. Activate the environment in new shells with:"
echo "      source $VENV/bin/activate"
echo "    then try:"
echo "      chipchamp --root examples/soc index"
echo "      python demo.py"
