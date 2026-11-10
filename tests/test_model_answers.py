"""A model answer is checked against its schema's types once, when it arrives: one that does not fit
is a failed call (counted, its unit of work skipped) and the stage goes on with the next unit."""

import time
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from test_window import make_home, quiet

from argus import backfill, derive, prompts, store
from argus.dbtypes import INTENT_ROW, count, query_as
from argus.jsontypes import JSON_OBJECT, JsonObject, JsonValue


class Llm:
    """assign: the first session's answer has a number where the title is text."""

    def __init__(self) -> None:
        self.assign_calls = 0

    def __call__(self, instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
        if purpose == "argus.assign":
            self.assign_calls += 1
            title: JsonObject = {"title": 123 if self.assign_calls == 1 else "a title"}
            return {"turns": [{"turn": "T1", "intent": "NEW", **title}]}
        if purpose == "argus.judge":
            return {"status": "achieved", "attention": "very"}
        return None


def test_an_answer_that_does_not_fit_skips_only_its_unit(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    make_home(home, time.time())
    backfill.run(home, since_days=0, log=quiet)
    llm = Llm()
    report = derive.run(home, llm, stages=["assign", "judge"], log=quiet)
    assign = report["profiles"]["default"]["stages"]["assign"]
    assert "error" not in assign
    assert llm.assign_calls == 3  # every session is still asked
    assert (assign["llm"], assign["unassigned"], assign["llm_failures"]) == (2, 1, 1)
    judge = report["profiles"]["default"]["stages"]["judge"]
    assert (judge["candidates"], judge["judged"], judge["llm_failures"]) == (2, 0, 2)
    conn = store.open_db(home)
    try:
        assert count(conn, "select count(*) from turn_intent") == 2
        assert all(
            r["judged_at"] is None for r in query_as(conn, INTENT_ROW, "select * from intent")
        )
    finally:
        conn.close()


class DownLlm:
    """A provider that is down: every call raises."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
        self.calls += 1
        raise ConnectionError("provider unreachable")


def test_a_model_that_keeps_raising_stops_the_run(tmp_path: Path) -> None:
    root = tmp_path / "hermes"
    homes = [root, root / "profiles" / "other", root / "profiles" / "third"]
    for h in homes:
        make_home(h, time.time())
        backfill.run(h, since_days=0, log=quiet)
    llm = DownLlm()
    report = derive.run(root, llm, log=quiet)
    # stops after ABORT_AFTER errors in a row (they span profiles: the provider is shared) instead
    # of trying every unit of every profile: 3 sessions in default, the 5th call in `other`
    assert llm.calls == derive.ABORT_AFTER
    assert "raised" in report.get("aborted", "")
    assert list(report["profiles"]["other"]["stages"]) == ["assign"]
    assert "third" not in report["profiles"]


def _fields(schema: JsonValue) -> JsonValue:
    """The property names of a JSON schema and of the objects inside it, as nested dicts."""
    if not isinstance(schema, dict):
        return None
    if schema.get("type") == "array":
        return _fields(schema.get("items"))
    props = schema.get("properties")
    if not isinstance(props, dict):
        return None
    return {k: _fields(v) for k, v in props.items()}


def _resolved(schema: JsonObject) -> JsonObject:
    """A pydantic JSON schema with its $refs inlined."""
    defs = schema.get("$defs")

    def walk(v: JsonValue) -> JsonValue:
        if isinstance(v, list):
            return [walk(x) for x in v]
        if not isinstance(v, dict):
            return v
        ref = v.get("$ref")
        if isinstance(ref, str) and isinstance(defs, dict):
            return walk(defs[ref.rsplit("/", 1)[-1]])
        return {k: walk(x) for k, x in v.items()}

    out = walk(schema)
    assert isinstance(out, dict)
    return out


@pytest.mark.parametrize(
    ("answer", "schema"),
    [
        (derive.ASSIGN_ANSWER, prompts.ASSIGN_SCHEMA),
        (derive.CONS_ANSWER, prompts.CONS_SCHEMA),
        (derive.JUDGEMENT, prompts.OUT_SCHEMA),
        (derive.RUN_ANSWER, prompts.RUN_SCHEMA),
        (derive.REVIEW, prompts.REV_SCHEMA),
        (derive.PROPOSALS_ANSWER, prompts.PROP_SCHEMA),
        (derive.PATCH_ANSWER, prompts.PATCH_SCHEMA),
    ],
)
def test_answer_types_name_the_schema_fields(
    answer: TypeAdapter[object], schema: JsonObject
) -> None:
    # the prompts' schemas are what the model sees (and what other installs pin): the answer
    # types must not drift from them
    raw: object = answer.json_schema()
    assert _fields(_resolved(JSON_OBJECT.validate_python(raw))) == _fields(schema)
