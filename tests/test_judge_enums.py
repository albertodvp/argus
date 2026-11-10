"""derive stores only schema values for the judge's enum fields: an out-of-enum status / stable /
who is stored as if missing, so model text never reaches the dashboard as a class or label."""

import time
from pathlib import Path

from test_window import make_home

from argus import backfill, derive, store
from argus.dbtypes import INTENT_ROW, query_as
from argus.jsontypes import JsonObject

EVIL = '"><img src=x onerror=alert(1)>'


def fake_llm(instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
    if purpose == "argus.assign":
        return {
            "turns": [
                {
                    "turn": "T1",
                    "intent": "NEW",
                    "title": "t",
                    "goal": "g",
                    "kind": "task",
                    "who": "real-user",
                }
            ]
        }
    if purpose == "argus.judge":
        return {"status": EVIL, "stable": EVIL, "who": EVIL, "headline": EVIL, "attention": 1}
    return None


def test_judge_out_of_enum_values_stored_as_missing(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    make_home(home, time.time())
    backfill.run(home, since_days=30, log=lambda _l: None)
    derive.run(home, fake_llm, stages=["assign", "judge"], log=lambda _l: None)
    conn = store.open_db(home)
    try:
        rows = query_as(conn, INTENT_ROW, "select * from intent")
    finally:
        conn.close()
    assert rows
    for r in rows:
        assert (r["status"], r["stable"]) == (None, None)
        assert r["who"] == "real-user"  # kept from assign, not overwritten
        assert r["headline"] == EVIL  # free text stays (escaped at render)
        assert r["judged_at"] is not None
