import argparse
from collections.abc import Callable
from typing import Protocol

from agent.plugin_llm import PluginLlm
from hermes_cli.plugins_ledger import PluginRegistration as PluginRegistration

class _HookCallback(Protocol):
    # plugins_dispatch calls it with the hook payload as keyword arguments
    def __call__(self, **kwargs: object) -> object: ...

class _CommandHandler(Protocol):
    # plugin_command_dispatch.invoke_plugin_command passes the raw arguments, then the context
    def __call__(self, raw_args: str, /, **context: object) -> object: ...

class PluginContext:
    @property
    def llm(self) -> PluginLlm: ...
    def get_config(self, key: str, default: object = None) -> object: ...
    def register_hook(
        self, hook_name: str, callback: _HookCallback, *, hold_stream: bool = False
    ) -> PluginRegistration: ...
    def register_cli_command(
        self,
        name: str,
        help: str,
        setup_fn: Callable[[argparse.ArgumentParser], object],
        handler_fn: Callable[[argparse.Namespace], object] | None = None,
        description: str = "",
    ) -> PluginRegistration: ...
    def register_command(
        self,
        name: str,
        handler: _CommandHandler,
        description: str = "",
        args_hint: str = "",
        argument_mode: str | None = None,
    ) -> PluginRegistration | None: ...
    def register_auxiliary_task(
        self,
        key: str,
        *,
        display_name: str,
        description: str,
        defaults: dict[str, object] | None = None,
        inherit_from: str | None = None,
    ) -> PluginRegistration: ...

# public dispatch helpers, used by tests/hermes_integration_cases.py only
def discover_plugins(force: bool = False) -> None: ...
def has_hook(hook_name: str) -> bool: ...
def resolve_pre_tool_block(
    tool_name: str, args: dict[str, object] | None, **hook_kwargs: object
) -> str | None: ...
