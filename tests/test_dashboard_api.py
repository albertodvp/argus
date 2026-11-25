"""The dashboard's /decision and /apply bodies: anything that is not a JSON object with a known
kind, action, id and profile is a 400; a decision is recorded, and cleared, in its profile, only
with the proof of a person the dashboard made of an authorized request."""

import time
from pathlib import Path

import pytest
from test_window import make_home

from argus import checks, store
from argus.dashboard import plugin_api
from argus.dbtypes import count

AUTHORIZED = object()
"""Stands for a dashboard request Hermes' route guard authorized."""


def _authorized(request: object) -> bool:
    return request is AUTHORIZED


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "hermes"
    make_home(h, time.time())
    monkeypatch.setattr(plugin_api, "_root", lambda: h)
    # the dashboard backend imports Argus under a package name of its own
    monkeypatch.setattr(f"{plugin_api._PKG}.checks.agent_session", lambda: None)
    monkeypatch.setattr(
        f"{plugin_api._PKG}.hermes_internals.dashboard_request_authorized", _authorized
    )
    return h


def person() -> checks.Person:
    """What the /decision route makes of an authorized request, in the dashboard's package."""
    who = plugin_api._person(AUTHORIZED)
    assert not isinstance(who, str), who
    return who


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
    code, out = plugin_api._decide(body, person())
    assert code == 400
    assert error in str(out)
    assert decisions(home) == 0


def test_decision_is_recorded_and_cleared(home: Path) -> None:
    assert plugin_api._decide(VALID, person())[0] == 200
    assert decisions(home) == 1
    code, _out = plugin_api._decide({**VALID, "action": "clear", "snapshot": None}, person())
    assert code == 200
    assert decisions(home) == 0


def test_a_decision_needs_a_person_proof_checked_again(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision writer runs the person gate itself: a proof Argus' dashboard package did not
    mint, or one whose gate no longer passes, is refused and writes nothing."""
    who = person()
    assert plugin_api._decide(VALID, who)[0] == 200
    forged = checks.Person("dashboard", object(), AUTHORIZED)  # not minted by the gate
    code, out = plugin_api._decide({**VALID, "action": "dismiss"}, forged)
    assert code == 403
    assert "a person's" in str(out)
    monkeypatch.setattr(
        f"{plugin_api._PKG}.checks.agent_session", lambda: "HERMES_SESSION_ID is set"
    )
    code, out = plugin_api._decide({**VALID, "action": "clear", "snapshot": None}, who)
    assert code == 403
    assert "HERMES_SESSION_ID" in str(out)
    assert decisions(home) == 1


def test_apply_needs_the_matching_decision(home: Path) -> None:
    body = {"target_kind": "proposal", "target_id": "p1", "profile": "default"}
    code, out = plugin_api._apply(body)
    assert code == 400
    assert 'no current "accept" decision' in str(out)
    assert plugin_api._apply("x")[0] == 400
