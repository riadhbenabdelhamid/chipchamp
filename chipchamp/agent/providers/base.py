"""Provider abstraction for the model layer (SPEC §14, FR-MDL-01).

Chipchamp is model-agnostic: it must run against Anthropic, OpenAI, and any
OpenAI-compatible endpoint — crucially the local servers a hardware team can run
inside its own network (LM Studio, Ollama, vLLM, llama.cpp), which matters for the
on-prem / air-gapped deployment modes (SPEC §13.1 D3/D4).

A :class:`Provider` translates the platform's *neutral transcript* (a
JSON-serializable list of turns) into its wire format, calls the model, and
returns a normalized :class:`ModelResponse`. It can also enumerate accessible
models so the ``/model`` selector can query and present them.

Neutral transcript turn shapes (all JSON-safe, so sessions persist cleanly):
    {"role": "user", "content": "<text>"}
    {"role": "assistant", "content": "<text>", "tool_calls": [{"id","name","input"}]}
    {"role": "tool", "content": [{"id","name","output": "<str>"}]}
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

# The DEFAULT per-call model budget, in seconds — one source of truth for the
# gateway's wall-clock deadline and the HTTP socket timeout, so they cannot
# diverge and silently cap each other (a slow reasoning model then dies at the
# socket's limit while the user raises the wrong knob). A per-gateway override
# (config [model] request_timeout) is threaded down via Provider.request_timeout.
# `or "600"` also covers a set-but-empty env var, which float() would reject.
from ...brand import env as _brand_env
DEFAULT_REQUEST_TIMEOUT = float(_brand_env("MODEL_TIMEOUT") or "600")


@dataclass
class ModelInfo:
    id: str
    provider: str
    context: Optional[int] = None
    note: str = ""

    @property
    def ref(self) -> str:
        return f"{self.provider}:{self.id}"


@dataclass
class ModelResponse:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # {id, name, input}
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning: str = ""  # model-internal reasoning (reasoning_content/<think>) — never entered into the transcript


@dataclass
class ProviderConfig:
    name: str  # instance name (e.g. "ollama", "openai", "anthropic", "work-gpt")
    kind: str  # "anthropic" | "openai"
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    default_model: str = ""
    local: bool = False  # a locally-hosted endpoint (no key expected)
    extra_headers: dict = field(default_factory=dict)

    def resolved_key(self) -> str:
        import os
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return ""


# Built-in presets. Local ones point at each server's default port and use a
# throwaway key (local servers ignore it). Users can override base_url/model.
PRESETS: dict[str, ProviderConfig] = {
    "anthropic": ProviderConfig(
        name="anthropic", kind="anthropic",
        base_url="https://api.anthropic.com", api_key_env="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-5"),
    "openai": ProviderConfig(
        name="openai", kind="openai",
        base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY",
        default_model="gpt-4o"),
    "lmstudio": ProviderConfig(
        name="lmstudio", kind="openai",
        base_url="http://localhost:1234/v1", api_key="lm-studio", local=True),
    "ollama": ProviderConfig(
        name="ollama", kind="openai",
        base_url="http://localhost:11434/v1", api_key="ollama", local=True),
    "vllm": ProviderConfig(
        name="vllm", kind="openai",
        base_url="http://localhost:8000/v1", api_key="-", local=True),
    "llamacpp": ProviderConfig(
        name="llamacpp", kind="openai",
        base_url="http://localhost:8080/v1", api_key="-", local=True),
}


class Provider:
    """Base class. Concrete providers implement chat + list_models + available."""

    # Per-call HTTP budget, set by the owning ModelGateway so the socket
    # timeout tracks the gateway's wall-clock deadline (config [model]
    # request_timeout included). None -> DEFAULT_REQUEST_TIMEOUT; 0 -> no limit
    # (mirrors the gateway's own "<= 0 disables the deadline" semantics).
    request_timeout: Optional[float] = None

    def __init__(self, config: ProviderConfig):
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    def available(self) -> bool:  # pragma: no cover - overridden
        return False

    def list_models(self) -> list[ModelInfo]:  # pragma: no cover - overridden
        return []

    def chat(self, model: str, system: str, transcript: list[dict],
             tools: list[dict], max_tokens: int = 4096,
             reasoning_effort: str = "",
             options: Optional[dict] = None) -> ModelResponse:  # pragma: no cover
        """`reasoning_effort` ("low"/"medium"/"high"/…) is sent only when set and
        only meaningful for reasoning models; providers ignore it otherwise.
        `options` (`/model tune`) are extra generation parameters
        (temperature/top_p/num_ctx/…) a provider merges into its wire request —
        sent only when set, so an untuned call is unchanged."""
        raise NotImplementedError

    # ---- neutral tool schema -> provider wire format ------------------------

    @staticmethod
    def _neutral_tools(tools: list[dict]) -> list[dict]:
        """tools are neutral: {name, description, schema}."""
        return tools
