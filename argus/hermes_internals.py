"""Hermes, as Argus calls it: the only module that imports Hermes.

Argus runs inside Hermes, but CI and the type checks import it without Hermes, and the modules
below are Hermes internals (not the documented plugin API) that a release may move or rename. Each
is imported once, here, at the top; one that cannot be imported (ImportError) is None, and only
what needs it fails, where it is called: apply answers `failed` with the command to copy, skill
lookups fall back to the profile's `skills/`, the home falls back to HERMES_HOME. A missing or
renamed module does not fail plugin load; a Hermes module that raises another error while
importing still does, as it would for Hermes itself.

Tests replace the module variables (CONSTANTS, SKILLS_TOOL, SKILL_LEDGER, SKILL_MANAGER) with
fakes. The signatures below are those of `typings/`, checked against Hermes with stubtest.

`tools.skill_manager_tool` and `tools.skills_tool` read HERMES_HOME when imported, so a process
that writes a profile's skills must run with that profile's HERMES_HOME from its start (the CLI's
`-p`, the dashboard's apply child): importing them later would not change that.
"""

from __future__ import annotations

import os
from contextvars import Token
from pathlib import Path
from typing import Protocol, TypeVar

T = TypeVar("T")


class Constants(Protocol):
    """`hermes_constants`."""

    def get_hermes_home(self) -> Path: ...
    def set_hermes_home_override(self, path: str | Path | None) -> Token[str | object]: ...
    def reset_hermes_home_override(self, token: Token[str | object]) -> None: ...


class SkillsTool(Protocol):
    """`tools.skills_tool` (published in Hermes' plugin_api.json)."""

    def skill_search_dirs(self) -> tuple[list[tuple[int, Path]], Path]: ...
    def locate_skill(
        self, name: str, local_category_name: str | None, roots: list[tuple[int, Path]]
    ) -> tuple[str | None, Path | None, Path | None]: ...


class SkillLedger(Protocol):
    """`tools.skill_ledger` (private: catalog rule 9 warns about it)."""

    def set_ledger_actor(self, actor: str | None) -> Token[str | None]: ...
    def reset_ledger_actor(self, token: Token[str | None]) -> None: ...
    def list_entries(
        self, skill: str | None = None, limit: int | None = None
    ) -> list[dict[str, object]]: ...
    def rollback_entry(self, entry_id: str) -> tuple[bool, str]: ...


class SkillManager(Protocol):
    """`tools.skill_manager_tool` (private: catalog rule 9 warns about it)."""

    def skill_manage(
        self,
        action: str,
        name: str,
        content: str | None = None,
        category: str | None = None,
        file_path: str | None = None,
        file_content: str | None = None,
        old_string: str | None = None,
        new_string: str | None = None,
        replace_all: bool = False,
        absorbed_into: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        operations: list[dict[str, object]] | None = None,
    ) -> str: ...


CONSTANTS: Constants | None
SKILLS_TOOL: SkillsTool | None
SKILL_LEDGER: SkillLedger | None
SKILL_MANAGER: SkillManager | None
try:
    import hermes_constants

    CONSTANTS = hermes_constants
except ImportError:
    CONSTANTS = None
try:
    from tools import skills_tool

    SKILLS_TOOL = skills_tool
except ImportError:
    SKILLS_TOOL = None
try:
    from tools import skill_ledger

    SKILL_LEDGER = skill_ledger
except ImportError:
    SKILL_LEDGER = None
try:
    from tools import skill_manager_tool

    SKILL_MANAGER = skill_manager_tool
except ImportError:
    SKILL_MANAGER = None


class HermesUnavailableError(RuntimeError):
    """A Hermes module Argus needs is not importable in this process."""


def need(module: T | None, name: str) -> T:
    if module is None:
        raise HermesUnavailableError(f"Hermes module {name} is not importable here")
    return module


def hermes_home() -> Path:
    """The Hermes home of this process: Hermes' answer, else HERMES_HOME, else ~/.hermes."""
    if CONSTANTS is not None:
        try:
            return Path(CONSTANTS.get_hermes_home())
        except Exception:  # any failure: the environment still names the home
            pass
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def skill_search_dirs(home: Path) -> tuple[list[tuple[int, Path]], Path]:
    """`(tier, dir)` of every skill root Hermes reports for the profile at `home`, and its own
    `skills/`. Hermes answers for its home in force, so the profile's home is set as an override
    for the call (export and derive read every profile from one process)."""
    constants = need(CONSTANTS, "hermes_constants")
    tool = need(SKILLS_TOOL, "tools.skills_tool")
    tok = constants.set_hermes_home_override(str(home))
    try:
        roots, active = tool.skill_search_dirs()
    finally:
        constants.reset_hermes_home_override(tok)
    return [(t, Path(d)) for t, d in roots], Path(active)


def locate_skill(
    name: str, roots: list[tuple[int, Path]]
) -> tuple[str | None, Path | None, Path | None]:
    """Hermes' published lookup of skill `name` among `roots`: (error JSON, dir, file)."""
    return need(SKILLS_TOOL, "tools.skills_tool").locate_skill(name, None, roots)


def ledger_entries(skill: str | None = None, limit: int | None = None) -> list[dict[str, object]]:
    """The curator ledger of this process' profile, newest first."""
    return need(SKILL_LEDGER, "tools.skill_ledger").list_entries(skill=skill, limit=limit)


def rollback_as_user(entry_id: str) -> tuple[bool, str]:
    """`rollback_entry` with the ledger actor `user`."""
    ledger = need(SKILL_LEDGER, "tools.skill_ledger")
    tok = ledger.set_ledger_actor("user")
    try:
        return ledger.rollback_entry(entry_id)
    finally:
        ledger.reset_ledger_actor(tok)


def skill_manage_as_user(
    action: str,
    name: str,
    content: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
) -> str:
    """`skill_manage` (create or patch of SKILL.md) with the ledger actor `user`."""
    ledger = need(SKILL_LEDGER, "tools.skill_ledger")
    manager = need(SKILL_MANAGER, "tools.skill_manager_tool")
    tok = ledger.set_ledger_actor("user")
    try:
        if action == "create":
            return manager.skill_manage("create", name, content=content)
        return manager.skill_manage(
            "patch", name, old_string=old_string, new_string=new_string, file_path=None
        )
    finally:
        ledger.reset_ledger_actor(tok)
