# Chipchamp — convenience targets. All run inside the local .venv (PEP 668-safe).
VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip
BIN  := $(VENV)/bin

.PHONY: help setup install test demo index clean

help:
	@echo "Chipchamp make targets:"
	@echo "  make setup    - create .venv and install chipchamp (editable) + dev deps"
	@echo "  make test     - run the pytest suite"
	@echo "  make demo     - run the end-to-end demo (inject bug -> triage -> fix -> evidence)"
	@echo "  make index    - build the design database for examples/soc"
	@echo "  make clean    - remove generated caches (keeps .venv)"
	@echo ""
	@echo "Tip: 'source .venv/bin/activate' to use the 'chipchamp' command directly."

setup:
	./setup.sh

install: setup

$(VENV):
	./setup.sh

test: | $(VENV)
	$(PY) -m pytest -q

demo: | $(VENV)
	$(PY) demo.py

index: | $(VENV)
	$(PY) -m chipchamp --root examples/soc index --rebuild

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf examples/*/.chipchamp/runs examples/*/.chipchamp/db-*.json \
	       examples/*/.chipchamp/bundles examples/*/.chipchamp/sessions \
	       examples/*/.chipchamp/audit.jsonl examples/*/obj_cov \
	       examples/*/*.vcd examples/*/*.vvp examples/*/coverage.dat 2>/dev/null || true
	@echo "cleaned generated artifacts (.venv kept)"
