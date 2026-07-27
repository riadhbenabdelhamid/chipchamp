"""Register-map compiler (SPEC §9-I): spec → RTL + header + docs, deterministically."""
from .compiler import RegmapSpec, generate, load_spec, validate

__all__ = ["RegmapSpec", "load_spec", "validate", "generate"]
