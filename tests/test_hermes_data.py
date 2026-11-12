"""hermes_data reads what state.db holds, skipping what it cannot use instead of failing."""

import sqlite3
import time
from pathlib import Path

from test_window import make_home

from argus import export, hermes_data


def test_skill_reads_skip_malformed_tool_calls(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    make_home(home, time.time())
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute(
            "insert into messages (session_id, role, tool_calls, timestamp) values (?, ?, ?, ?)",
            (
                "s2",
                "assistant",
                (
                    '[1, {"function": {"name": "skill_view",'
                    ' "arguments": "{\\"name\\": \\"a:k\\"}"}},'
                    ' {"function": {"name": "skill_view", "arguments": {"name": "j"}}}]'
                ),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    dump = hermes_data.load_dump(home)
    assert [name for _sid, _ts, name in hermes_data.skill_reads([dump])] == ["k"]
    export.build(home)  # the dashboard export reads the same calls


def test_a_ledger_skill_named_by_its_category_path_is_its_directory() -> None:
    """Apply calls skill_manage with `cat/sk` to pin the file; the skill is still `sk`."""
    e = hermes_data.parse_ledger_line(
        '{"id": "a1", "ts": "2026-11-11T10:00:00+00:00", "skill": "cat/sk", "action": "patch"}'
    )
    assert e is not None
    assert e.get("skill") == "sk"
    assert e.get("qualified") == "cat/sk"
