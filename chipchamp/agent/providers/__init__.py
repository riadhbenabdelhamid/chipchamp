"""Provider-agnostic model layer (SPEC §14, FR-MDL-01)."""
from .anthropic_provider import AnthropicProvider
from .base import PRESETS, ModelInfo, ModelResponse, Provider, ProviderConfig
from .openai_compat import OpenAICompatProvider
from .registry import ModelRegistry

__all__ = [
    "Provider",
    "ProviderConfig",
    "ModelInfo",
    "ModelResponse",
    "PRESETS",
    "AnthropicProvider",
    "OpenAICompatProvider",
    "ModelRegistry",
]
