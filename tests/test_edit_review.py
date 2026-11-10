"""edit_review judges an edit only on a run state.db holds: an edit whose session has no messages
gets `no-run` (judged without its run it would read as speculative), a person's edit or a rollback
is not reviewed, and rows of an older review version are relabelled without model calls."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_window import DAY, make_home, quiet

from argus import backfill, derive, export, hermes_data, store
from argus.dbtypes import SKILL_EDIT_ROW, SkillEditRow, query_as
from argus.jsontypes import JsonObject

# ledger entry id -> (actor, action, session id); s2 is in state.db (make_home), "gone" is not
EDITS: dict[str, tuple[str, str, str | None]] = {
    "run": ("agent", "patch", "s2"),
    "review": ("curator", "patch", "s2"),
    "gone": ("curator", "patch", "gone"),
    "nosession": ("curator", "patch", None),
    "undo": ("user", "rollback", None),
    "manual": ("user", "patch", None),
}


class Llm:
    def __init__(self) -> None:
        self.purposes: list[str] = []

    def __call__(self, instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
        self.purposes.append(purpose)
        if purpose == "argus.edit_review":
            return {"learned": "judged", "trigger": "t", "evidence_in_run": "observed-failure"}
        if purpose == "argus.edit_learned":
            return {"learned": "from the diff"}
        return None


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "hermes"
    make_home(h, time.time())
    blobs = h / ".curator_backups" / "blobs"
    blobs.mkdir(parents=True)
    for sha, text in (("b0", "# skill\nold\n"), ("a0", "# skill\nnew\n")):
        (blobs / sha).write_text(text, encoding="utf-8")
    when = datetime.fromtimestamp(time.time() - 0.5 * DAY, UTC).isoformat()
    lines = [
        {
            "id": eid,
            "ts": when,
            "actor": actor,
            "action": action,
            "skill": "k",
            "evidence": {"session_id": sid} if sid else {},
            "before": [{"path": "/h/skills/k/SKILL.md", "sha256": "b0"}],
            "after": [{"path": "/h/skills/k/SKILL.md", "sha256": "a0"}],
        }
        for eid, (actor, action, sid) in EDITS.items()
    ]
    hermes_data.ledger_path(h).parent.mkdir(parents=True)
    hermes_data.ledger_path(h).write_text(
        "".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8"
    )
    backfill.run(h, since_days=0, log=quiet)  # the whole history: no window guards anything
    return h


def edits(home: Path) -> dict[str, SkillEditRow]:
    conn = store.open_db(home)
    try:
        rows = query_as(conn, SKILL_EDIT_ROW, "select * from skill_edit")
    finally:
        conn.close()
    return {r["ledger_entry_id"]: r for r in rows}


def labels(home: Path) -> dict[str, tuple[str | None, str | None, str | None]]:
    return {k: (r["evidence"], r["learned"], r["trigger"]) for k, r in edits(home).items()}


def test_only_edits_with_a_run_are_judged_on_it(home: Path) -> None:
    llm = Llm()
    derive.run(home, llm, stages=["edit_review"], log=quiet)
    assert sorted(llm.purposes) == ["argus.edit_learned"] * 2 + ["argus.edit_review"] * 2
    assert labels(home) == {
        "run": ("observed-failure", "judged", "t"),
        "review": ("observed-failure", "judged", "t"),
        "gone": ("no-run", "from the diff", None),
        "nosession": ("no-run", "from the diff", None),
        "undo": (None, None, None),
        "manual": (None, None, None),
    }
    assert {r["derived_by"] for r in edits(home).values()} == {derive.V_REVIEW}

    llm = Llm()
    derive.run(home, llm, stages=["edit_review"], log=quiet)
    assert llm.purposes == []  # every edit is done


def test_older_review_version_is_relabelled_without_model_calls(home: Path) -> None:
    conn = store.open_db(home)
    try:
        # what edit_review/v1 stored: every edit judged, those without a run as speculative
        conn.execute(
            "update skill_edit set evidence='speculative', learned='old rule', trigger='none',"
            " derived_by='edit_review/v1'"
        )
        conn.execute("update skill_edit set evidence='user-correction' where ledger_entry_id='run'")
        conn.commit()
    finally:
        conn.close()
    llm = Llm()
    report = derive.run(home, llm, stages=["edit_review"], log=quiet)
    assert llm.purposes == []
    assert report["profiles"]["default"]["stages"]["edit_review"]["relabelled"] == 4
    assert labels(home) == {
        "run": ("user-correction", "old rule", "none"),
        "review": ("speculative", "old rule", "none"),
        "gone": ("no-run", "old rule", None),
        "nosession": ("no-run", "old rule", None),
        "undo": (None, None, None),
        "manual": (None, None, None),
    }


def test_curator_actor_in_a_session_with_messages_is_the_background_review() -> None:
    with_messages = {"s2"}
    assert export.writer_of("curator", "s2", with_messages) == "review"
    assert export.writer_of("curator", "gone", with_messages) == "curator"
    assert export.writer_of("curator", None, with_messages) == "curator"
    assert export.writer_of("agent", "s2", with_messages) == "agent"
