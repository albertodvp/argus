"""Argus — read a Hermes agent by intents and govern what it learns.

Hooks capture what Hermes does not persist; `hermes argus backfill|derive` read past sessions and
derive intents with the `argus` auxiliary model slot; the dashboard tab shows the result.
"""

from __future__ import annotations

from typing import Final

from . import capture, cli
from .hermes_api import HookCallback, PluginContext

HOOKS: Final[dict[str, HookCallback]] = {
    "pre_llm_call": capture.pre_llm_call,
    "post_tool_call": capture.post_tool_call,
    "on_session_end": capture.on_session_end,
    "on_skill_lifecycle": capture.on_skill_lifecycle,
    "on_session_delete": capture.on_session_delete,
}


def register(ctx: PluginContext) -> None:
    for name, fn in HOOKS.items():
        ctx.register_hook(name, fn)
    capture.after_turn = cli.IdleScheduler(ctx).turn_ended
    ctx.register_auxiliary_task(
        "argus",
        display_name="Argus",
        description="Intent assignment, consolidation and judgement, skill-edit review and "
        "proposals (hermes argus derive)",
        inherit_from="curator",
    )
    ctx.register_cli_command(
        "argus",
        help="argus: backfill, derive, status, export, apply",
        setup_fn=cli.setup,
        handler_fn=cli.make_handler(ctx),
        description="Read the agent by intents and review what it learns.",
    )
    ctx.register_command(
        "argus", cli.slash, description="Intent of this session and pending reviews"
    )
