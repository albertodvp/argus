"""SQLite rows without `Any`.

`sqlite3` cursors return `Any`. Read through these helpers instead: they run the query on a cursor
whose row factory is reset to plain tuples (whatever the connection's `row_factory`), check every
value against the five SQLite storage classes (`SqlValue`) and return `dict[str, SqlValue]` keyed
by column name, in select order. Values are not converted. Writes need no helper:
`conn.execute(sql, params)` is fully typed.

For a precise row type, validate with the adapter of a TypedDict below (`query_as`): one per table
of `schema.sql`, the column types following SQLite affinity (REAL -> float, INTEGER -> int, TEXT ->
str; nullable columns `| None`, CHECK-constrained ones `Literal`). Validation is strict except that
a REAL column's integer becomes a float; a row that does not fit raises `pydantic.ValidationError`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Literal, TypeAlias, TypeVar

from pydantic import TypeAdapter
from typing_extensions import TypedDict

__all__ = [
    "APPLY_LOG_ROW",
    "DECISION_ROW",
    "INTENT_ROW",
    "META_ROW",
    "PROPOSAL_ROW",
    "RUN_ROW",
    "SKILL_EDIT_ROW",
    "SQLITE_ROW",
    "TOOL_OUTCOME_ROW",
    "TURN_INTENT_ROW",
    "TURN_OUTCOME_ROW",
    "ApplyLogRow",
    "DecisionRow",
    "IntentRow",
    "MetaRow",
    "ProposalRow",
    "Row",
    "RunRow",
    "SkillEditRow",
    "SqlParams",
    "SqlValue",
    "ToolOutcomeRow",
    "TurnIntentRow",
    "TurnOutcomeRow",
    "count",
    "iter_rows",
    "query",
    "query_as",
    "query_one",
    "scalar",
    "sql_value",
]

SqlValue: TypeAlias = str | int | float | bytes | None
SqlParams: TypeAlias = Sequence[SqlValue] | Mapping[str, SqlValue]
Row: TypeAlias = dict[str, SqlValue]

T = TypeVar("T")

FETCH = 500  # rows per fetchmany while streaming

SQLITE_ROW: Callable[[sqlite3.Cursor, tuple[object, ...]], object] = sqlite3.Row
"""`sqlite3.Row` as a typed row factory (`conn.row_factory = SQLITE_ROW`); the class object
itself is typed with Any. Rows read through this module are unaffected by it."""


def sql_value(value: object) -> SqlValue:
    """`value` as one of SQLite's storage classes; TypeError for anything else (a converter's
    output, say)."""
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    raise TypeError(f"not an SQLite value: {type(value).__name__}")


def iter_rows(conn: sqlite3.Connection, sql: str, params: SqlParams = ()) -> Iterator[Row]:
    """Stream the rows of one query as `{column: value}`."""
    cur = conn.cursor()
    cur.row_factory = None  # plain tuples, even on a connection with row_factory = sqlite3.Row
    try:
        cur.execute(sql, params)
        description: Sequence[Sequence[object]] | None = cur.description
        names = [str(d[0]) for d in description or ()]
        while True:
            batch: list[object] = cur.fetchmany(FETCH)
            if not batch:
                return
            for raw in batch:
                if not isinstance(raw, tuple):
                    raise TypeError(f"not an SQLite row: {type(raw).__name__}")
                values: tuple[object, ...] = raw
                yield {name: sql_value(v) for name, v in zip(names, values, strict=True)}
    finally:
        cur.close()


def query(conn: sqlite3.Connection, sql: str, params: SqlParams = ()) -> list[Row]:
    """Every row of one query."""
    return list(iter_rows(conn, sql, params))


def query_one(conn: sqlite3.Connection, sql: str, params: SqlParams = ()) -> Row | None:
    """The first row of one query, or None when it returns none."""
    return next(iter_rows(conn, sql, params), None)


def scalar(conn: sqlite3.Connection, sql: str, params: SqlParams = ()) -> SqlValue:
    """First column of the first row; None also when there is no row."""
    row = query_one(conn, sql, params)
    return None if row is None else next(iter(row.values()), None)


def count(conn: sqlite3.Connection, sql: str, params: SqlParams = ()) -> int:
    """The integer a `select count(*) ...` returns; TypeError when the result is not an integer."""
    v = scalar(conn, sql, params)
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypeError(f"not a count: {v!r}")
    return v


def query_as(
    conn: sqlite3.Connection, adapter: TypeAdapter[T], sql: str, params: SqlParams = ()
) -> list[T]:
    """Every row of one query validated (strictly) into the adapter's type, e.g. `INTENT_ROW`."""
    return [adapter.validate_python(r, strict=True) for r in iter_rows(conn, sql, params)]


# ---------------------------------------------------------------- plugin tables (schema.sql)
class MetaRow(TypedDict):
    key: str
    value: str


class ToolOutcomeRow(TypedDict):
    tool_call_id: str
    session_id: str
    ts: float
    tool_name: str
    status: str | None
    error_type: str | None
    error_message: str | None
    duration_ms: int | float | None  # INTEGER affinity keeps a non-integral float as REAL
    origin: Literal["hook", "backfill"]
    error_sig: str | None
    error_category: str | None
    derived_by: str | None


class TurnOutcomeRow(TypedDict):
    turn_id: str
    session_id: str
    started_at: float
    ended_at: float | None
    exit_reason: str | None
    completed: int | None
    interrupted: int | None
    failed: int | None


class SkillEditRow(TypedDict):
    ledger_entry_id: str
    ts: float
    skill_name: str
    provenance: str | None
    session_id: str | None
    turn_id: str | None
    tool_call_id: str | None
    origin: Literal["hook", "backfill"]
    evidence: str | None
    learned: str | None
    trigger: str | None
    derived_by: str | None


class IntentRow(TypedDict):
    id: str
    title: str
    goal: str | None
    kind: str
    who: str | None
    status: str | None
    stable: str | None
    risk: str | None
    headline: str | None
    outcome: str | None
    consistency: str | None
    friction: str | None
    next: str | None
    attention: int | None
    merged_into: str | None
    created_at: float
    updated_at: float
    judged_at: float | None
    derived_by: str


class TurnIntentRow(TypedDict):
    session_id: str
    user_message_id: int
    intent_id: str
    gate: str
    derived_by: str


class RunRow(TypedDict):
    intent_id: str
    session_id: str
    started_at: float
    signal_status: str | None
    model_ok: int | None
    model_result: str | None
    derived_by: str


class ProposalRow(TypedDict):
    id: str
    skill_name: str
    is_new_skill: int
    file: str | None
    rule: str
    why: str | None
    diff: str | None
    confidence: str | None
    evidence: str
    derived_by: str


class DecisionRow(TypedDict):
    id: int
    target_kind: Literal["skill_edit", "proposal"]
    target_id: str
    action: Literal["keep", "reject", "accept", "dismiss"]
    target_snapshot: str
    decided_at: float
    decided_by: str | None
    note: str | None
    executed_at: float | None
    execution_result: str | None
    ledger_entry_id: str | None


class ApplyLogRow(TypedDict):
    id: int
    target_kind: Literal["skill_edit", "proposal"]
    target_id: str
    outcome: Literal["applied", "staged", "refused", "failed", "unknown"]
    message: str | None
    ledger_entry: str | None
    pending_id: str | None
    applied_at: float
    applied_by: str | None


META_ROW = TypeAdapter(MetaRow)
TOOL_OUTCOME_ROW = TypeAdapter(ToolOutcomeRow)
TURN_OUTCOME_ROW = TypeAdapter(TurnOutcomeRow)
SKILL_EDIT_ROW = TypeAdapter(SkillEditRow)
INTENT_ROW = TypeAdapter(IntentRow)
TURN_INTENT_ROW = TypeAdapter(TurnIntentRow)
RUN_ROW = TypeAdapter(RunRow)
PROPOSAL_ROW = TypeAdapter(ProposalRow)
DECISION_ROW = TypeAdapter(DecisionRow)
APPLY_LOG_ROW = TypeAdapter(ApplyLogRow)
