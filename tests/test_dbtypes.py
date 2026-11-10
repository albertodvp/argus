"""argus.dbtypes reads rows without converting values, whatever the connection's row factory."""

import sqlite3
from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from argus import dbtypes

SCHEMA = (
    "create table t (k text primary key, r real, i integer, b blob, n text);"
    "insert into t values ('a', 1.5, 2, x'00ff', null), ('b', 3, 4, null, 'x');"
)


@pytest.fixture(params=["tuples", "sqlite3.Row"])
def conn(request: pytest.FixtureRequest) -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    factory: object = request.param
    if factory == "sqlite3.Row":
        c.row_factory = dbtypes.SQLITE_ROW
    c.executescript(SCHEMA)
    yield c
    c.close()


def test_query_returns_typed_dicts_in_select_order(conn: sqlite3.Connection) -> None:
    rows = dbtypes.query(conn, "select * from t order by k")
    assert rows == [
        {"k": "a", "r": 1.5, "i": 2, "b": b"\x00\xff", "n": None},
        {"k": "b", "r": 3.0, "i": 4, "b": None, "n": "x"},
    ]
    assert list(rows[0]) == ["k", "r", "i", "b", "n"]


def test_query_one_scalar_count(conn: sqlite3.Connection) -> None:
    assert dbtypes.query_one(conn, "select k from t where i = ?", (4,)) == {"k": "b"}
    assert dbtypes.query_one(conn, "select k from t where i = ?", (9,)) is None
    assert dbtypes.scalar(conn, "select r from t where k = :k", {"k": "a"}) == 1.5
    assert dbtypes.scalar(conn, "select r from t where k = 'zz'") is None
    assert dbtypes.count(conn, "select count(*) from t") == 2
    with pytest.raises(TypeError):
        dbtypes.count(conn, "select k from t")


def test_iter_rows_streams_past_one_batch(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "insert into t (k) values (?)", [(f"z{i:04}",) for i in range(dbtypes.FETCH + 7)]
    )
    assert sum(1 for _ in dbtypes.iter_rows(conn, "select k from t")) == dbtypes.FETCH + 9


def test_sql_value_rejects_converted_values() -> None:
    assert dbtypes.sql_value(memoryview(b"x").tobytes()) == b"x"
    with pytest.raises(TypeError):
        dbtypes.sql_value(object())


def test_query_as_validates_table_rows() -> None:
    c = sqlite3.connect(":memory:")
    c.execute("create table meta (key text primary key, value text not null)")
    c.execute("insert into meta values ('schema_version', '2')")
    assert dbtypes.query_as(c, dbtypes.META_ROW, "select * from meta") == [
        {"key": "schema_version", "value": "2"}
    ]
    c.execute("create table m2 (key text, value integer)")
    c.execute("insert into m2 values ('x', 1)")
    with pytest.raises(ValidationError):
        dbtypes.query_as(c, dbtypes.META_ROW, "select * from m2")
    c.close()
