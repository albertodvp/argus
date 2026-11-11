"""Apply and skill discovery against a real Hermes, in throw-away Hermes homes.

Not collected by pytest (the dev env has no Hermes). Run it with the interpreter of a Hermes
install, from the repository root:

    just integration /path/to/hermes-env/bin/python3

(for a source checkout of Hermes, put the checkout on PYTHONPATH).

Each case runs in its own process with HERMES_HOME set to a fresh temporary profile, because
Hermes reads its home when its modules are imported. The cases write only under that temporary
directory; the script is not a sandbox (it inherits the environment, and Hermes may prepare its
runtime for a new data root). Exit status 0 only when every case holds.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
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
    from argus import store

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
    from tools import skill_ledger

    for e in skill_ledger.list_entries(limit=50):
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
    from tools import skill_ledger

    out: list[set[str]] = []
    for e in skill_ledger.list_entries(limit=50):
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
    from argus import apply

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
    assert r["outcome"] == "refused" and "already in" in r["message"], r


def case_patch_writes_the_checked_file(tmp: Path) -> None:
    """A unique skill in a category directory, with a decoy of another name next to it."""
    from argus import apply

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
    from argus import apply

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
    from argus import apply

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
    from argus import apply

    ext = tmp / "external"
    home = _profile(tmp, ext)
    md = _skill(home / "skills", "c", "sk")
    alias = _skill(ext, "alias", name="sk", body="Step   one.")
    before = _snapshot(alias)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "applied", r
    assert RULE in md.read_text(encoding="utf-8") and _snapshot(alias) == before
    assert _entry(r["ledger_entry"]) == ("user", "patch", {os.path.realpath(md)})


def case_namesake_in_another_category(tmp: Path) -> None:
    """Argus keys a skill by its directory name: c/sk next to d/sk is refused, nothing written."""
    from argus import apply

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
    from argus import apply

    home = _profile(tmp)
    md = _skill(home / "skills", "sk")
    backup = _skill(home / "skills", ".hermes-dist", "old", "sk")
    before = _snapshot(md, backup)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "refused", r
    assert _snapshot(md, backup) == before


def case_symlinked_skill_file(tmp: Path) -> None:
    from argus import apply

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
    from argus import apply

    home = _profile(tmp)
    cfg = (home / "config.yaml").read_text(encoding="utf-8")
    (home / "config.yaml").write_text(
        cfg.replace("write_approval: false", "write_approval: true"), encoding="utf-8"
    )
    md = _skill(home / "skills", "c", "sk")
    before = _snapshot(md)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "staged" and r.get("pending_id"), r
    again = apply.run(home, "proposal", "p1", force=True)
    assert again["outcome"] == "refused" and str(r.get("pending_id")) in again["message"], again
    assert _snapshot(md) == before


def case_top_level_skill_with_a_namesake(tmp: Path) -> None:
    """Directly in skills/ skill_manage matches the bare name against every directory: another
    directory called sk anywhere refuses the apply."""
    from argus import apply

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
    from argus import apply

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
    from argus import apply

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
    from argus import apply

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
    from argus import apply

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
    from argus import apply

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
    from argus import apply

    home = _profile(tmp, extra="  ledger: false\n")
    _skill(home / "skills", "cat", "sk")
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "unknown" and "no ledger entry" in r["message"], r


def case_support_copy_does_not_block(tmp: Path) -> None:
    """An example skill inside another skill's references/ is not a skill for Hermes."""
    from argus import apply

    home = _profile(tmp)
    md = _skill(home / "skills", "cat", "sk")
    _skill(home / "skills", "parent")
    example = _skill(home / "skills", "parent", "references", "sk", body="Example.")
    before = _snapshot(example)
    _propose(home, "p1", "sk", DIFF)
    r = apply.run(home, "proposal", "p1")
    assert r["outcome"] == "applied", r
    assert RULE in md.read_text(encoding="utf-8") and _snapshot(example) == before


def case_every_profile_from_one_process(tmp: Path) -> None:
    """Export and derive read all profiles from one process: each gets its own roots."""
    from argus import store

    ext = tmp / "external"
    a = _profile(tmp / "a", ext)
    b = _profile(tmp / "b")
    shared = _skill(ext, "d", "shared")
    own = _skill(b / "skills", "c", "own")
    for _ in range(2):
        for home, want in ((a, {"shared": shared}), (b, {"own": own})):
            assert store.skill_files(home) == want, (home, store.skill_files(home))


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
    )
}


def main() -> int:
    if len(sys.argv) == 3:  # child: one case, HERMES_HOME already set by the parent
        CASES[sys.argv[1]](Path(sys.argv[2]))
        return 0
    failed = 0
    for name in CASES:
        tmp = Path(tempfile.mkdtemp(prefix=f"argus-it-{name}-"))
        env = {
            # TERMINAL_CWD (set inside a Hermes terminal) would replace the case's cwd as the
            # project Hermes looks at
            **{k: v for k, v in os.environ.items() if k != "TERMINAL_CWD"},
            "HERMES_HOME": str(tmp / "home"),
            "HOME": str(tmp / "user"),
            # the repository first, then whatever made Hermes importable here (a source checkout)
            "PYTHONPATH": os.pathsep.join(
                [str(REPO), *filter(None, [os.environ.get("PYTHONPATH")])]
            ),
        }
        if name == "every_profile_from_one_process":
            env["HERMES_HOME"] = str(tmp / "a" / "home")
        (tmp / "user").mkdir()
        p = subprocess.run(
            [sys.executable, __file__, name, str(tmp)],
            env=env,
            cwd=tmp,
            capture_output=True,
            text=True,
            timeout=300,
        )
        ok = p.returncode == 0
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}", flush=True)
        if not ok:
            print((p.stdout + p.stderr)[-3000:], flush=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{len(CASES) - failed}/{len(CASES)} cases passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
