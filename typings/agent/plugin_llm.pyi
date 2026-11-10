from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeAlias

@dataclass
class PluginLlmTextInput:
    text: str
    type: str = "text"

@dataclass
class PluginLlmImageInput:
    data: bytes | None = None
    url: str | None = None
    mime_type: str = "image/png"
    file_name: str = ""
    type: str = "image"

PluginLlmInput: TypeAlias = PluginLlmTextInput | PluginLlmImageInput | dict[str, object]

@dataclass
class PluginLlmUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None

@dataclass
class PluginLlmStructuredResult:
    text: str
    provider: str
    model: str
    agent_id: str
    usage: PluginLlmUsage = ...
    parsed: object = None
    content_type: str = "text"
    audit: dict[str, object] = ...

class PluginLlm:
    def complete_structured(
        self,
        *,
        instructions: str,
        input: Sequence[PluginLlmInput],
        json_schema: object = None,
        json_mode: bool = False,
        schema_name: str | None = None,
        system_prompt: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        agent_id: str | None = None,
        profile: str | None = None,
        purpose: str | None = None,
        task: str | None = None,
    ) -> PluginLlmStructuredResult: ...
