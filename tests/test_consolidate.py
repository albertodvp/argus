"""consolidate merges, splits and renames intents from the model's ops: a merge moves the dropped
intent's turns to the kept one, a split moves only the named turns to a new intent, and an op on
unknown ids, a self-merge or a split of every turn is ignored."""

import hashlib
import time
from pathlib import Path

import pytest
from test_window import make_home, quiet

from argus import backfill, derive, store
from argus.dbtypes import INTENT_ROW, TURN_INTENT_ROW, IntentRow, query_as
from argus.jsontypes import JsonObject


def intent_of(sid: str, msg_id: int) -> str:
    """The id assign gives a NEW intent opened by this turn."""
    return "i" + hashlib.sha1(f"{sid}:{msg_id}".encode()).hexdigest()[:12]


# make_home: session s<n> has its user message at id 4n + 1
I0, I1, I2 = intent_of("s0", 1), intent_of("s1", 5), intent_of("s2", 9)


class Llm:
    """assign: every session opens its own intent, or with `one_intent` joins the first one.
    consolidate: answers `ops`."""

    def __init__(self, ops: list[JsonObject], one_intent: bool = False) -> None:
        self.ops, self.one_intent, self.assigned = ops, one_intent, 0

    def __call__(self, instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
        if purpose == "argus.assign":
            self.assigned += 1
            pick = I0 if self.one_intent and self.assigned > 1 else "NEW"
            turn: JsonObject = {"turn": "T1", "intent": pick, "title": f"t{self.assigned}"}
            return {"turns": [turn]}
        if purpose == "argus.consolidate":
            return {"ops": self.ops}
        return None


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "hermes"
    make_home(h, time.time())
    backfill.run(h, since_days=0, log=quiet)
    return h


def consolidate(home: Path, llm: Llm) -> dict[str, int | float | str]:
    report = derive.run(home, llm, stages=["assign", "consolidate"], log=quiet)
    return report["profiles"]["default"]["stages"]["consolidate"]


def state(home: Path) -> tuple[dict[str, IntentRow], dict[str, str]]:
    """Intents by id, and the intent of each session's turn."""
    conn = store.open_db(home)
    try:
        intents = {r["id"]: r for r in query_as(conn, INTENT_ROW, "select * from intent")}
        links = {
            r["session_id"]: r["intent_id"]
            for r in query_as(conn, TURN_INTENT_ROW, "select * from turn_intent")
        }
    finally:
        conn.close()
    return intents, links


def test_merge_and_rename(home: Path) -> None:
    stats = consolidate(
        home,
        Llm(
            [
                {"op": "merge", "keep": I0, "drop": I1},
                {"op": "rename", "id": I2, "title": "laptops", "goal": "pick one"},
                {"op": "merge", "keep": I2, "drop": I2},  # self-merge
                {"op": "merge", "keep": I2, "drop": "i-unknown"},
                {"op": "rename", "id": I0, "title": "  "},  # empty title
            ]
        ),
    )
    assert (stats["merge"], stats["rename"], stats["ignored"]) == (1, 1, 3)
    intents, links = state(home)
    assert intents[I1]["merged_into"] == I0
    assert links == {"s0": I0, "s1": I0, "s2": I2}
    assert (intents[I2]["title"], intents[I2]["goal"]) == ("laptops", "pick one")


def test_merge_cycle_is_ignored(home: Path) -> None:
    stats = consolidate(
        home,
        Llm([{"op": "merge", "keep": I0, "drop": I1}, {"op": "merge", "keep": I1, "drop": I0}]),
    )
    assert (stats["merge"], stats["ignored"]) == (1, 1)
    intents, _links = state(home)
    assert (intents[I0]["merged_into"], intents[I1]["merged_into"]) == (None, I0)


def test_split_moves_only_the_named_turns(home: Path) -> None:
    # one intent with three turns, labelled by time: #1 s0, #2 s1, #3 s2
    stats = consolidate(
        home,
        Llm(
            [
                {"op": "split", "from": I0, "turns": [f"{I0}#3"], "title": "laptops"},
                {"op": "split", "from": I0, "turns": [f"{I0}#1", f"{I0}#2"], "title": "all"},
            ],
            one_intent=True,
        ),
    )
    assert (stats["split"], stats["ignored"]) == (1, 1)  # the second would empty I0
    intents, links = state(home)
    new = links["s2"]
    assert new not in (I0, I1, I2)
    assert (links["s0"], links["s1"]) == (I0, I0)
    assert (intents[new]["title"], intents[new]["kind"]) == ("laptops", "task")
