"""apply rolls back a rejected skill edit only while every file is still what the edit left behind:
an edit changed since (by a newer ledger entry, outside the ledger, or by an earlier rollback) is
refused without touching anything, and so is one without a current reject decision."""

import hashlib
import time
import types
from contextvars import ContextVar, Token
from pathlib import Path

import pytest

from argus import apply, hermes_internals, store
from argus.dbtypes import APPLY_LOG_ROW, query_as

Entry = dict[str, object]


class Ledger(types.ModuleType):
    """Hermes' tools.skill_ledger, as far as apply calls it: entries newest first."""

    def __init__(self) -> None:
        super().__init__("tools.skill_ledger")
        self.entries: list[Entry] = []
        self.rolled_back: list[tuple[str, str | None]] = []  # (entry id, ledger actor)
        self.actor: ContextVar[str | None] = ContextVar("actor", default=None)

    def set_ledger_actor(self, actor: str | None) -> Token[str | None]:
        return self.actor.set(actor)

    def reset_ledger_actor(self, token: Token[str | None]) -> None:
        self.actor.reset(token)

    def list_entries(self, skill: str | None = None, limit: int | None = None) -> list[Entry]:
        return [e for e in self.entries if skill is None or e.get("skill") == skill][:limit]

    def rollback_entry(self, entry_id: str) -> tuple[bool, str]:
        self.rolled_back.append((entry_id, self.actor.get()))
        self.entries.insert(
            0,
            {
                "id": "rb",
                "ts": "2026-11-03T10:00:00+00:00",
                "action": "rollback",
                "skill": "k",
                "evidence": {"rollback_target": entry_id},
            },
        )
        return True, "restored"


@pytest.fixture()
def ledger(monkeypatch: pytest.MonkeyPatch) -> Ledger:
    fake = Ledger()
    monkeypatch.setattr(hermes_internals, "SKILL_LEDGER", fake)
    return fake


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture()
def home(tmp_path: Path, ledger: Ledger) -> Path:
    """A skill whose SKILL.md is what ledger entry e1 wrote, with a reject decision on e1."""
    h = tmp_path / "hermes"
    skill = h / "skills" / "k" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("new\n", encoding="utf-8")
    ledger.entries.append(
        {
            "id": "e1",
            "ts": "2026-11-01T10:00:00+00:00",
            "action": "patch",
            "actor": "agent",
            "skill": "k",
            "before": [{"path": str(skill), "sha256": sha("old\n")}],
            "after": [{"path": str(skill), "sha256": sha("new\n")}],
        }
    )
    decide(h, "reject")
    return h


def decide(home: Path, action: str) -> None:
    conn = store.open_db(home)
    try:
        conn.execute(
            "insert into decision (target_kind, target_id, action, target_snapshot, decided_at)"
            " values ('skill_edit', 'e1', ?, '{}', ?)",
            (action, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def logged(home: Path) -> list[str]:
    conn = store.open_db(home)
    try:
        return [r["outcome"] for r in query_as(conn, APPLY_LOG_ROW, "select * from apply_log")]
    finally:
        conn.close()


def test_rejected_edit_is_rolled_back_as_the_user(home: Path, ledger: Ledger) -> None:
    r = apply.run(home, "skill_edit", "e1")
    assert (r["outcome"], r["ledger_entry"]) == ("applied", "rb")
    assert ledger.rolled_back == [("e1", "user")]
    assert logged(home) == ["applied"]


def test_without_a_reject_decision_nothing_happens(home: Path, ledger: Ledger) -> None:
    decide(home, "keep")  # the latest decision wins
    r = apply.run(home, "skill_edit", "e1")
    assert r["outcome"] == "refused"
    assert "no current" in r["message"]
    assert ledger.rolled_back == []


def test_dry_run_changes_and_logs_nothing(home: Path, ledger: Ledger) -> None:
    assert apply.run(home, "skill_edit", "e1", dry_run=True)["outcome"] == "dry-run"
    assert (ledger.rolled_back, logged(home)) == ([], [])


def test_edit_changed_by_a_newer_entry_is_refused(home: Path, ledger: Ledger) -> None:
    skill = home / "skills" / "k" / "SKILL.md"
    skill.write_text("newer\n", encoding="utf-8")
    ledger.entries.insert(
        0,
        {
            "id": "e2",
            "ts": "2026-11-02T10:00:00+00:00",
            "action": "patch",
            "actor": "curator",
            "skill": "k",
            "before": [{"path": str(skill), "sha256": sha("new\n")}],
            "after": [{"path": str(skill), "sha256": sha("newer\n")}],
        },
    )
    r = apply.run(home, "skill_edit", "e1")
    assert (r["outcome"], r.get("newer")) == ("refused", ["e2"])
    assert ledger.rolled_back == []
    assert skill.read_text(encoding="utf-8") == "newer\n"


def test_edit_changed_outside_the_ledger_is_refused(home: Path, ledger: Ledger) -> None:
    (home / "skills" / "k" / "SKILL.md").write_text("by hand\n", encoding="utf-8")
    r = apply.run(home, "skill_edit", "e1")
    assert r["outcome"] == "refused"
    assert "outside the ledger" in r["message"]
    assert ledger.rolled_back == []


def test_second_rollback_is_refused(home: Path, ledger: Ledger) -> None:
    assert apply.run(home, "skill_edit", "e1")["outcome"] == "applied"
    (home / "skills" / "k" / "SKILL.md").write_text("old\n", encoding="utf-8")  # what it restored
    ledger.entries[0].update(
        before=[{"path": str(home / "skills" / "k" / "SKILL.md"), "sha256": sha("new\n")}]
    )
    r = apply.run(home, "skill_edit", "e1")
    assert r["outcome"] == "refused"
    assert "already rolled back" in r["message"]
    assert ledger.rolled_back == [("e1", "user")]


def test_entry_outside_the_profile_home_is_refused(home: Path, ledger: Ledger) -> None:
    ledger.entries[0]["after"] = [{"path": "/elsewhere/skills/k/SKILL.md", "sha256": sha("x")}]
    r = apply.run(home, "skill_edit", "e1")
    assert r["outcome"] == "refused"
    assert "outside this profile's home" in r["message"]
    assert ledger.rolled_back == []
