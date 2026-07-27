"""EDA Adapter Layer (SPEC §8.4)."""
from .base import (
    Adapter,
    CapabilityManifest,
    NormalizedDiagnostic,
    Plan,
    Step,
    StepResult,
    ToolResult,
)
from .registry import AdapterRegistry

__all__ = [
    "Adapter",
    "CapabilityManifest",
    "NormalizedDiagnostic",
    "Plan",
    "Step",
    "StepResult",
    "ToolResult",
    "AdapterRegistry",
]
