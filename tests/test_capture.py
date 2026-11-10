"""Hooks record what Hermes sends them whatever its types: a hook argument of an unexpected type
narrows to a usable value instead of dropping the row."""

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_window import make_home

from argus import capture, hermes_data, store
from argus.dbtypes import query


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "hermes"
    make_home(h, time.time())
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


def rows(home: Path, sql: str) -> list[dict[str, str | int | float | bytes | None]]:
    assert store.writer(home).flush()
    conn = store.open_db(home)
    try:
        return query(conn, sql)
    finally:
        conn.close()


def test_tool_error_with_a_structured_message_is_recorded(home: Path) -> None:
    capture.post_tool_call(
        tool_name="terminal",
        result=None,
        session_id="s1",
        tool_call_id="c1",
        status="error",
        error_message={"code": 7, "detail": "boom"},
    )
    got = rows(home, "select tool_call_id, status, error_sig from tool_outcome")
    assert len(got) == 1
    assert got[0]["tool_call_id"] == "c1" and got[0]["status"] == "error"
    assert "boom" in str(got[0]["error_sig"])


def test_session_delete_with_one_id_as_text(home: Path) -> None:
    for sid in ("s1", "s2"):
        capture.post_tool_call(tool_name="x", session_id=sid, tool_call_id=f"c-{sid}")
    assert len(rows(home, "select * from tool_outcome")) == 2
    capture.on_session_delete(deleted_ids="s1")
    assert [r["session_id"] for r in rows(home, "select * from tool_outcome")] == ["s2"]


def test_session_delete_keeps_its_skill_edit_unlinked(home: Path) -> None:
    # the ledger entry and the skill change outlive the session: its review must survive too
    store.writer(home).put(
        "insert into skill_edit (ledger_entry_id, ts, skill_name, session_id, turn_id, origin,"
        " evidence) values ('e1', 1, 'sk', 's1', 't1', 'hook', 'speculative')",
        (),
    )
    capture.on_session_delete(deleted_ids=["s1"])
    got = rows(home, "select ledger_entry_id, session_id, turn_id, evidence from skill_edit")
    assert got == [
        {"ledger_entry_id": "e1", "session_id": None, "turn_id": None, "evidence": "speculative"}
    ]


def write_ledger(home: Path, entries: list[tuple[str, str, float, str | None]]) -> None:
    """Ledger lines (id, skill, epoch ts, session id in the evidence) as Hermes writes them."""
    path = hermes_data.ledger_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for eid, skill, ts, sid in entries:
        when = datetime.fromtimestamp(ts, UTC).isoformat()
        evidence = f', "evidence": {{"session_id": "{sid}"}}' if sid else ""
        lines.append(
            f'{{"id": "{eid}", "ts": "{when}", "actor": "agent", "action": "patch",'
            f' "skill": "{skill}"{evidence}}}'
        )
    path.write_text("\n".join(lines) + "\n")


def linked(home: Path) -> list[tuple[str, str, str]]:
    got = rows(
        home,
        "select ledger_entry_id, tool_call_id, origin from skill_edit order by ledger_entry_id",
    )
    return [(str(r["ledger_entry_id"]), str(r["tool_call_id"]), str(r["origin"])) for r in got]


def test_skill_manage_operations_call_links_its_ledger_entries(home: Path) -> None:
    # Hermes 0.23 advertises only the operations shape; each operation writes its own entry.
    now = time.time()
    write_ledger(
        home,
        [
            ("e1", "demo", now - 1, "s1"),
            ("e2", "demo", now - 0.5, "s1"),
            ("e3", "other", now - 0.4, "s1"),
            ("x1", "demo", now - 0.3, "s2"),  # same skill, another session's edit
        ],
    )
    capture.post_tool_call(
        tool_name="skill_manage",
        args={
            "operations": [
                {"action": "patch", "name": "demo"},
                {"action": "write_file", "name": "demo"},
                {"action": "patch", "name": "other"},
            ]
        },
        session_id="s1",
        tool_call_id="c1",
        status="ok",
    )
    assert linked(home) == [("e1", "c1", "hook"), ("e2", "c1", "hook"), ("e3", "c1", "hook")]


def test_skill_manage_operations_fall_back_to_the_top_level_name(home: Path) -> None:
    now = time.time()
    write_ledger(home, [("e1", "demo", now - 1, None)])
    capture.post_tool_call(
        tool_name="skill_manage",
        args='{"name": "demo", "operations": [{"action": "patch"}]}',
        session_id="s1",
        tool_call_id="c1",
        status="ok",
    )
    assert linked(home) == [("e1", "c1", "hook")]


def test_skill_manage_flat_call_still_links(home: Path) -> None:
    now = time.time()
    write_ledger(home, [("e1", "demo", now - 1, "s1")])
    capture.post_tool_call(
        tool_name="skill_manage",
        args={"action": "patch", "name": "demo"},
        session_id="s1",
        tool_call_id="c1",
    )
    assert linked(home) == [("e1", "c1", "hook")]


def test_a_later_call_on_the_same_skill_keeps_its_own_entry(home: Path) -> None:
    now = time.time()
    write_ledger(home, [("e1", "demo", now - 2, "s1")])
    flat = {"action": "patch", "name": "demo"}
    capture.post_tool_call(
        tool_name="skill_manage", args=flat, session_id="s1", tool_call_id="c1", status="ok"
    )
    assert linked(home) == [("e1", "c1", "hook")]
    write_ledger(home, [("e1", "demo", now - 2, "s1"), ("e2", "demo", now - 1, "s1")])
    capture.post_tool_call(
        tool_name="skill_manage", args=flat, session_id="s1", tool_call_id="c2", status="ok"
    )
    assert linked(home) == [("e1", "c1", "hook"), ("e2", "c2", "hook")]
