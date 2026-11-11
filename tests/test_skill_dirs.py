"""Argus finds a profile's skills where Hermes does: `skills/`, then `skills.create_dir` and
`skills.external_dirs`, as Hermes' published `tools.skills_tool.skill_search_dirs` lists them for
that profile. A skill kept in an external directory is a skill of the profile: proposals can name
it, and it is not taken for one removed outside Hermes. Argus never reads config.yaml itself."""

import sys
import types
from contextvars import ContextVar, Token
from pathlib import Path

import pytest

from argus import store

PROJECT, LOCAL, CREATE_DIR, EXTERNAL = range(4)  # Hermes' agent.skill_utils tiers


def _skill(root: Path, category: str, name: str) -> Path:
    p = root / category / name / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text(f"# {name}\n", encoding="utf-8")
    return p


@pytest.fixture()
def hermes(monkeypatch: pytest.MonkeyPatch) -> dict[Path, list[tuple[int, Path]]]:
    """Hermes' public skill roots: `skill_search_dirs()` answers for the Hermes home in force
    (`hermes_constants.set_hermes_home_override`), from `roots[home]`; the process's own home is
    `/nowhere`, so a lookup that forgets the override finds nothing."""
    roots: dict[Path, list[tuple[int, Path]]] = {}
    override: ContextVar[str | None] = ContextVar("override", default=None)

    def set_hermes_home_override(path: str | Path | None) -> Token[str | None]:
        return override.set(None if path is None else str(path))

    def reset_hermes_home_override(token: Token[str | None]) -> None:
        override.reset(token)

    def skill_search_dirs() -> tuple[list[tuple[int, Path]], Path]:
        home = Path(override.get() or "/nowhere")
        return roots.get(home, []), home / "skills"

    constants = types.ModuleType("hermes_constants")
    constants.set_hermes_home_override = set_hermes_home_override  # type: ignore[attr-defined]
    constants.reset_hermes_home_override = reset_hermes_home_override  # type: ignore[attr-defined]
    skills_tool = types.ModuleType("tools.skills_tool")
    skills_tool.skill_search_dirs = skill_search_dirs  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.skills_tool", skills_tool)
    return roots


def test_external_and_create_dirs_hold_skills_of_the_profile(
    tmp_path: Path, hermes: dict[Path, list[tuple[int, Path]]]
) -> None:
    home, ext = tmp_path / "home", tmp_path / "shared-skills"
    own = _skill(home / "skills", "general", "hermes-agent")
    shadowed = _skill(ext, "domain", "hermes-agent")  # the local copy wins, as in Hermes
    domain = _skill(ext, "domain", "import-rules")
    made = _skill(home / "made", "x", "new-one")
    hermes[home] = [(LOCAL, home / "skills"), (CREATE_DIR, home / "made"), (EXTERNAL, ext)]

    files = store.skill_files(home)
    assert files == {"hermes-agent": own, "import-rules": domain, "new-one": made}
    assert shadowed not in files.values()
    assert store.skill_rel(home, domain) == "domain/import-rules/SKILL.md"

    conn = store.open_db(home)
    try:
        conn.execute(
            "insert into proposal (id, skill_name, rule, evidence, derived_by)"
            " values ('p1', 'import-rules', 'r', '{}', 't')"
        )
        conn.commit()
        assert store.pending_proposals(conn, home, ("accept", "dismiss")) == 1
    finally:
        conn.close()


def test_each_profile_gets_its_own_dirs_in_one_process(
    tmp_path: Path, hermes: dict[Path, list[tuple[int, Path]]]
) -> None:
    """Export and derive read every profile from one process: the roots come from the profile
    asked for, not from the profile the process runs as."""
    a, b, ext = tmp_path / "a", tmp_path / "b", tmp_path / "ext"
    shared = _skill(ext, "d", "shared-one")
    own_b = _skill(b / "skills", "c", "own")
    hermes[a] = [(LOCAL, a / "skills"), (EXTERNAL, ext)]
    hermes[b] = [(LOCAL, b / "skills")]
    assert store.skill_files(a) == {"shared-one": shared}
    assert store.skill_files(b) == {"own": own_b}


def test_trusted_project_dirs_are_not_skills_of_the_profile(
    tmp_path: Path, hermes: dict[Path, list[tuple[int, Path]]]
) -> None:
    """Project skill dirs follow the cwd of whoever asks, and skill_manage never writes there."""
    home, repo = tmp_path / "home", tmp_path / "repo" / ".hermes" / "skills"
    own = _skill(home / "skills", "c", "s")
    _skill(repo, "p", "from-the-repo")
    hermes[home] = [(PROJECT, repo), (LOCAL, home / "skills")]
    assert store.skill_search_roots(home) == [(LOCAL, home / "skills")]
    assert store.skill_files(home) == {"s": own}


def test_without_the_profile_skills_dir_project_dirs_cannot_be_told_apart(
    tmp_path: Path, hermes: dict[Path, list[tuple[int, Path]]]
) -> None:
    """Hermes drops a missing `skills/` from its roots, and only `skills/` marks where the project
    dirs before it end: Argus then cannot list the profile's roots (apply refuses on None) and
    readers keep to `skills/`, never to a project dir."""
    home, repo, ext = tmp_path / "home", tmp_path / "repo" / ".hermes" / "skills", tmp_path / "ext"
    _skill(repo, "p", "from-the-repo")
    _skill(ext, "d", "shared")
    hermes[home] = [(PROJECT, repo), (EXTERNAL, ext)]
    assert store.hermes_skill_roots(home) is None
    assert store.skill_files(home) == {}
    hermes[home] = []
    assert store.hermes_skill_roots(home) == []


def test_a_missing_dir_listed_by_hermes_is_skipped(
    tmp_path: Path, hermes: dict[Path, list[tuple[int, Path]]]
) -> None:
    home = tmp_path / "home"
    own = _skill(home / "skills", "c", "s")
    hermes[home] = [(LOCAL, home / "skills"), (CREATE_DIR, home / "not-made-yet")]
    assert store.skill_roots(home) == [home / "skills"]
    assert store.skill_files(home) == {"s": own}


def test_without_hermes_only_the_profile_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "tools.skills_tool", None)  # import fails
    home = tmp_path / "home"
    own = _skill(home / "skills", "c", "s")
    assert store.skill_search_roots(home) == []
    assert store.skill_files(home) == {"s": own}
