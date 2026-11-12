"""Opt-in auto-apply: off unless the setting is a real `true`; it applies only additive,
high-confidence proposals of the latest pass backed by 2+ intents, through the same apply as a
person, within its weekly and per-skill budget, and never again on a skill where one was rolled
back. Hermes' skill functions are faked with a file-backed ledger, as skill_manage writes it."""

import argparse
import hashlib
import json
import sqlite3
import time
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest
from test_window import make_home, quiet

from argus import apply, auto, backfill, cli, derive, hermes_internals, store
from argus.dbtypes import ProposalRow, count, query, scalar
from argus.jsontypes import JsonList, JsonObject, as_list, loads, loads_object

NOT_FOUND: Final = '{"success": false, "error": "not found.", "available_skills": []}'
BODY: Final = "# {name}\n\nStep one.\n"


def _diff(rule: str) -> str:
    return f"@@ -3 +3,2 @@\n Step one.\n+{rule}\n"


def _ledger(home: Path) -> Path:
    return home / "skills" / ".curator_ledger.jsonl"


def _append(home: Path, entry: JsonObject) -> None:
    with _ledger(home).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _entries(home: Path) -> list[JsonObject]:
    if not _ledger(home).exists():
        return []
    lines = _ledger(home).read_text(encoding="utf-8").splitlines()
    return [loads_object(x) for x in lines if x.strip()]


class FakeHermes:
    """skill lookup over `<home>/skills`, skill_manage patching for real and appending its
    ledger entry (actor user), list_entries newest first."""

    def __init__(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = home
        self.calls: list[str] = []
        self.locked: set[str] = set()
        self.ledger_off = False  # skills.ledger off: the write happens, no entry names it
        constants = types.ModuleType("hermes_constants")

        def override(path: object) -> None:
            return None

        def get_home() -> Path:
            return home

        setattr(constants, "get_hermes_home", get_home)  # noqa: B010
        setattr(constants, "set_hermes_home_override", override)  # noqa: B010
        setattr(constants, "reset_hermes_home_override", override)  # noqa: B010
        st = types.ModuleType("tools.skills_tool")
        skills = home / "skills"

        def search_dirs() -> tuple[list[tuple[int, Path]], Path]:
            return [(1, skills)], skills

        def locate(
            name: str, local: str | None, given: list[tuple[int, Path]]
        ) -> tuple[str | None, Path | None, Path | None]:
            md = skills / name / "SKILL.md"
            return (None, md.parent, md) if md.exists() else (NOT_FOUND, None, None)

        setattr(st, "skill_search_dirs", search_dirs)  # noqa: B010
        setattr(st, "locate_skill", locate)  # noqa: B010
        smt = types.ModuleType("tools.skill_manager_tool")

        def manage(
            action: str,
            name: str,
            old_string: str = "",
            new_string: str = "",
            file_path: str | None = None,
            content: str = "",
        ) -> str:
            self.calls.append(f"{action} {name}")
            if name in self.locked:
                return '{"success": false, "error": "skill is locked"}'
            md = skills / name / "SKILL.md"
            text = md.read_text(encoding="utf-8")
            md.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
            if self.ledger_off:
                return '{"success": true, "message": "patched"}'
            sha = hashlib.sha256(md.read_bytes()).hexdigest()
            n = len(_entries(home)) + 1
            _append(
                home,
                {
                    "id": f"e{n}",
                    "ts": datetime.now(UTC).isoformat(),
                    "actor": "user",
                    "action": action,
                    "skill": name,
                    "after": [{"path": str(md), "sha256": sha}],
                },
            )
            return '{"success": true, "message": "patched"}'

        setattr(smt, "skill_manage", manage)  # noqa: B010
        sl = types.ModuleType("tools.skill_ledger")

        def entries(skill: str | None = None, limit: int | None = None) -> list[JsonObject]:
            out = [e for e in reversed(_entries(home)) if skill is None or e.get("skill") == skill]
            return out[:limit] if limit else out

        def actor(a: object) -> None:
            return None

        setattr(sl, "list_entries", entries)  # noqa: B010
        setattr(sl, "set_ledger_actor", actor)  # noqa: B010
        setattr(sl, "reset_ledger_actor", actor)  # noqa: B010
        monkeypatch.setattr(hermes_internals, "CONSTANTS", constants)
        monkeypatch.setattr(hermes_internals, "SKILLS_TOOL", st)
        monkeypatch.setattr(hermes_internals, "SKILL_MANAGER", smt)
        monkeypatch.setattr(hermes_internals, "SKILL_LEDGER", sl)


def _skill(home: Path, name: str) -> Path:
    md = home / "skills" / name / "SKILL.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(BODY.format(name=name), encoding="utf-8")
    return md


def _proposal(
    home: Path,
    pid: str,
    skill: str = "sk",
    rule: str = "Check the lock first.",
    confidence: str = "high",
    intents: tuple[str, ...] = ("i1", "i2"),
    diff: str | None = None,
    new: bool = False,
    latest: bool = True,
) -> None:
    cited: JsonList = [*intents]
    evidence: JsonObject = {"intents": cited, "error_sigs": [], "overlaps": []}
    conn = store.open_db(home)
    try:
        conn.execute(
            "insert into proposal (id, skill_name, is_new_skill, file, rule, why, diff, confidence,"
            " evidence, derived_by) values (?, ?, ?, ?, ?, 'why', ?, ?, ?, 'test')",
            (
                pid,
                skill,
                int(new),
                None if new else f"{skill}/SKILL.md",
                rule,
                _diff(rule) if diff is None else diff,
                confidence,
                json.dumps(evidence),
            ),
        )
        if latest:
            ids: JsonList = [*sorted(auto.last_pass(conn) | {pid})]
            store.meta_set(conn, auto.LAST_PASS, json.dumps(ids))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    (h / "skills").mkdir(parents=True)
    return h


def _decisions(home: Path) -> list[tuple[str, str, str]]:
    conn = store.open_db(home)
    try:
        return [
            (str(r["target_id"]), str(r["action"]), str(r["decided_by"]))
            for r in query(conn, "select * from decision order by id")
        ]
    finally:
        conn.close()


def _attempts(home: Path) -> list[tuple[str, str, str]]:
    conn = store.open_db(home)
    try:
        return [
            (str(r["target_id"]), str(r["outcome"]), str(r["applied_by"]))
            for r in query(conn, "select * from apply_log order by id")
        ]
    finally:
        conn.close()


def test_the_setting_is_off_unless_a_real_true() -> None:
    assert not auto.setting_on(None)
    assert not auto.setting_on("true")
    assert not auto.setting_on(1)
    assert auto.setting_on(True)
    assert auto.setting_per_week(None) == auto.PER_WEEK
    assert auto.setting_per_week(True) == auto.PER_WEEK
    assert auto.setting_per_week(-1) == auto.PER_WEEK
    assert auto.setting_per_week(0) == 0


def test_only_additive_one_hunk_diffs_qualify() -> None:
    assert auto.additive("@@ -1 +1,2 @@\n ctx\n+new\n")
    assert not auto.additive("@@ -1,2 +1,2 @@\n ctx\n-old\n+new\n")
    assert not auto.additive("@@ -1 +1,2 @@\n ctx\n+a\n@@ -9 +10,2 @@\n ctx\n+b\n")
    assert not auto.additive(None)


def test_applies_a_qualifying_proposal_as_auto(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    md = _skill(home, "sk")
    hermes = FakeHermes(home, monkeypatch)
    _proposal(home, "p1")
    [(_p, r)] = auto.run(home, per_week=3)
    assert r["outcome"] == "applied", r["message"]
    assert r["ledger_entry"] == "e1"
    assert "Check the lock first." in md.read_text(encoding="utf-8")
    assert hermes.calls == ["patch sk"]
    assert _decisions(home) == [("p1", "accept", "auto")]
    assert _attempts(home) == [("p1", "applied", "auto")]
    assert auto.run(home, per_week=3) == []  # decided and applied: never again


def test_a_dry_run_decides_and_writes_nothing(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    md = _skill(home, "sk")
    hermes = FakeHermes(home, monkeypatch)
    _proposal(home, "p1")
    [(_p, r)] = auto.run(home, per_week=3, dry_run=True)
    assert r["outcome"] == "dry-run"
    assert md.read_text(encoding="utf-8") == BODY.format(name="sk")
    assert hermes.calls == []
    assert _decisions(home) == []
    assert _attempts(home) == []


@pytest.mark.parametrize(
    "kw",
    [
        {"confidence": "medium"},
        {"intents": ("i1",)},
        {"intents": ("i1", "i1")},
        {"diff": "@@ -3 +3 @@\n-Step one.\n+Check the lock first.\n"},
        {"new": True, "skill": "fresh"},
        {"latest": False},
    ],
)
def test_proposals_outside_the_policy_are_left_to_the_person(
    home: Path, monkeypatch: pytest.MonkeyPatch, kw: dict[str, object]
) -> None:
    _skill(home, "sk")
    hermes = FakeHermes(home, monkeypatch)
    _proposal(home, "p0", skill="other", latest=True, confidence="low")  # a latest pass exists
    _proposal(home, "p1", **kw)  # type: ignore[arg-type]
    assert auto.run(home, per_week=3) == []
    assert hermes.calls == []
    assert _decisions(home) == []


def test_a_proposal_someone_decided_or_tried_is_not_touched(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skill(home, "sk")
    _skill(home, "sk2")
    hermes = FakeHermes(home, monkeypatch)
    _proposal(home, "p1")
    _proposal(home, "p2", skill="sk2")
    conn = store.open_db(home)
    try:
        conn.execute(
            "insert into decision (target_kind, target_id, action, target_snapshot, decided_at,"
            " decided_by) values ('proposal', 'p1', 'dismiss', '{}', ?, 'dashboard')",
            (time.time(),),
        )
        conn.execute(
            "insert into apply_log (target_kind, target_id, outcome, message, applied_at,"
            " applied_by) values ('proposal', 'p2', 'refused', 'm', ?, 'cli --force')",
            (time.time(),),
        )
        conn.commit()
    finally:
        conn.close()
    assert auto.run(home, per_week=3) == []
    assert hermes.calls == []


def test_the_weekly_budget(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for s in ("a", "b", "c"):
        _skill(home, s)
    FakeHermes(home, monkeypatch)
    _proposal(home, "pa", skill="a", rule="Rule a.", intents=("i1", "i2"))
    _proposal(home, "pb", skill="b", rule="Rule b.", intents=("i3", "i4", "i5"))
    _proposal(home, "pc", skill="c", rule="Rule c.", intents=("i6", "i7"))
    done = auto.run(home, per_week=2)
    assert [p["id"] for p, _r in done] == ["pb", "pa"]  # most intents first, then by id
    assert auto.run(home, per_week=2) == []  # the 7 days' budget is spent
    assert [p["id"] for p, _r in auto.run(home, per_week=3)] == ["pc"]


def test_a_failed_apply_returns_the_proposal_to_the_person(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skill(home, "sk")
    hermes = FakeHermes(home, monkeypatch)
    hermes.locked.add("sk")
    _proposal(home, "p1")
    [(_p, r)] = auto.run(home, per_week=3)
    assert r["outcome"] == "refused"
    assert "locked" in r["message"]
    assert _decisions(home) == []  # its own decision removed: the person decides
    assert _attempts(home) == [("p1", "refused", "auto")]
    hermes.locked.clear()
    assert auto.run(home, per_week=3) == []  # one try, not one per derive pass
    conn = store.open_db(home)
    try:
        assert auto.used(conn, time.time()) == 0  # a refusal spends no budget
    finally:
        conn.close()


def test_a_rollback_stops_auto_apply_on_that_skill(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skill(home, "sk")
    FakeHermes(home, monkeypatch)
    _proposal(home, "p1")
    [(_p, r)] = auto.run(home, per_week=3)
    assert r["outcome"] == "applied"
    _append(
        home,
        {
            "id": "rb1",
            "ts": datetime.now(UTC).isoformat(),
            "actor": "user",
            "action": "rollback",
            "skill": "sk",
            "evidence": {"rollback_target": r["ledger_entry"]},
        },
    )
    conn = store.open_db(home)
    try:
        assert auto.stopped_skills(conn, home) == {"sk"}
        # a week later, outside the budget's window, a new lesson for the skill still waits
        conn.execute("update apply_log set applied_at = applied_at - 8 * 86400")
        conn.commit()
    finally:
        conn.close()
    _proposal(home, "p2", rule="Another rule.", intents=("i8", "i9"))  # a different lesson
    assert auto.run(home, per_week=3) == []


def test_a_lesson_already_answered_is_not_applied_reworded(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proposals pass rewords one lesson from the same intents: once the person dismissed (or
    anyone applied) a proposal for the skill, another citing any of its intents waits for them."""
    _skill(home, "sk")
    FakeHermes(home, monkeypatch)
    _proposal(home, "p0", rule="Never retry web_extract.", intents=("i1", "i2"), latest=False)
    conn = store.open_db(home)
    try:
        conn.execute(
            "insert into decision (target_kind, target_id, action, target_snapshot, decided_at,"
            " decided_by) values ('proposal', 'p0', 'dismiss', '{}', ?, 'dashboard')",
            (time.time(),),
        )
        conn.commit()
    finally:
        conn.close()
    _proposal(home, "p1", rule="Do not retry web_extract.", intents=("i2", "i3"))
    _proposal(home, "p2", rule="Say what you checked.", intents=("i4", "i5"))
    assert [p["id"] for p, _r in auto.run(home, per_week=3)] == ["p2"]


def test_a_stale_diff_is_skipped_without_a_trace(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    md = _skill(home, "sk")
    md.write_text("# sk\n\nStep two.\n", encoding="utf-8")  # changed since the proposal
    hermes = FakeHermes(home, monkeypatch)
    _proposal(home, "p1")
    assert auto.run(home, per_week=3) == []
    assert hermes.calls == []
    assert _attempts(home) == []
    assert _decisions(home) == []


class Ctx:
    """The plugin context as cli reads settings from it."""

    def __init__(self, settings: dict[str, object]) -> None:
        self.settings = settings

    def get_config(self, key: str, default: object = None) -> object:
        return self.settings.get(key, default)


def test_cli_policy_reads_the_profile_settings() -> None:
    assert cli.auto_policy(Ctx({})) == (False, 3)  # type: ignore[arg-type]
    assert cli.auto_policy(Ctx({"auto_apply": True, "auto_apply_per_week": 1})) == (  # type: ignore[arg-type]
        True,
        1,
    )


class ProposeLlm:
    """Answers only the proposals and patch stages."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
        if purpose == "argus.assign":
            return {"turns": [{"turn": "T1", "intent": "NEW", "title": "plan the garden"}]}
        if purpose == "argus.judge":
            return {"status": "partial", "friction": "the user corrected the plan twice"}
        if purpose == "argus.proposals":
            self.prompts.append(text)
            return {
                "proposals": [
                    {
                        "skill": "sk",
                        "rule": "Say what you checked.",
                        "why": "w",
                        "intents": [],
                        "patterns": [],
                        "overlaps": ["e9", "argus-staged", "no-such-entry"],
                        "confidence": "high",
                    }
                ]
            }
        if purpose == "argus.patch":
            return {"after_line": 3, "lines": ["Say what you checked."]}
        return None


def test_the_proposals_pass_records_its_ids_and_lists_applied_rules(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """auto-apply reads only the latest pass; that pass sees the rules Argus already wrote."""
    h = home.parent / "hermes"
    make_home(h, time.time())
    _skill(h, "sk")
    backfill.run(h, since_days=0, log=quiet)
    conn = store.open_db(h)
    try:
        conn.execute(
            "insert into proposal (id, skill_name, rule, evidence, derived_by) values"
            " ('old', 'sk', 'Check the lock first.', '{}', 'test')"
        )
        conn.execute(
            "insert into apply_log (target_kind, target_id, outcome, message, ledger_entry,"
            " applied_at, applied_by) values ('proposal', 'old', 'applied', 'm', 'e9', ?, 'auto')",
            (time.time(),),
        )
        conn.execute(
            "insert into proposal (id, skill_name, rule, evidence, derived_by) values"
            " ('staged', 'sk', 'Read the error first.', '{}', 'test')"
        )
        conn.execute(
            "insert into apply_log (target_kind, target_id, outcome, message, pending_id,"
            " applied_at, applied_by) values ('proposal', 'staged', 'staged', 'm', 'w1', ?,"
            " 'auto')",
            (time.time(),),
        )
        conn.commit()
    finally:
        conn.close()
    llm = ProposeLlm()
    px_report = derive.run(h, llm, stages=["assign", "judge", "proposals"], log=quiet)
    assert "error" not in px_report["profiles"]["default"]["stages"]["proposals"], px_report
    conn = store.open_db(h)
    try:
        ids = as_list(loads(store.meta_get(conn, auto.LAST_PASS) or "null"))
        assert ids is not None
        assert len(ids) == 1
        assert ids[0] not in {"old", "staged"}
        assert count(conn, "select count(*) from proposal") == 3
        ev = loads_object(
            str(scalar(conn, "select evidence from proposal where id = ?", (str(ids[0]),)))
        )
        # the applied rule and the staged one (no ledger entry yet) can be cited as overlaps
        assert ev["overlaps"] == ["e9", "argus-staged"]
    finally:
        conn.close()
    assert len(llm.prompts) == 1
    assert "e9: sk: Check the lock first." in llm.prompts[0]
    assert "argus-staged: sk: Read the error first." in llm.prompts[0]


def test_a_dismiss_after_the_policy_read_wins(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A person dismisses between the policy read and the automatic decision: nothing is written."""
    md = _skill(home, "sk")
    hermes = FakeHermes(home, monkeypatch)
    _proposal(home, "p1")
    read = auto.candidates

    def then_dismissed(conn: sqlite3.Connection, h: Path) -> list[ProposalRow]:
        rows = read(conn, h)
        if _decisions(h):
            return rows
        c = store.open_db(h)
        try:
            c.execute(
                "insert into decision (target_kind, target_id, action, target_snapshot,"
                " decided_at, decided_by) values ('proposal', 'p1', 'dismiss', '{}', ?,"
                " 'dashboard')",
                (time.time(),),
            )
            c.commit()
        finally:
            c.close()
        return rows

    monkeypatch.setattr(auto, "candidates", then_dismissed)
    assert auto.run(home, per_week=3) == []
    assert hermes.calls == []
    assert md.read_text(encoding="utf-8") == BODY.format(name="sk")
    assert _decisions(home) == [("p1", "dismiss", "dashboard")]


def test_a_write_without_a_ledger_entry_spends_the_budget_and_stops(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """skills.ledger off: skill_manage writes but nothing proves which file. The outcome is unknown,
    it counts against the budget, keeps its decision, and the pass ends."""
    _skill(home, "a")
    _skill(home, "b")
    hermes = FakeHermes(home, monkeypatch)
    hermes.ledger_off = True
    _proposal(home, "pa", skill="a", rule="Rule a.")
    _proposal(home, "pb", skill="b", rule="Rule b.")
    [(_p, r)] = auto.run(home, per_week=3)
    assert r["outcome"] == "unknown"
    assert hermes.calls == ["patch a"]
    assert _decisions(home) == [("pa", "accept", "auto")]
    conn = store.open_db(home)
    try:
        assert auto.used(conn, time.time()) == 1
    finally:
        conn.close()
    assert [p["id"] for p, _r in auto.run(home, per_week=1)] == []  # budget spent


def test_a_staged_write_approved_then_rolled_back_stops_the_skill(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With skills.write_approval, apply_log keeps `staged` without a ledger entry; the approval
    writes the entry later. Its rollback still stops auto-apply on the skill."""
    _skill(home, "sk")
    FakeHermes(home, monkeypatch)
    _proposal(home, "p1")
    staged_at = time.time() - 60
    conn = store.open_db(home)
    try:
        conn.execute(
            "insert into apply_log (target_kind, target_id, outcome, message, pending_id,"
            " applied_at, applied_by) values ('proposal', 'p1', 'staged', 'm', 'w1', ?, 'auto')",
            (staged_at,),
        )
        conn.commit()
    finally:
        conn.close()
    now = datetime.now(UTC).isoformat()
    _append(home, {"id": "e1", "ts": now, "actor": "user", "action": "patch", "skill": "sk"})
    conn = store.open_db(home)
    try:
        assert auto.stopped_skills(conn, home) == set()
    finally:
        conn.close()
    _append(
        home,
        {
            "id": "rb1",
            "ts": now,
            "actor": "user",
            "action": "rollback",
            "skill": "sk",
            "evidence": {"rollback_target": "e1"},
        },
    )
    conn = store.open_db(home)
    try:
        assert auto.stopped_skills(conn, home) == {"sk"}
    finally:
        conn.close()


REPORTS: Final[list[tuple[derive.ProfileReport, int]]] = [
    ({"home": "h", "stages": {"proposals": {}}}, 1),
    ({"home": "h", "stages": {}, "error": "state.db changed"}, 0),
    ({"home": "h", "stages": {"proposals": {"error": "boom"}}}, 0),
    ({"home": "h", "stages": {}, "skipped": "never backfilled"}, 0),
]


@pytest.mark.parametrize(("rep", "expect"), REPORTS)
def test_derive_auto_applies_only_after_a_clean_pass_of_its_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rep: derive.ProfileReport, expect: int
) -> None:
    h = tmp_path / "hermes"
    make_home(h, time.time())
    ran: list[Path] = []

    def fake_auto(home: Path, per_week: int, dry_run: bool, quiet: bool = False) -> int:
        ran.append(home)
        return 0

    def fake_run(*a: object, **k: object) -> derive.RunReport:
        return {"started_at": "", "stages": [], "profiles": {"default": rep}}

    monkeypatch.setattr(cli, "run_auto", fake_auto)
    monkeypatch.setattr(derive, "run", fake_run)
    code = cli.run_derive(Ctx({"auto_apply": True}), h, ["default"], None, True, h)  # type: ignore[arg-type]
    assert code == 0
    assert len(ran) == expect


def test_the_cli_command_only_previews(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`hermes argus auto-apply` shows what the policy would apply, even with the setting on, and
    writes nothing: writes happen only at the end of a derive pass, under its lock."""
    md = _skill(home, "sk")
    hermes = FakeHermes(home, monkeypatch)
    _proposal(home, "p1")
    handler = cli.make_handler(Ctx({"auto_apply": True}))  # type: ignore[arg-type]
    assert handler(argparse.Namespace(argus_cmd="auto-apply")) == 0
    out = capsys.readouterr().out
    assert "on, 0 of 3 used" in out
    assert "proposal p1 on sk: dry-run" in out
    assert hermes.calls == []
    assert md.read_text(encoding="utf-8") == BODY.format(name="sk")
    assert _decisions(home) == []
    assert _attempts(home) == []


def test_one_lesson_is_applied_once_per_pass(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two wordings of one lesson (same skill, same intents) in one pass: the first answers it."""
    md = _skill(home, "sk")
    FakeHermes(home, monkeypatch)
    _proposal(home, "p1", rule="Check the lock first.")
    _proposal(home, "p2", rule="Always check the lock before anything else.")
    assert [p["id"] for p, _r in auto.run(home, per_week=3)] == ["p1"]
    assert "Always check" not in md.read_text(encoding="utf-8")
    assert [p["id"] for p, _r in auto.run(home, per_week=3, dry_run=True)] == []


def test_the_preview_shows_one_wording_of_a_lesson(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skill(home, "sk")
    FakeHermes(home, monkeypatch)
    _proposal(home, "p1", rule="Check the lock first.")
    _proposal(home, "p2", rule="Always check the lock before anything else.")
    assert [p["id"] for p, _r in auto.run(home, per_week=3, dry_run=True)] == ["p1"]


def test_a_dismiss_during_the_pass_answers_the_lesson(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The person dismisses p1 after the pass picked it: p1 is not applied, and neither is p2,
    another wording of the same lesson."""
    md = _skill(home, "sk")
    hermes = FakeHermes(home, monkeypatch)
    _proposal(home, "p1", rule="Check the lock first.", intents=("i1", "i2", "i3"))
    _proposal(home, "p2", rule="Always check the lock before anything else.")
    dry = apply.proposal

    def dismissed_meanwhile(
        h: Path, pid: str, dry_run: bool, deadline: float | None = None
    ) -> apply.ApplyResult:
        r = dry(h, pid, dry_run, deadline)
        if pid == "p1":
            c = store.open_db(h)
            try:
                c.execute(
                    "insert into decision (target_kind, target_id, action, target_snapshot,"
                    " decided_at, decided_by) values ('proposal', 'p1', 'dismiss', '{}', ?,"
                    " 'dashboard')",
                    (time.time(),),
                )
                c.commit()
            finally:
                c.close()
        return r

    monkeypatch.setattr(apply, "proposal", dismissed_meanwhile)
    assert auto.run(home, per_week=3) == []
    assert hermes.calls == []
    assert md.read_text(encoding="utf-8") == BODY.format(name="sk")


def test_a_rollback_during_the_pass_stops_the_skill(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The person rolls p1 back while the pass goes on: p2, a different lesson for the same
    skill, is left to them."""
    md = _skill(home, "sk")
    FakeHermes(home, monkeypatch)
    _proposal(home, "p1", rule="Check the lock first.", intents=("i1", "i2", "i3"))
    _proposal(home, "p2", rule="Say what you checked.", intents=("i8", "i9"))
    real = apply.run

    def then_rolled_back(h: Path, kind: str, pid: str, applied_by: str) -> apply.ApplyResult:
        r = real(h, kind, pid, applied_by=applied_by)
        md.write_text(BODY.format(name="sk"), encoding="utf-8")
        _append(
            h,
            {
                "id": f"rb-{pid}",
                "ts": datetime.now(UTC).isoformat(),
                "actor": "user",
                "action": "rollback",
                "skill": "sk",
                "evidence": {"rollback_target": r["ledger_entry"]},
            },
        )
        return r

    monkeypatch.setattr(apply, "run", then_rolled_back)
    assert [p["id"] for p, _r in auto.run(home, per_week=3)] == ["p1"]
    assert "Say what you checked." not in md.read_text(encoding="utf-8")
