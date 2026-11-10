"""apply writes an accepted proposal only as the person saw it, and only where Hermes can write: a
rule or diff that a later derive changed after the accept is refused until the proposal is accepted
again, and so is a skill in a read-only directory (a shared skills.external_dirs)."""

import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

from argus import apply, store


def _accepted(home: Path, shown_diff: str, current_diff: str) -> None:
    conn = store.open_db(home)
    try:
        conn.execute(
            "insert into proposal (id, skill_name, rule, diff, evidence, derived_by)"
            " values ('p1', 'sk', 'Check the lock first.', ?, '{}', 'test')",
            (current_diff,),
        )
        snap = {"skill": "sk", "rule": "Check the lock first.", "diff": shown_diff}
        conn.execute(
            "insert into decision (target_kind, target_id, action, target_snapshot, decided_at)"
            " values ('proposal', 'p1', 'accept', ?, ?)",
            (json.dumps(snap), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_diff_filled_after_the_accept_is_refused(tmp_path: Path) -> None:
    _accepted(tmp_path, shown_diff="", current_diff="@@ -1 +1,2 @@\n ctx\n+rm -rf build/\n")
    r = apply.run(tmp_path, "proposal", "p1")
    assert r["outcome"] == "refused"
    assert "diff changed since it was accepted" in r["message"]


def test_the_diff_that_was_shown_passes_the_check(tmp_path: Path) -> None:
    _accepted(tmp_path, shown_diff="no hunk", current_diff="no hunk")
    r = apply.run(tmp_path, "proposal", "p1")
    assert "changed since" not in r["message"]  # stopped later, by the diff itself


def test_a_read_only_skill_is_refused_before_hermes_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = tmp_path / "shared" / "domain" / "sk"
    skill.mkdir(parents=True)
    md = skill / "SKILL.md"
    md.write_text("# sk\n\nStep one.\n", encoding="utf-8")
    diff = "@@ -3 +3,2 @@\n Step one.\n+Check the lock first.\n"
    _accepted(tmp_path / "home", shown_diff=diff, current_diff=diff)
    called: list[str] = []
    smt = types.ModuleType("tools.skill_manager_tool")

    def find(name: str) -> dict[str, Path]:
        return {"path": skill}

    def manage(**kw: object) -> str:
        called.append("skill_manage")
        return "{}"

    smt._find_skill = find  # type: ignore[attr-defined]
    smt.skill_manage = manage  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", smt)
    md.chmod(0o444)
    skill.chmod(0o555)
    try:
        if os.access(md, os.W_OK):
            pytest.skip("running with permissions that ignore file modes")
        r = apply.run(tmp_path / "home", "proposal", "p1")
    finally:
        skill.chmod(0o755)
        md.chmod(0o644)
    assert r["outcome"] == "refused" and "read-only" in r["message"]
    assert called == []
