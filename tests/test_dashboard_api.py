"""The dashboard's /decision and /apply bodies: anything that is not a JSON object with a known
kind, action, id and profile is a 400; a decision is recorded, and cleared, in its profile."""

import time
from pathlib import Path

import pytest
from test_window import make_home

from argus import store
from argus.dashboard import plugin_api
from argus.dbtypes import count


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "hermes"
    make_home(h, time.time())
    monkeypatch.setattr(plugin_api, "_root", lambda: h)
    return h


def decisions(home: Path) -> int:
    conn = store.open_db(home)
    try:
        return count(conn, "select count(*) from decision")
    finally:
        conn.close()


VALID: dict[str, object] = {
    "target_kind": "proposal",
    "target_id": "p1",
    "action": "accept",
    "profile": "default",
    "snapshot": {"rule": "r"},
}


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (None, "expected a JSON object"),
        ({"target_kind": "intent"}, "target_kind must be one of"),
        ({"action": "keep"}, "action for proposal must be one of"),
        ({"target_id": " "}, "target_id must be a non-empty string"),
        ({"profile": "nobody"}, "unknown profile"),
        ({"snapshot": "text"}, "snapshot must be a JSON object"),
    ],
)
def test_bad_decisions_are_refused(
    home: Path, change: dict[str, object] | None, error: str
) -> None:
    body: object = [VALID] if change is None else {**VALID, **change}
    code, out = plugin_api._decide(body)
    assert code == 400
    assert error in str(out)
    assert decisions(home) == 0


def test_decision_is_recorded_and_cleared(home: Path) -> None:
    assert plugin_api._decide(VALID)[0] == 200
    assert decisions(home) == 1
    code, _out = plugin_api._decide({**VALID, "action": "clear", "snapshot": None})
    assert code == 200
    assert decisions(home) == 0


def test_apply_needs_the_matching_decision(home: Path) -> None:
    body = {"target_kind": "proposal", "target_id": "p1", "profile": "default"}
    code, out = plugin_api._apply(body)
    assert code == 400
    assert 'no current "accept" decision' in str(out)
    assert plugin_api._apply("x")[0] == 400
