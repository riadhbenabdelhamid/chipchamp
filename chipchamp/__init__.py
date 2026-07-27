"""Chipchamp — an agentic coding platform for RTL design & verification.

This package implements the platform substrate described in ``SPEC.md``: the
deterministic "loop around the model" (design database, EDA adapters, job
orchestration, waveform/coverage services, policy gates, evidence bundles) plus
a model-agnostic agent core that drives them.

The guiding principle (SPEC §6 P1) is *evidence over eloquence*: every claim the
agent makes is backed by a machine-generated job record, not prose.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
