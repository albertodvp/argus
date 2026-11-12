"""The per-profile history window (meta `window_start`): backfill sets and widens it, never narrows
it, and derive / export read state.db only from its start on."""

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from argus import apply, backfill, cli, derive, export, hermes_data, store
from argus.dbtypes import count
from argus.jsontypes import JsonObject

DAY = 86400.0
# one session per request, oldest first; distinct texts so no deterministic gate (repeat,
# follow-up) assigns a turn without the model
REQUESTS = (
    (10.0, "please plan the garden irrigation schedule for the coming spring season"),
    (5.0, "draft an email to the landlord about the broken heater in the kitchen"),
    (1.0, "compare three laptops for video editing that fit a modest budget"),
)


def make_home(home: Path, now: float) -> None:
    """A state.db with the sessions/messages columns hermes_data reads: one session per request,
    each a user turn, a tool call, its result and a reply."""
    home.mkdir(parents=True)
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.executescript(
            "create table sessions (id text primary key, source text not null, title text,"
            " parent_session_id text, started_at real not null, ended_at real, end_reason text,"
            " message_count integer, tool_call_count integer, profile_name text, model text);"
            "create table messages (id integer primary key autoincrement,"
            " session_id text not null, role text not null, content text, tool_call_id text,"
            " tool_calls text, tool_name text, timestamp real not null, finish_reason text);"
        )
        for n, (age, text) in enumerate(REQUESTS):
            sid, t = f"s{n}", now - age * DAY
            conn.execute(
                "insert into sessions values (?, 'cli', ?, null, ?, ?, null, 4, 1, null, null)",
                (sid, f"session {n}", t, t + 60),
            )
            call = f'[{{"id": "c{n}", "type": "function", "function": {{"name": "x"}}}}]'
            q = (
                "insert into messages (session_id, role, content, tool_call_id, tool_calls,"
                " tool_name, timestamp, finish_reason) values (?, ?, ?, ?, ?, ?, ?, ?)"
            )
            conn.execute(q, (sid, "user", text, None, None, None, t + 1, None))
            conn.execute(q, (sid, "assistant", None, None, call, None, t + 2, "tool_calls"))
            conn.execute(q, (sid, "tool", "done", f"c{n}", None, "x", t + 3, None))
            conn.execute(q, (sid, "assistant", f"answer {n}", None, None, None, t + 4, "stop"))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "hermes"
    make_home(h, time.time())
    return h


def quiet(_line: str) -> None:
    return None


def tool_outcomes(home: Path) -> int:
    conn = store.open_db(home)
    try:
        return count(conn, "select count(*) from tool_outcome")
    finally:
        conn.close()


def test_window_request_semantics() -> None:
    now = 1_000_000.0
    assert backfill.window(None, None, now) == (None, False, None)
    assert backfill.window(None, 3, now) == (now - 3 * DAY, True, None)
    assert backfill.window(None, 0, now) == (0.0, True, None)
    have = now - 3 * DAY
    assert backfill.window(have, None, now) == (have, False, None)
    assert backfill.window(have, 7, now) == (now - 7 * DAY, True, None)
    assert backfill.window(have, 0, now) == (0.0, True, None)
    kept, reread, note = backfill.window(have, 1, now)
    assert (kept, reread) == (have, False)
    assert note is not None
    assert "would narrow" in note
    assert backfill.window(0.0, 3, now) == (
        0.0,
        False,
        "whole history kept (--since 3 would narrow it)",
    )
    with pytest.raises(ValueError, match="positive number of days"):
        backfill.window(have, -1, now)
    # the since_days setting: only a profile's first backfill, never a note
    assert backfill.window(None, 30, now, explicit=False) == (now - 30 * DAY, True, None)
    assert backfill.window(None, 30, now, explicit=False, fresh=False) == (None, False, None)
    assert backfill.window(have, 30, now, explicit=False, fresh=False) == (have, False, None)
    assert backfill.window(have, 1, now, explicit=False, fresh=False) == (have, False, None)


def test_backfill_sets_widens_and_never_narrows(home: Path) -> None:
    logs: list[str] = []
    backfill.run(home, since_days=3, log=logs.append)
    first = store.read_window(home)
    assert first is not None
    assert abs(first - (time.time() - 3 * DAY)) < 60
    assert tool_outcomes(home) == 1

    # more history: the window widens and the older tool results are read despite the cursor
    rep = backfill.run(home, since_days=7, log=logs.append)
    second = store.read_window(home)
    assert second is not None
    assert second < first
    done = rep["default"]
    assert "error" not in done
    assert done.get("window_start") == second
    assert tool_outcomes(home) == 2

    # less history is reported, not applied; no request (or the setting) keeps the window quietly
    logs.clear()
    backfill.run(home, since_days=2, log=logs.append)
    assert store.read_window(home) == second
    assert any("would narrow" in line for line in logs)
    logs.clear()
    backfill.run(home, log=logs.append, default_days=1)
    backfill.run(home, log=logs.append, default_days=30)
    assert store.read_window(home) == second
    assert not any("would narrow" in line for line in logs)

    # --since 0: the whole history
    backfill.run(home, since_days=0, log=logs.append)
    assert store.read_window(home) == 0.0
    assert tool_outcomes(home) == 3


def test_setting_bounds_only_the_first_backfill(home: Path) -> None:
    backfill.run(home, log=quiet, default_days=3)
    assert store.read_window(home) is not None
    assert tool_outcomes(home) == 1


def test_no_request_sets_no_window(home: Path) -> None:
    backfill.run(home, log=quiet)
    assert store.read_window(home) is None
    assert tool_outcomes(home) == 3
    assert "history from" not in cli.status(home, home)
    # backfilled before without a window: the setting does not narrow it, an explicit --since
    # sets one
    backfill.run(home, log=quiet, default_days=3)
    assert store.read_window(home) is None
    backfill.run(home, since_days=3, log=quiet)
    assert store.read_window(home) is not None


def test_load_is_bounded_by_the_window(home: Path) -> None:
    assert len(hermes_data.load_dump(home)["sessions"]) == 3
    backfill.run(home, since_days=3, log=quiet)
    dump = hermes_data.load_dump(home, since=store.read_window(home))
    assert [s["id"] for s in dump["sessions"]] == ["s2"]
    assert {m["session_id"] for m in dump["messages"]} == {"s2"}
    assert "history from" in cli.status(home, home)


def test_window_is_by_activity(home: Path) -> None:
    """A session started before the window with new messages in it is loaded, from the window's
    start on."""
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute(
            "insert into messages (session_id, role, content, timestamp) values (?, ?, ?, ?)",
            (
                "s0",
                "user",
                "resume the irrigation plan with a drip line for the roses",
                time.time() - 0.5 * DAY,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    backfill.run(home, since_days=3, log=quiet)
    dump = hermes_data.load_dump(home, since=store.read_window(home))
    assert [s["id"] for s in dump["sessions"]] == ["s0", "s2"]
    assert [m["id"] for m in dump["messages"] if m["session_id"] == "s0"] == [13]
    turns, _sessions, _children = hermes_data.build_turns([dump])
    assert [t["sid"] for t in turns] == ["s2", "s0"]


def test_export_reads_only_the_window(home: Path) -> None:
    backfill.run(home, since_days=7, log=quiet)
    conn = store.open_db(home)
    try:
        derive.new_intent(conn, {}, "i1", "chores", None, "task", None, time.time(), "test")
        for n in range(len(REQUESTS)):
            conn.execute(
                "insert into turn_intent (session_id, user_message_id, intent_id, gate,"
                " derived_by) values (?, ?, 'i1', 'llm', 'test')",
                (f"s{n}", n * 4 + 1),
            )
        conn.commit()
    finally:
        conn.close()
    harness = export.build(home)["harnesses"]["hermes"]
    assert sorted(harness["sessions"]) == ["s1", "s2"]
    assert [len(i["turns"]) for i in harness["intents"]] == [2]


def test_counts_follow_the_window(home: Path) -> None:
    now = time.time()
    conn = store.open_db(home)
    try:
        for n, age in enumerate((10.0, 1.0)):
            derive.new_intent(conn, {}, f"i{n}", "x", None, "task", None, now, "test")
            conn.execute(
                "insert into run (intent_id, session_id, started_at, derived_by)"
                " values (?, ?, ?, 'test')",
                (f"i{n}", f"s{n}", now - age * DAY),
            )
            conn.execute(
                "insert into skill_edit (ledger_entry_id, ts, skill_name, origin, evidence)"
                " values (?, ?, 'k', 'backfill', 'speculative')",
                (f"e{n}", now - age * DAY),
            )
        conn.commit()
        assert (store.live_intents(conn), store.speculative_edits(conn)) == (2, 2)
        store.meta_set(conn, store.WINDOW, repr(now - 3 * DAY))
        assert (store.live_intents(conn), store.speculative_edits(conn)) == (1, 1)
    finally:
        conn.close()


class FakeLlm:
    """Records the purpose of every call; answers nothing useful (no turn gets assigned)."""

    def __init__(self) -> None:
        self.purposes: list[str] = []

    def __call__(self, instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
        self.purposes.append(purpose)
        return {}


def test_derive_without_backfill_reads_nothing(home: Path) -> None:
    llm = FakeLlm()
    logs: list[str] = []
    report = derive.run(home, llm, log=logs.append)
    assert llm.purposes == []
    assert "backfill --since" in report["profiles"]["default"].get("skipped", "")
    assert any("skipped" in line for line in logs)


def test_derive_assigns_only_turns_in_the_window(home: Path) -> None:
    backfill.run(home, since_days=3, log=quiet)
    llm = FakeLlm()
    report = derive.run(home, llm, stages=["assign"], log=quiet)
    assert llm.purposes == ["argus.assign"]  # one call: the one session in the window
    assert report["profiles"]["default"].get("window_start") == store.read_window(home)

    backfill.run(home, since_days=0, log=quiet)
    llm = FakeLlm()
    derive.run(home, llm, stages=["assign"], log=quiet)
    assert llm.purposes == ["argus.assign"] * 3


def test_edit_review_leaves_edits_of_unloaded_sessions_pending(home: Path) -> None:
    """An edit inside the window whose session has no messages in it is not reviewed (it would
    read as speculative); one whose session is loaded is."""
    blobs = home / ".curator_backups" / "blobs"
    blobs.mkdir(parents=True)
    for sha, text in (("b0", "# skill\nold\n"), ("a0", "# skill\nnew\n")):
        (blobs / sha).write_text(text, encoding="utf-8")
    when = datetime.fromtimestamp(time.time() - 0.5 * DAY, UTC).isoformat()
    lines = [
        {
            "id": f"e{sid}",
            "ts": when,
            "actor": "agent",
            "action": "patch",
            "skill": "k",
            "evidence": {"session_id": sid},
            "before": [{"path": "/h/skills/k/SKILL.md", "sha256": "b0"}],
            "after": [{"path": "/h/skills/k/SKILL.md", "sha256": "a0"}],
        }
        for sid in ("s0", "s2")
    ]
    hermes_data.ledger_path(home).parent.mkdir(parents=True)
    hermes_data.ledger_path(home).write_text(
        "".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8"
    )
    backfill.run(home, since_days=3, log=quiet)
    llm = FakeLlm()
    report = derive.run(home, llm, stages=["edit_review"], log=quiet)
    assert llm.purposes == ["argus.edit_review"]
    stage = report["profiles"]["default"]["stages"]["edit_review"]
    assert (stage["reviewed"], stage["skipped"]) == (1, 1)
    conn = store.open_db(home)
    try:
        pending = count(conn, "select count(*) from skill_edit where derived_by is null")
    finally:
        conn.close()
    assert pending == 1


def test_export_flags_proposals_whose_skill_is_gone(home: Path) -> None:
    backfill.run(home, since_days=7, log=quiet)
    kept = home / "skills" / "tools" / "kept"
    kept.mkdir(parents=True)
    (kept / "SKILL.md").write_text("---\nname: kept\ndescription: d\n---\n\nbody\n")
    conn = store.open_db(home)
    try:
        for pid, name, new in (("p1", "kept", 0), ("p2", "removed", 0), ("p3", "fresh", 1)):
            conn.execute(
                "insert into proposal (id, skill_name, is_new_skill, file, rule, why, diff,"
                " confidence, evidence, derived_by) values (?, ?, ?, ?, 'r', 'w', null, 'medium',"
                " '{}', 1)",
                (pid, name, new, None if new else f"tools/{name}/SKILL.md"),
            )
        conn.commit()
    finally:
        conn.close()
    harness = export.build(home)["harnesses"]["hermes"]
    props = harness["proposals"]
    assert {p["id"]: p["skill_gone"] for p in props} == {"p1": False, "p2": True, "p3": False}
    # no ledger entry removed it: missing (removed outside Hermes), with no last entry in the window
    assert harness["absent"] == {
        "default:removed": {"state": "missing", "ts": None, "entry": None, "action": None}
    }
    conn = store.open_db(home)
    try:
        # the obsolete one is not pending; a dismissed one neither
        assert store.pending_proposals(conn, home, ("accept", "dismiss")) == 2
        conn.execute(
            "insert into decision (target_kind, target_id, action, target_snapshot, decided_at)"
            " values ('proposal', 'p3', 'dismiss', '{}', 1)"
        )
        assert store.pending_proposals(conn, home, ("accept", "dismiss")) == 1
    finally:
        conn.close()


def test_export_tells_an_approved_staged_apply_from_a_rejected_one(home: Path) -> None:
    """A staged apply counts while Hermes holds its pending write. Once the write is gone it was
    approved, when the ledger has the proposal's write since (under the approver's actor, without
    the pending id), or rejected. An approved one carries its ledger entry, hence its rollback."""
    backfill.run(home, since_days=7, log=quiet)
    body = "---\nname: kept\ndescription: d\n---\n\nbody\n"
    kept = home / "skills" / "tools" / "kept"
    kept.mkdir(parents=True)
    (kept / "SKILL.md").write_text(body + "- Always check X.\n")
    (home / "pending" / "skills").mkdir(parents=True)
    (home / "pending" / "skills" / "held.json").write_text("{}")
    diff = "--- a/SKILL.md\n+++ b/SKILL.md\n@@ -6 +6,2 @@\n body\n+- {}\n"
    rule = "Use fresh for Y. It saves a step."
    now = time.time()
    conn = store.open_db(home)
    try:
        for pid, name, new, d, pending in (
            ("approved", "kept", 0, diff.format("Always check X."), "gone1"),
            ("held", "kept", 0, diff.format("Prefer Z."), "held"),
            ("rejected", "kept", 0, diff.format("Never do W."), "gone2"),
            ("created", "fresh", 1, None, "gone3"),
        ):
            conn.execute(
                "insert into proposal (id, skill_name, is_new_skill, file, rule, why, diff,"
                " confidence, evidence, derived_by) values (?, ?, ?, ?, ?, 'w', ?, 'medium',"
                " '{}', 1)",
                (pid, name, new, None if new else "tools/kept/SKILL.md", rule, d),
            )
            conn.execute(
                "insert into apply_log (target_kind, target_id, outcome, message, ledger_entry,"
                " pending_id, applied_at, applied_by) values ('proposal', ?, 'staged', 'm', null,"
                " ?, ?, 'dashboard')",
                (pid, pending, now - 7200),
            )
        conn.commit()
    finally:
        conn.close()
    blobs = home / ".curator_backups" / "blobs"
    blobs.mkdir(parents=True)
    for sha, text in (
        ("b0", body),
        ("a0", body + "- Always check X.\n"),
        ("c0", apply.new_skill("fresh", rule)),
    ):
        (blobs / sha).write_text(text, encoding="utf-8")
    when = datetime.fromtimestamp(now - 3600, UTC).isoformat()
    kp, fp = str(kept / "SKILL.md"), str(home / "skills" / "fresh" / "SKILL.md")
    ledger = [
        {"id": "e1", "ts": when, "actor": "approver", "action": "patch", "skill": "kept",
         "before": [{"path": kp, "sha256": "b0"}], "after": [{"path": kp, "sha256": "a0"}]},
        {"id": "e2", "ts": when, "actor": "approver", "action": "create", "skill": "fresh",
         "after": [{"path": fp, "sha256": "c0"}]},
    ]  # fmt: skip
    hermes_data.ledger_path(home).write_text(
        "".join(json.dumps(x) + "\n" for x in ledger), encoding="utf-8"
    )

    def state() -> dict[str, tuple[str | None, str | None, str | None]]:
        props = export.build(home)["harnesses"]["hermes"]["proposals"]
        return {
            p["id"]: (a["outcome"], a["ledger_entry"], a.get("rolled_back")) if (a := p["applied"])
            else (None, None, None)
            for p in props
        }  # fmt: skip

    assert state() == {
        "approved": ("applied", "e1", None),
        "held": ("staged", None, None),
        "rejected": (None, None, None),
        "created": ("applied", "e2", None),
    }
    rollback = {"id": "r1", "ts": datetime.fromtimestamp(now - 60, UTC).isoformat(),
                "actor": "user", "action": "rollback", "skill": "kept",
                "evidence": {"rollback_target": "e1"}}  # fmt: skip
    with hermes_data.ledger_path(home).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rollback) + "\n")
    assert state()["approved"] == ("applied", "e1", "r1")
