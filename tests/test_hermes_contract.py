"""Hermes' plugin API, as stubbed in typings/, satisfies the Protocols Argus is written against.

The check is static (mypy reads the TYPE_CHECKING block); `just stubtest` ties the stubs to the
Hermes runtime. At run time Argus' Protocols must not need Hermes.
"""

import subprocess
import sys
from typing import TYPE_CHECKING

from argus import hermes_api

if TYPE_CHECKING:
    from agent.plugin_llm import PluginLlm, PluginLlmStructuredResult
    from hermes_cli.plugins import PluginContext

    def _context(ctx: PluginContext) -> hermes_api.PluginContext:
        return ctx

    def _llm(llm: PluginLlm) -> hermes_api.PluginLlm:
        return llm

    def _result(result: PluginLlmStructuredResult) -> hermes_api.StructuredResult:
        return result


def test_contract_module_does_not_import_hermes() -> None:
    probe = (
        "import sys, argus.hermes_api\n"
        "print(sorted(m for m in ('hermes_cli', 'agent', 'tools') if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=60
    )
    assert result.stdout.strip() == "[]"
    assert hermes_api.PluginContext.__name__ == "PluginContext"
