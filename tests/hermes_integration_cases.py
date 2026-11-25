"""The cases of tests/hermes_integration.py: each runs in its own process, which that runner
starts with HERMES_HOME set to a fresh temporary profile (Hermes reads its home when its modules
are imported, so this file imports Argus, and through it Hermes, only once that is set).

`python tests/hermes_integration_cases.py --list` prints the case names;
`python tests/hermes_integration_cases.py <case> <tmp dir>` runs one.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, cast

from hermes_cli import plugin_host_http_auth, plugins
from starlette.applications import Starlette
from starlette.requests import Request

from argus import apply, auto, checks, hermes_internals, store
from argus.dbtypes import count
from argus.jsontypes import JsonList, JsonObject

CONFIG = (
    "skills:\n  write_approval: false\n  guard_agent_created: false\n  project_discovery: false\n"
)
CONTEXT = "Step one."
RULE = "Check the lock first."
DIFF = f"@@ -6 +6,2 @@\n {CONTEXT}\n+{RULE}\n"


def _skill(root: Path, *parts: str, name: str | None = None, body: str = CONTEXT) -> Path:
    md = root.joinpath(*parts, "SKILL.md")
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(
        f"---\nname: {name or parts[-1]}\ndescription: integration fixture\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return md


def _profile(tmp: Path, *external: Path, extra: str = "") -> Path:
    home = tmp / "home"
    # the skeleton Hermes expects; a managed (Nix) Hermes does not create it, and its config
    # reads (e.g. skills.ledger) then fall back to defaults
    for sub in ("cron", "sessions", "logs/curator", "memories", "pairing", "hooks", "skills"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    for sub in ("image_cache", "audio_cache"):
        (home / sub).mkdir()
    listed = "".join(f"    - {e}\n" for e in external)
    cfg = CONFIG + (f"  external_dirs:\n{listed}" if external else "") + extra
    (home / "config.yaml").write_text(cfg, encoding="utf-8")
    return home


def _propose(
    home: Path, pid: str, name: str, diff: str | None, new: bool = False, rule: str = RULE
) -> None:
    path = store.skill_files(home).get(name)  # as derive records it
    file = store.skill_rel(home, path) if path is not None and not new else None
    conn = store.open_db(home)
    try:
        conn.execute(
            "insert into proposal (id, skill_name, file, rule, diff, evidence, derived_by,"
            " is_new_skill) values (?, ?, ?, ?, ?, '{}', 'integration', ?)",
            (pid, name, file, rule, diff, int(new)),
        )
        fields: dict[str, str] = {"skill": name, "rule": rule, "diff": diff or ""}
        snap = json.dumps(fields)
        conn.execute(
            "insert into decision (target_kind, target_id, action, target_snapshot, decided_at)"
            " values ('proposal', ?, 'accept', ?, ?)",
            (pid, snap, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _snapshot(*files: Path) -> dict[str, str]:
    return {str(f): f.read_text(encoding="utf-8") for f in files}


def _entry(entry_id: str | None) -> tuple[str, str, set[str]]:
    """`(actor, action, files)` of one ledger entry."""
    for e in hermes_internals.ledger_entries(limit=50):
        if e.get("id") == entry_id:
            files: list[object] = []
            for key in ("before", "after"):
                got = e.get(key)
                if isinstance(got, list):
                    files.extend(got)
            paths = {
                os.path.realpath(str(f.get("path")))
                for f in files
                if isinstance(f, dict) and f.get("path")
            }
            return str(e.get("actor")), str(e.get("action")), paths
    raise AssertionError(f"no ledger entry {entry_id}")


def _ledger_paths() -> list[set[str]]:
    out: list[set[str]] = []
    for e in hermes_internals.ledger_entries(limit=50):
        files: list[object] = []
        for key in ("before", "after"):
            got = e.get(key)
            if isinstance(got, list):
                files.extend(got)
        out.append(
            {
                os.path.realpath(str(f.get("path")))
                for f in files
                if isinstance(f, dict) and f.get("path")
            }
        )
    return out


def case_create_patch_repeat(tmp: Path) -> None:
    """The ordinary path: a new skill, a patch to it, the same patch again."""
    home = _profile(tmp)
    _propose(home, "c1", "itest", None, new=True)
    r = apply.run(home, "proposal", "c1")
    assert r["outcome"] == "applied", r
    md = home / "skills" / "itest" / "SKILL.md"
    assert md.read_text(encoding="utf-8").rstrip().endswith(RULE)
    diff = f"@@ -6,3 +6,4 @@\n # itest\n \n {RULE}\n+Then answer.\n"
    _propose(home, "p1", "itest", diff, rule="Then answer.")
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "applied", r
    assert _entry(r["ledger_entry"]) == ("user", "patch", {os.path.realpath(md)})
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused", r
    assert "already in" in r["message"], r


def case_patch_writes_the_checked_file(tmp: Path) -> None:
    """A unique skill in a category directory, with a decoy of another name next to it."""
    home = _profile(tmp)
    md = _skill(home / "skills", "cat", "sk")
    decoy = _skill(home / "skills", "cat", "sk-old", name="sk-old")
    before = _snapshot(decoy)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "applied", r
    assert RULE in md.read_text(encoding="utf-8")
    assert _snapshot(decoy) == before
    assert _entry(r["ledger_entry"]) == ("user", "patch", {os.path.realpath(md)})


def case_frontmatter_alias_across_tiers(tmp: Path) -> None:
    """locate_skill picks the local alias (frontmatter name sk), while the proposal was derived from
    the external directory called sk: apply refuses and changes nothing (round 1, B1)."""
    ext = tmp / "external"
    home = _profile(tmp, ext)
    alias = _skill(home / "skills", "alias", name="sk")
    other = _skill(ext, "sk", body="Step   one.")
    before = _snapshot(alias, other)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused", r
    assert _snapshot(alias, other) == before
    assert not any(_ledger_paths())


def case_symlinked_directory_hiding_another(tmp: Path) -> None:
    ext, elsewhere = tmp / "external", tmp / "elsewhere"
    home = _profile(tmp, ext)
    real = _skill(elsewhere, "sk")
    (home / "skills" / "sk").symlink_to(real.parent, target_is_directory=True)
    other = _skill(ext, "sk")
    before = _snapshot(real, other)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused", r
    assert _snapshot(real, other) == before


def case_category_skill_with_an_alias_elsewhere(tmp: Path) -> None:
    """A skill in a category is written by its path (`c/sk`): an external frontmatter alias of the
    name, with stale context, stays untouched."""
    ext = tmp / "external"
    home = _profile(tmp, ext)
    md = _skill(home / "skills", "c", "sk")
    alias = _skill(ext, "alias", name="sk", body="Step   one.")
    before = _snapshot(alias)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "applied", r
    assert RULE in md.read_text(encoding="utf-8")
    assert _snapshot(alias) == before
    assert _entry(r["ledger_entry"]) == ("user", "patch", {os.path.realpath(md)})


def case_namesake_in_another_category(tmp: Path) -> None:
    """Argus keys a skill by its directory name: c/sk next to d/sk is refused, nothing written."""
    home = _profile(tmp)
    md = _skill(home / "skills", "c", "sk")
    other = _skill(home / "skills", "d", "sk", body="Step   one.")
    before = _snapshot(md, other)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused", r
    assert _snapshot(md, other) == before


def case_namesake_in_a_dist_backup(tmp: Path) -> None:
    """Hermes 0.22.5 still enumerates .hermes-dist backups: one called sk refuses the apply."""
    home = _profile(tmp)
    md = _skill(home / "skills", "sk")
    backup = _skill(home / "skills", ".hermes-dist", "old", "sk")
    before = _snapshot(md, backup)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused", r
    assert _snapshot(md, backup) == before


def case_symlinked_skill_file(tmp: Path) -> None:
    home = _profile(tmp)
    real = _skill(tmp / "elsewhere", "sk")
    (home / "skills" / "c" / "sk").mkdir(parents=True)
    (home / "skills" / "c" / "sk" / "SKILL.md").symlink_to(real)
    before = _snapshot(real)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused", r
    assert _snapshot(real) == before


def case_staged_once_with_write_approval(tmp: Path) -> None:
    """With skills.write_approval on, apply stages the patch once; a second apply finds it."""
    home = _profile(tmp)
    cfg = (home / "config.yaml").read_text(encoding="utf-8")
    (home / "config.yaml").write_text(
        cfg.replace("write_approval: false", "write_approval: true"), encoding="utf-8"
    )
    md = _skill(home / "skills", "c", "sk")
    before = _snapshot(md)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "staged", r
    assert r.get("pending_id"), r
    again = apply.run(home, "proposal", "p1", force=True)
    assert again["outcome"] == "refused", again
    assert str(r.get("pending_id")) in again["message"], again
    assert _snapshot(md) == before


def case_top_level_skill_with_a_namesake(tmp: Path) -> None:
    """Directly in skills/ skill_manage matches the bare name against every directory: another
    directory called sk anywhere refuses the apply."""
    ext = tmp / "external"
    home = _profile(tmp, ext)
    md = _skill(home / "skills", "sk")
    other = _skill(ext, "d", "sk", body="Step   one.")
    before = _snapshot(md, other)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused", r
    assert _snapshot(md, other) == before


def case_skill_in_an_excluded_create_dir(tmp: Path) -> None:
    """create_dir under .venv: Hermes' writer skips it and would fall back to an external
    frontmatter alias (round 3, B1)."""
    ext, created = tmp / "external", tmp / ".venv" / "package" / "skills"
    home = _profile(tmp, ext, extra=f"  create_dir: {created}\n")
    md = _skill(created, "sk")
    alias = _skill(ext, "alias", name="sk", body="Step   one.")
    before = _snapshot(md, alias)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused", r
    assert _snapshot(md, alias) == before


def case_symlink_hiding_a_deeper_namesake(tmp: Path) -> None:
    """A differently named link to a deeper directory called sk must not hide it from the count
    (round 3, B2)."""
    home = _profile(tmp)
    md = _skill(home / "skills", "sk", name="other")
    deep = _skill(home / "skills", "category-005", "inner", "sk", name="other")
    (home / "skills" / "link-005").symlink_to(deep.parent, target_is_directory=True)
    before = _snapshot(md, deep)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused", r
    assert _snapshot(md, deep) == before


def case_create_refused_when_the_name_exists(tmp: Path) -> None:
    ext = tmp / "external"
    home = _profile(tmp, ext)
    other = _skill(ext, "d", "sk")
    before = _snapshot(other)
    _propose(home, "c1", "sk", None, new=True)
    r = apply.run(home, "proposal", "c1")
    assert r["outcome"] == "refused", r
    assert _snapshot(other) == before
    assert not (home / "skills" / "sk").exists()


def case_symlinked_skill_with_an_alias_elsewhere(tmp: Path) -> None:
    """skill_manage does not enumerate a symlinked directory, so it would fall back to the external
    frontmatter alias: apply must refuse (the second review's B1)."""
    ext, elsewhere = tmp / "external", tmp / "elsewhere"
    home = _profile(tmp, ext)
    real = _skill(elsewhere, "real", name="sk")
    (home / "skills" / "sk").symlink_to(real.parent, target_is_directory=True)
    alias = _skill(ext, "alias", name="sk", body="Step   one.")
    before = _snapshot(real, alias)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused", r
    assert _snapshot(real, alias) == before


def _project_overlap(tmp: Path, as_create_dir: bool) -> None:
    """A trusted project's skill dir that the profile also configures comes back from Hermes as a
    project dir, before `skills/`, while running inside the project; skill_manage still writes
    there."""
    ext, project = tmp / "external", tmp / "project"
    (project / ".git").mkdir(parents=True)
    pskills = project / ".hermes" / "skills"
    trusted = f"  trusted_project_dirs:\n    - {project}\n"
    if as_create_dir:
        home = _profile(tmp, ext, extra=trusted + f"  create_dir: {pskills}\n")
    else:
        home = _profile(tmp, pskills, ext, extra=trusted)
    cfg = (home / "config.yaml").read_text(encoding="utf-8")
    (home / "config.yaml").write_text(
        cfg.replace("project_discovery: false", "project_discovery: true"), encoding="utf-8"
    )
    inproject = _skill(pskills, "sk", body="Step   one.")
    checked = _skill(ext, "sk")
    before = _snapshot(inproject, checked)
    _propose(home, "p1", "sk", DIFF)
    os.chdir(project)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused", r
    assert _snapshot(inproject, checked) == before


def case_project_dir_also_create_dir(tmp: Path) -> None:
    _project_overlap(tmp, as_create_dir=True)


def case_project_dir_also_external(tmp: Path) -> None:
    _project_overlap(tmp, as_create_dir=False)


def case_no_ledger_evidence_is_unknown(tmp: Path) -> None:
    """With the ledger off nothing proves which file skill_manage wrote: unknown, not applied."""
    home = _profile(tmp, extra="  ledger: false\n")
    _skill(home / "skills", "cat", "sk")
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "unknown", r
    assert "no ledger entry" in r["message"], r


def case_support_copy_does_not_block(tmp: Path) -> None:
    """An example skill inside another skill's references/ is not a skill for Hermes."""
    home = _profile(tmp)
    md = _skill(home / "skills", "cat", "sk")
    _skill(home / "skills", "parent")
    example = _skill(home / "skills", "parent", "references", "sk", body="Example.")
    before = _snapshot(example)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "applied", r
    assert RULE in md.read_text(encoding="utf-8")
    assert _snapshot(example) == before


def case_every_profile_from_one_process(tmp: Path) -> None:
    """Export and derive read all profiles from one process: each gets its own roots."""
    ext = tmp / "external"
    a = _profile(tmp / "a", ext)
    b = _profile(tmp / "b")
    shared = _skill(ext, "d", "shared")
    own = _skill(b / "skills", "c", "own")
    for _ in range(2):
        for home, want in ((a, {"shared": shared}), (b, {"own": own})):
            assert store.skill_files(home) == want, (home, store.skill_files(home))


def _auto_proposal(home: Path, pid: str, rule: str, intents: JsonList) -> None:
    """A proposal of the latest pass that the auto_apply policy takes: high, additive, 2 intents."""
    ids: JsonList = [pid]
    evidence: JsonObject = {"intents": intents}
    path = store.skill_files(home)["sk"]
    conn = store.open_db(home)
    try:
        conn.execute(
            "insert into proposal (id, skill_name, file, rule, diff, confidence, evidence,"
            " derived_by) values (?, 'sk', ?, ?, ?, 'high', ?, 'integration')",
            (
                pid,
                store.skill_rel(home, path),
                rule,
                f"@@ -6 +6,2 @@\n {CONTEXT}\n+{rule}\n",
                json.dumps(evidence),
            ),
        )
        store.meta_set(conn, auto.LAST_PASS, json.dumps(ids))
        conn.commit()
    finally:
        conn.close()


def case_auto_apply_then_a_rollback_stops_it(tmp: Path) -> None:
    """auto-apply writes through skill_manage as actor user and records `auto`; once the person
    rolls the write back, a later lesson for that skill is left to them."""
    home = _profile(tmp)
    md = _skill(home / "skills", "cat", "sk")
    _auto_proposal(home, "p1", RULE, ["i1", "i2"])
    [(_p, r)] = auto.run(home, per_week=3)
    assert r["outcome"] == "applied", r
    assert _entry(r["ledger_entry"]) == ("user", "patch", {os.path.realpath(md)})
    assert RULE in md.read_text(encoding="utf-8")
    ok, msg = hermes_internals.rollback_as_user(str(r["ledger_entry"]))
    assert ok, msg
    assert RULE not in md.read_text(encoding="utf-8")
    conn = store.open_db(home)
    try:
        assert auto.stopped_skills(conn, home) == {"sk"}
    finally:
        conn.close()
    _auto_proposal(home, "p2", "Say what you checked.", ["i3", "i4"])
    before = _snapshot(md)
    assert auto.run(home, per_week=3) == []
    assert _snapshot(md) == before


def _events(home: Path, want: int) -> int:
    """check_event rows, once the hook's writer (in the plugin as Hermes loaded it) wrote `want`."""
    n = 0
    for _ in range(100):
        conn = sqlite3.connect(store.db_path(home))
        try:
            n = count(conn, "select count(*) from check_event")
        finally:
            conn.close()
        if n >= want:
            break
        time.sleep(0.05)
    return n


def _person() -> str | None:
    return None


def _no_cache(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n == "__pycache__"}


def case_pre_tool_call_enforces_an_approved_check(tmp: Path) -> None:
    """Argus as Hermes loads it (plugin.yaml, register): Hermes' own pre_tool_call dispatch lets
    an observed check's call run, and blocks it once a person set the check to enforce."""
    home = _profile(tmp, extra="plugins:\n  enabled:\n    - argus\n")
    src = Path(__file__).resolve().parent.parent / "argus"
    shutil.copytree(src, home / "plugins" / "argus", ignore=_no_cache)
    _propose(home, "p1", "sk", None, rule="Never force-push.")
    # the person gate refuses this test process (no TTY, maybe inside an agent): a person is
    # assumed here; the gate is unit-tested (tests/test_checks.py)
    setattr(checks, "refusal_here", _person)  # noqa: B010
    conn = store.open_db(home)
    try:
        answer: checks.DraftAnswer = {
            "checkable": True,
            "reason": "one terminal command",
            "tool": "terminal",
            "field": "command",
            "match": "command",
            "program": "git",
            "args": ["push"],
            "flags": ["--force", "-f"],
        }
        assert checks.save_draft(conn, "p1", answer) == "drafted"
        draft = checks.draft_of(conn, "p1")
        assert draft is not None
        sha = str(draft["sha256"])
        checks.approve(conn, home, "p1", sha, checks.at_terminal())
    finally:
        conn.close()
    plugins.discover_plugins()
    assert plugins.has_hook("pre_tool_call")
    push: dict[str, object] = {"command": "git push origin main --force"}
    assert plugins.resolve_pre_tool_block("terminal", push, tool_call_id="c1") is None  # observe
    assert _events(home, 1) == 1
    conn = store.open_db(home)
    try:
        checks.set_mode(conn, home, "p1", "enforce", sha, checks.at_terminal())
    finally:
        conn.close()
    msg = plugins.resolve_pre_tool_block("terminal", push, tool_call_id="c2")
    assert msg is not None, "not blocked"
    assert "Argus blocked this terminal call: check p1 v1" in msg, msg
    assert "checks observe p1" in msg, msg
    status: dict[str, object] = {"command": "git status"}
    assert plugins.resolve_pre_tool_block("terminal", status, tool_call_id="c3") is None
    assert plugins.resolve_pre_tool_block("read_file", push, tool_call_id="c4") is None
    assert _events(home, 2) == 2


class _DashboardServer(Protocol):
    """`hermes_cli.web_server` as this case reads it: the dashboard's FastAPI app."""

    app: Starlette


def _dashboard_request(server: _DashboardServer, token: str) -> Request:
    scope: dict[str, object] = {
        "type": "http",
        "method": "POST",
        "path": "/api/plugins/argus/check",
        "query_string": b"",
        "headers": [(b"x-hermes-session-token", token.encode())],
        "app": server.app,
    }
    return Request(scope)


def _refusal(fn: Callable[[], object]) -> str:
    """The ApprovalError `fn` raises (AssertionError when it does not)."""
    try:
        fn()
    except checks.ApprovalError as e:
        return str(e)
    raise AssertionError("not refused")


def case_dashboard_proof_is_hermes_route_guard(tmp: Path) -> None:
    """checks.on_dashboard asks Hermes' own dashboard route guard (web_server._require_token):
    in a process that serves the dashboard, a request with its session token is a person's proof
    and approves; a wrong token, or a process without the dashboard, is refused."""
    home = _profile(tmp)
    _propose(home, "p1", "sk", None, rule="Never force-push.")
    # this test process may run inside an agent: the session part of the gate is unit-tested
    setattr(checks, "agent_session", _person)  # noqa: B010
    empty: dict[str, object] = {"type": "http", "headers": []}
    probe = Request(empty)
    assert not hermes_internals.dashboard_request_authorized(probe)  # no dashboard here yet
    os.environ["HERMES_DASHBOARD_SESSION_TOKEN"] = "argus-integration-token"
    server = cast("_DashboardServer", importlib.import_module("hermes_cli.web_server"))
    refused = _refusal(lambda: checks.on_dashboard(_dashboard_request(server, "wrong")))
    assert "not a request the Hermes dashboard authenticated" in refused, refused
    who = checks.on_dashboard(_dashboard_request(server, "argus-integration-token"))
    conn = store.open_db(home)
    try:
        answer: checks.DraftAnswer = {
            "checkable": True,
            "reason": "one terminal command",
            "tool": "terminal",
            "field": "command",
            "match": "command",
            "program": "git",
            "args": ["push"],
            "flags": ["--force", "-f"],
        }
        assert checks.save_draft(conn, "p1", answer) == "drafted"
        draft = checks.draft_of(conn, "p1")
        assert draft is not None
        row = checks.approve(conn, home, "p1", str(draft["sha256"]), who)
        assert row["decided_by"] == "dashboard", row
    finally:
        conn.close()


def case_hosted_dashboard_proof_uses_hermes_admission(tmp: Path) -> None:
    """With `plugins.isolation: host`, the Argus API runs in Hermes' plugin host and Hermes hands
    it the dashboard server's guard decision in the request's ASGI state: on_dashboard accepts a
    request Hermes admitted and refuses one it did not."""
    _profile(tmp)
    setattr(checks, "agent_session", _person)  # noqa: B010
    seen: list[str] = []

    async def app(
        scope: dict[str, object],
        receive: Callable[[], Awaitable[object]],
        send: Callable[[object], Awaitable[None]],
    ) -> None:
        request = Request(scope)
        try:
            checks.on_dashboard(request)
            seen.append("admitted")
        except checks.ApprovalError as e:
            seen.append(str(e))

    async def receive() -> object:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: object) -> None:
        return None

    for authenticated in (True, False):
        scoped = plugin_host_http_auth.with_hosted_admission(app, authenticated)
        asyncio.run(scoped({"type": "http", "headers": []}, receive, send))
    assert seen[0] == "admitted", seen
    assert "not a request the Hermes dashboard authenticated" in seen[1], seen


CASES: dict[str, Callable[[Path], None]] = {
    f.__name__.removeprefix("case_"): f
    for f in (
        case_create_patch_repeat,
        case_patch_writes_the_checked_file,
        case_frontmatter_alias_across_tiers,
        case_symlinked_directory_hiding_another,
        case_category_skill_with_an_alias_elsewhere,
        case_namesake_in_another_category,
        case_namesake_in_a_dist_backup,
        case_symlinked_skill_file,
        case_staged_once_with_write_approval,
        case_top_level_skill_with_a_namesake,
        case_skill_in_an_excluded_create_dir,
        case_symlink_hiding_a_deeper_namesake,
        case_create_refused_when_the_name_exists,
        case_symlinked_skill_with_an_alias_elsewhere,
        case_project_dir_also_create_dir,
        case_project_dir_also_external,
        case_no_ledger_evidence_is_unknown,
        case_support_copy_does_not_block,
        case_every_profile_from_one_process,
        case_auto_apply_then_a_rollback_stops_it,
        case_pre_tool_call_enforces_an_approved_check,
        case_dashboard_proof_is_hermes_route_guard,
        case_hosted_dashboard_proof_uses_hermes_admission,
    )
}


def main() -> int:
    if sys.argv[1:] == ["--list"]:
        print("\n".join(CASES))
        return 0
    CASES[sys.argv[1]](Path(sys.argv[2]))  # HERMES_HOME already set by the runner
    return 0


if __name__ == "__main__":
    sys.exit(main())
