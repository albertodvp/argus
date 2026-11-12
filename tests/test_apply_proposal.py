"""apply writes an accepted proposal only as the person saw it, and only where Hermes can write: a
rule or diff that a later derive changed after the accept is refused until the proposal is accepted
again, and so is a skill in a read-only directory (a shared skills.external_dirs)."""

import json
import os
import time
import types
from pathlib import Path
from typing import Final

import pytest

from argus import apply, export, hermes_internals, store
from argus.dbtypes import ProposalRow
from argus.exporttypes import ApplyAttempt


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


def _hermes(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    located: str | Path,
    called: list[str],
    roots: list[tuple[int, Path]] | None = None,
) -> None:
    """Hermes' published skill lookup answering `located` for every name: a SKILL.md path, or the
    JSON error `locate_skill` returns; `skill_manage` records that it was called. `roots` are the
    profile's skill roots (default `<home>/skills`)."""
    roots = roots if roots is not None else [(1, home / "skills")]
    constants = types.ModuleType("hermes_constants")

    def override(path: object) -> None:
        return None

    constants.set_hermes_home_override = override  # type: ignore[attr-defined]
    constants.reset_hermes_home_override = override  # type: ignore[attr-defined]
    st = types.ModuleType("tools.skills_tool")

    def search_dirs() -> tuple[list[tuple[int, Path]], Path]:
        return roots, home / "skills"

    def locate(
        name: str, local: str | None, given: list[tuple[int, Path]]
    ) -> tuple[str | None, Path | None, Path | None]:
        start = [d for _t, d in roots].index(home / "skills")
        assert given == roots[start:]  # Argus leaves out the project dirs that precede skills/
        if isinstance(located, Path):
            return None, located.parent, located
        return located, None, None

    st.skill_search_dirs = search_dirs  # type: ignore[attr-defined]
    st.locate_skill = locate  # type: ignore[attr-defined]
    smt = types.ModuleType("tools.skill_manager_tool")

    def manage(*a: object, **kw: object) -> str:
        called.append("skill_manage")
        return "{}"

    smt.skill_manage = manage  # type: ignore[attr-defined]
    monkeypatch.setattr(hermes_internals, "CONSTANTS", constants)
    monkeypatch.setattr(hermes_internals, "SKILLS_TOOL", st)
    monkeypatch.setattr(hermes_internals, "SKILL_MANAGER", smt)


# locate_skill's JSON answers (tools/skills_tool.py, unchanged from 0.22.5 to 0.23.1)
AMBIGUOUS: Final = (
    '{"success": false, "error": "Ambiguous skill name \'sk\': use one of a/sk, b/sk. 2 different '
    'skills share this name in the same skills directory tier; refusing to guess.", '
    '"matches": ["/s/a/sk/SKILL.md", "/s/b/sk/SKILL.md"]}'
)
NOT_FOUND: Final = (
    '{"success": false, "error": "Skill \'sk\' not found.", "available_skills": ["other"]}'
)


def test_a_name_hermes_cannot_resolve_to_one_skill_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """skill_manage would pick one of the namesakes by its own rules: never patch a guess."""
    diff = "@@ -3 +3,2 @@\n Step one.\n+Check the lock first.\n"
    _accepted(tmp_path / "home", shown_diff=diff, current_diff=diff)
    called: list[str] = []
    _hermes(monkeypatch, tmp_path / "home", AMBIGUOUS, called)
    r = apply.run(tmp_path / "home", "proposal", "p1")
    assert r["outcome"] == "refused"
    assert "cannot resolve skill sk" in r["message"]
    assert "refusing to guess" in r["message"]
    assert called == []


def test_a_skill_that_is_gone_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    diff = "@@ -3 +3,2 @@\n Step one.\n+Check the lock first.\n"
    _accepted(tmp_path / "home", shown_diff=diff, current_diff=diff)
    called: list[str] = []
    _hermes(monkeypatch, tmp_path / "home", NOT_FOUND, called)
    r = apply.run(tmp_path / "home", "proposal", "p1")
    assert r["outcome"] == "refused"
    assert "not found" in r["message"]
    assert called == []


def _new_skill_proposal(home: Path) -> None:
    conn = store.open_db(home)
    try:
        conn.execute(
            "insert into proposal (id, skill_name, rule, evidence, derived_by, is_new_skill)"
            " values ('p2', 'sk', 'Check the lock first.', '{}', 'test', 1)"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("located", "outcome"), [(NOT_FOUND, "dry-run"), (AMBIGUOUS, "refused"), ("exists", "refused")]
)
def test_a_new_skill_is_created_only_when_hermes_finds_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, located: str, outcome: str
) -> None:
    home = tmp_path / "home"
    _new_skill_proposal(home)
    md = home / "skills" / "c" / "sk" / "SKILL.md"
    if located == "exists":
        md.parent.mkdir(parents=True)
        md.write_text("# sk\n", encoding="utf-8")
    called: list[str] = []
    _hermes(monkeypatch, home, md if located == "exists" else located, called)
    r = apply.run(home, "proposal", "p2", dry_run=True)
    assert r["outcome"] == outcome, r["message"]
    if outcome == "refused":
        assert "exists now" in r["message"] or "cannot confirm" in r["message"]
    assert called == []


DIFF: Final = "@@ -3 +3,2 @@\n Step one.\n+Check the lock first.\n"


def _skill_md(root: Path, *parts: str, body: str = "# sk\n\nStep one.\n") -> Path:
    md = root.joinpath(*parts, "SKILL.md")
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(body, encoding="utf-8")
    return md


def _manage_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace the faked skill_manage with one recording `(action, name)` and answering success,
    and fake the ledger it would write (no entries: the outcome is then `unknown`)."""
    calls: list[tuple[str, str]] = []

    def manage(action: str, name: str, **kw: object) -> str:
        calls.append((action, name))
        return '{"success": true}'

    smt = types.ModuleType("tools.skill_manager_tool")
    smt.skill_manage = manage  # type: ignore[attr-defined]
    monkeypatch.setattr(hermes_internals, "SKILL_MANAGER", smt)
    sl = types.ModuleType("tools.skill_ledger")

    def entries(skill: str | None = None, limit: int | None = None) -> list[dict[str, object]]:
        return []

    def actor(a: object) -> None:
        return None

    sl.list_entries = entries  # type: ignore[attr-defined]
    sl.set_ledger_actor = actor  # type: ignore[attr-defined]
    sl.reset_ledger_actor = actor  # type: ignore[attr-defined]
    monkeypatch.setattr(hermes_internals, "SKILL_LEDGER", sl)
    return calls


def test_a_read_only_skill_is_refused_before_hermes_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    md = _skill_md(home / "skills", "domain", "sk")
    _accepted(home, shown_diff=DIFF, current_diff=DIFF)
    called: list[str] = []
    _hermes(monkeypatch, home, md, called)
    md.chmod(0o444)
    md.parent.chmod(0o555)
    try:
        if os.access(md, os.W_OK):
            pytest.skip("running with permissions that ignore file modes")
        r = apply.run(home, "proposal", "p1")
    finally:
        md.parent.chmod(0o755)
        md.chmod(0o644)
    assert r["outcome"] == "refused"
    assert "read-only" in r["message"]
    assert called == []


def test_a_skill_in_a_category_is_written_by_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """skill_manage resolves `cat/sk` by its path under skills/: the name given pins the file."""
    home = tmp_path / "home"
    _skill_md(home / "skills", "c", "sk")
    md = home / "skills" / "c" / "sk" / "SKILL.md"
    _accepted(home, shown_diff=DIFF, current_diff=DIFF)
    called: list[str] = []
    _hermes(monkeypatch, home, md, called)
    calls = _manage_calls(monkeypatch)
    r = apply.run(home, "proposal", "p1")
    assert calls == [("patch", "c/sk")]
    assert r["outcome"] == "unknown"  # the fake ledger has no entry naming the file


@pytest.mark.parametrize("namesake", [("d", "sk"), (".hermes-dist", "old", "sk")])
def test_a_namesake_directory_anywhere_refuses_the_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, namesake: tuple[str, ...]
) -> None:
    """Argus keys a skill by its directory name, and an older skill_manage still enumerates
    .hermes-dist backups: any other directory called sk refuses, even for `c/sk`."""
    home = tmp_path / "home"
    md = _skill_md(home / "skills", "c", "sk")
    other = _skill_md(home / "skills", *namesake, body="# other\n")
    _accepted(home, shown_diff=DIFF, current_diff=DIFF)
    called: list[str] = []
    _hermes(monkeypatch, home, md, called)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused"
    assert str(other) in r["message"]
    assert called == []


def test_a_patch_staged_under_the_path_name_is_not_staged_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    md = _skill_md(home / "skills", "c", "sk")
    _accepted(home, shown_diff=DIFF, current_diff=DIFF)
    pending = home / "pending" / "skills"
    pending.mkdir(parents=True)
    payload: dict[str, str] = {
        "action": "patch",
        "name": "c/sk",
        "old_string": "Step one.",
        "new_string": "Step one.\nCheck the lock first.",
    }
    wrapped: dict[str, dict[str, str]] = {"payload": payload}
    (pending / "w1.json").write_text(json.dumps(wrapped), encoding="utf-8")
    called: list[str] = []
    _hermes(monkeypatch, home, md, called)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused", r["message"]
    assert "w1" in r["message"], r["message"]
    assert called == []


def test_a_top_level_skill_needs_a_unique_directory_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directly in skills/ the name is the bare name, which skill_manage matches against every
    directory it enumerates: any other directory with it, in any root, refuses the apply."""
    home, ext = tmp_path / "home", tmp_path / "ext"
    md = _skill_md(home / "skills", "sk")
    other = _skill_md(ext, "d", "sk", body="# other\n")
    _accepted(home, shown_diff=DIFF, current_diff=DIFF)
    called: list[str] = []
    _hermes(monkeypatch, home, md, called, roots=[(1, home / "skills"), (3, ext)])
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused"
    assert str(other) in r["message"]
    assert called == []


def test_a_project_dir_counts_for_the_unique_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trusted project dir the profile also configures comes back from Hermes as a project dir,
    before `skills/`, which Argus' reads leave out; skill_manage still enumerates it, so it counts
    here."""
    home, project = tmp_path / "home", tmp_path / "repo" / ".hermes" / "skills"
    md = _skill_md(home / "skills", "sk")
    _skill_md(project, "sk", body="# project\n")
    _accepted(home, shown_diff=DIFF, current_diff=DIFF)
    called: list[str] = []
    _hermes(monkeypatch, home, md, called, roots=[(0, project), (1, home / "skills")])
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused"
    assert "not the only directory" in r["message"]
    assert called == []


@pytest.mark.parametrize(
    "where", ["external", "symlink", "leaf-symlink", "venv", "dist", "support"]
)
def test_a_skill_skill_manage_cannot_reach_by_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    roots = [(1, home / "skills")]
    if where == "external":
        md = _skill_md(tmp_path / "ext", "c", "sk")
        roots.append((3, tmp_path / "ext"))
    elif where == "symlink":
        real = _skill_md(tmp_path / "elsewhere", "sk")
        (home / "skills" / "c").mkdir()
        (home / "skills" / "c" / "sk").symlink_to(real.parent, target_is_directory=True)
        md = home / "skills" / "c" / "sk" / "SKILL.md"
    elif where == "leaf-symlink":
        real = _skill_md(tmp_path / "elsewhere", "sk")
        (home / "skills" / "c" / "sk").mkdir(parents=True)
        md = home / "skills" / "c" / "sk" / "SKILL.md"
        md.symlink_to(real)
    elif where == "venv":
        md = _skill_md(home / "skills", ".venv", "pkg", "sk")
    elif where == "dist":
        md = _skill_md(home / "skills", ".hermes-dist", "new", "sk")
    else:
        _skill_md(home / "skills", "parent", body="# parent\n")
        md = _skill_md(home / "skills", "parent", "references", "sk")
    _accepted(home, shown_diff=DIFF, current_diff=DIFF)
    called: list[str] = []
    _hermes(monkeypatch, home, md, called, roots=roots)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused"
    assert "cannot make skill_manage write" in r["message"]
    assert called == []


@pytest.mark.parametrize("copy", [(".archive", "sk"), ("parent", "references", "sk")])
def test_copies_hermes_does_not_scan_do_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, copy: tuple[str, ...]
) -> None:
    home = tmp_path / "home"
    md = _skill_md(home / "skills", "sk")
    _skill_md(home / "skills", "parent", body="# parent\n")
    _skill_md(home / "skills", *copy, body="# copy\n")
    _accepted(home, shown_diff=DIFF, current_diff=DIFF)
    called: list[str] = []
    _hermes(monkeypatch, home, md, called)
    r = apply.run(home, "proposal", "p1", dry_run=True)
    assert r["outcome"] == "dry-run", r["message"]


def test_a_symlink_to_a_namesake_is_followed_when_counting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count follows symlinks and does not let one visit hide another place: a differently
    named link to a deeper directory called sk still counts that directory."""
    home = tmp_path / "home"
    md = _skill_md(home / "skills", "sk")
    deep = _skill_md(home / "skills", "cat", "inner", "sk", body="# deep\n")
    (home / "skills" / "link").symlink_to(deep.parent, target_is_directory=True)
    _accepted(home, shown_diff=DIFF, current_diff=DIFF)
    called: list[str] = []
    _hermes(monkeypatch, home, md, called)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused"
    assert str(deep) in r["message"]


def test_when_hermes_cannot_list_the_skill_dirs_nothing_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _new_skill_proposal(home)
    called: list[str] = []
    _hermes(monkeypatch, home, NOT_FOUND, called)

    def broken() -> tuple[list[tuple[int, Path]], Path]:
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(hermes_internals.SKILLS_TOOL, "skill_search_dirs", broken)
    r = apply.run(home, "proposal", "p2", dry_run=True)
    assert r["outcome"] == "refused"
    assert "could not list" in r["message"]
    assert called == []


def test_a_new_skill_is_refused_when_a_directory_has_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermes says not found (e.g. a directory it skips) but skill_manage could still find it."""
    home = tmp_path / "home"
    _new_skill_proposal(home)
    _skill_md(home / "skills", "c", "sk")
    called: list[str] = []
    _hermes(monkeypatch, home, NOT_FOUND, called)
    r = apply.run(home, "proposal", "p2", dry_run=True)
    assert r["outcome"] == "refused"
    assert "exists" in r["message"]
    assert called == []


def test_a_skill_that_moved_since_the_proposal_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proposal was derived from one SKILL.md; if Hermes now resolves the name to another
    file (a frontmatter alias, a relocation), its diff is not about that file."""
    home = tmp_path / "home"
    md = _skill_md(home / "skills", "alias")
    _accepted(home, shown_diff=DIFF, current_diff=DIFF)
    conn = store.open_db(home)
    try:
        conn.execute("update proposal set file = 'sk/SKILL.md' where id = 'p1'")
        conn.commit()
    finally:
        conn.close()
    called: list[str] = []
    _hermes(monkeypatch, home, md, called)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused"
    assert "was derived from sk/SKILL.md" in r["message"]
    assert called == []


def test_an_approval_in_another_directory_of_the_name_is_not_this_proposals(
    tmp_path: Path,
) -> None:
    """A staged patch of skills/sk was rejected; an equivalent write to skills/b/sk (whose path
    also ends in sk/SKILL.md) or a/sk later is not its approval: the ledger reads all as sk."""
    skills = tmp_path / "skills"
    pr: ProposalRow = {
        "id": "p1",
        "skill_name": "sk",
        "is_new_skill": 0,
        "file": "sk/SKILL.md",
        "rule": "Check the lock first.",
        "why": None,
        "diff": DIFF,
        "confidence": None,
        "evidence": "{}",
        "derived_by": "t",
    }
    staged: ApplyAttempt = {
        "outcome": "staged",
        "message": None,
        "ledger_entry": None,
        "pending_id": "w1",
        "at": 1.0,
        "by": "dashboard",
    }
    before, after = "# sk\n\nStep one.\n", "# sk\n\nStep one.\nCheck the lock first.\n"
    target = os.path.realpath(skills / "sk" / "SKILL.md")
    nested = ("e1", "patch", 2.0, str(skills / "b" / "sk" / "SKILL.md"), before, after)
    other = ("e2", "patch", 2.5, str(skills / "a" / "sk" / "SKILL.md"), before, after)
    mine = ("e3", "patch", 3.0, str(skills / "sk" / "SKILL.md"), before, after)
    assert export._approved(pr, staged, [nested, other], target) is None
    got = export._approved(pr, staged, [nested, other, mine], target)
    assert got is not None
    assert got["ledger_entry"] == "e3"
