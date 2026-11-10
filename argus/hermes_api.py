"""What Argus needs from Hermes at run time, as structural types.

`register(ctx)` receives Hermes' `hermes_cli.plugins.PluginContext`; Argus types it with the
Protocol below, which names only the members Argus calls, with the parameter types Argus passes
(signatures: hermes_cli/plugins.py, agent/plugin_llm.py, hermes_cli/plugins_dispatch.py and
hermes_cli/plugin_command_dispatch.py). tests/test_hermes_contract.py checks statically that the
stubbed `PluginContext` (typings/, checked against the Hermes runtime by `just stubtest`) satisfies
it.

Hermes calls hooks with keyword arguments only and slash commands with the raw argument string plus
context keywords; it guarantees no types for either, so callbacks take `object` and narrow.

`LlmFn` is the model call injected into derive.run; `Llm` is a call whose answer is already a JSON
object or None (cli.make_llm and derive's call counter are one).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Protocol, TypeAlias

from .jsontypes import JsonObject

__all__ = [
    "CliHandler",
    "CliSetup",
    "HookCallback",
    "InputBlock",
    "Llm",
    "LlmFn",
    "PluginContext",
    "PluginLlm",
    "SlashCommand",
    "StructuredResult",
]

InputBlock: TypeAlias = dict[str, object]
"""One structured input block, `{"type": "text", "text": ...}` (plugin_llm.PluginLlmInput)."""

CliSetup: TypeAlias = Callable[[argparse.ArgumentParser], None]
"""register_cli_command `setup_fn`: receives the command's subparser."""

CliHandler: TypeAlias = Callable[[argparse.Namespace], int]
"""register_cli_command `handler_fn`: becomes the subparser's `func`; returns the exit code."""


class HookCallback(Protocol):
    """A lifecycle hook: `callback(**payload)`; payload fields are untyped and grow additively."""

    def __call__(self, **kwargs: object) -> object: ...


class SlashCommand(Protocol):
    """A slash command: `handler(raw_args, **context)`; context: source, session_id, session_key,
    platform."""

    def __call__(self, raw_args: str, /, **context: object) -> str | None: ...


class StructuredResult(Protocol):
    """agent.plugin_llm.PluginLlmStructuredResult: `parsed` is the JSON answer when it parsed."""

    @property
    def parsed(self) -> object: ...


class PluginLlm(Protocol):
    """agent.plugin_llm.PluginLlm, the `ctx.llm` facade (keyword-only; unused options omitted)."""

    def complete_structured(
        self,
        *,
        instructions: str,
        input: Sequence[InputBlock],
        json_schema: JsonObject | None = None,
        schema_name: str | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        purpose: str | None = None,
        task: str | None = None,
    ) -> StructuredResult: ...


class PluginContext(Protocol):
    """The members of hermes_cli.plugins.PluginContext that Argus uses."""

    @property
    def llm(self) -> PluginLlm: ...

    def get_config(self, key: str, default: object = None) -> object: ...

    def register_hook(
        self, hook_name: str, callback: HookCallback, *, hold_stream: bool = False
    ) -> object: ...

    def register_cli_command(
        self,
        name: str,
        help: str,
        setup_fn: CliSetup,
        handler_fn: CliHandler | None = None,
        description: str = "",
    ) -> object: ...

    def register_command(
        self,
        name: str,
        handler: SlashCommand,
        description: str = "",
        args_hint: str = "",
        argument_mode: str | None = None,
    ) -> object: ...

    def register_auxiliary_task(
        self,
        key: str,
        *,
        display_name: str,
        description: str,
        defaults: dict[str, object] | None = None,
        inherit_from: str | None = None,
    ) -> object: ...


class LlmFn(Protocol):
    """derive.run's model call `(instructions, text, schema, purpose) -> answer`; any answer that is
    not a JSON object, or an exception, counts as a failed call."""

    def __call__(
        self, instructions: str, text: str, schema: JsonObject, purpose: str, /
    ) -> object: ...


class Llm(Protocol):
    """A model call whose answer is a JSON object, or None when the call failed."""

    def __call__(
        self, instructions: str, text: str, schema: JsonObject, purpose: str, /
    ) -> JsonObject | None: ...
