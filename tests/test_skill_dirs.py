"""Argus finds a profile's skills where Hermes does: `skills/`, then `skills.create_dir` and
`skills.external_dirs` from its config.yaml. A skill kept in an external directory is a skill of
the profile: proposals can name it, and it is not taken for one removed outside Hermes."""

import sys
import types
from pathlib import Path

import pytest

from argus import store


def _skill(root: Path, category: str, name: str) -> Path:
    p = root / category / name / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text(f"# {name}\n", encoding="utf-8")
    return p


@pytest.fixture()
def config(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Hermes' public config reader, answering `cfg` for the profile's config.yaml."""
    cfg: dict[str, object] = {}
    mod = types.ModuleType("hermes_cli.config")

    def read_user_config_raw(config_path: Path | None = None) -> dict[str, object]:
        return cfg if config_path is not None and config_path.exists() else {}

    mod.read_user_config_raw = read_user_config_raw  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", mod)
    return cfg


def test_external_and_create_dirs_hold_skills_of_the_profile(
    tmp_path: Path, config: dict[str, object]
) -> None:
    home, ext = tmp_path / "home", tmp_path / "shared-skills"
    own = _skill(home / "skills", "general", "hermes-agent")
    shadowed = _skill(ext, "domain", "hermes-agent")  # the local copy wins, as in Hermes
    domain = _skill(ext, "domain", "import-rules")
    made = _skill(home / "made", "x", "new-one")
    (home / "config.yaml").write_text("skills: {}\n", encoding="utf-8")
    config["skills"] = {"create_dir": "made", "external_dirs": [str(ext)]}

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


def test_a_single_external_dir_written_as_a_scalar(
    tmp_path: Path, config: dict[str, object]
) -> None:
    home, ext = tmp_path / "home", tmp_path / "ext"
    domain = _skill(ext, "d", "only")
    (home / "skills").mkdir(parents=True)
    (home / "config.yaml").write_text("skills: {}\n", encoding="utf-8")
    config["skills"] = {"external_dirs": str(ext)}
    assert store.skill_files(home) == {"only": domain}


def test_without_hermes_only_the_profile_skills(tmp_path: Path) -> None:
    home = tmp_path / "home"
    own = _skill(home / "skills", "c", "s")
    (home / "config.yaml").write_text("skills: {}\n", encoding="utf-8")
    assert store.skill_files(home) == {"s": own}
