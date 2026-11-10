"""Hook callbacks: record what Hermes does not persist (layer 1). They only enqueue rows on the
profile's store.Writer; they never call a model and never raise.

Hermes guarantees no types for hook arguments: they arrive as `object`, go to SQLite as they are
(sqlite3 rejects, on the writer thread, what it cannot bind) and are narrowed only where Argus
computes with them."""

from __future__ import annotations

import functools
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, ParamSpec, TypeVar

from pydantic import TypeAdapter, ValidationError

from . import hermes_data, signals, store
from .jsontypes import as_number

log = logging.getLogger("hermes.plugins.argus")

P = ParamSpec("P")
K = TypeVar("K")
V = TypeVar("V")

RULES: Final = "rules/v1"
LEDGER_SLACK: Final = 5.0  # seconds between the ledger write and post_tool_call
LEDGER_TAIL: Final = 256 * 1024  # bytes of the ledger read when matching a skill_manage call
MEMO_MAX: Final = 2000

_DICT: Final = TypeAdapter(dict[object, object])
_LIST: Final = TypeAdapter(list[object])

# called with the profile home after every turn (wired by register to the idle derive scheduler)
after_turn: Callable[[Path], None] | None = None

_lock = threading.Lock()
_turn_start: dict[str, float] = {}  # turn_id -> pre_llm_call time
# (session_id, skill) -> latest on_skill_lifecycle provenance
_provenance: dict[tuple[object, str], object] = {}


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _remember(d: dict[K, V], key: K, value: V) -> None:
    with _lock:
        d.pop(key, None)
        d[key] = value
        while len(d) > MEMO_MAX:
            d.pop(next(iter(d)))


def _safe(fn: Callable[P, None]) -> Callable[P, None]:
    @functools.wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            log.warning("argus: hook %s failed", fn.__name__, exc_info=True)
        return None

    return wrapped


def _text(v: object) -> str | None:
    if v is None or isinstance(v, str):
        return v
    try:
        return json.dumps(v, default=str)
    except Exception:
        return str(v)


def _as_dict(v: object) -> dict[object, object] | None:
    """A shallow copy of `v` when it is a dict (or a dict subclass), else None."""
    try:
        return _DICT.validate_python(v, strict=True)
    except ValidationError:
        return None


def _args(v: object) -> dict[object, object]:
    d = _as_dict(v)
    if d is not None:
        return d
    if isinstance(v, str):
        try:
            parsed: object = json.loads(v)
            return _as_dict(parsed) or {}
        except Exception:
            return {}
    return {}


def _ids(deleted_ids: object, session_id: object) -> list[object]:
    """The session ids on_session_delete names: `deleted_ids` (a list, or one id), else
    `session_id`."""
    if not deleted_ids or isinstance(deleted_ids, str):
        return [deleted_ids or session_id]
    try:
        return _LIST.validate_python(deleted_ids)
    except ValidationError:
        return [deleted_ids]


def _skill_key(name: object) -> str:
    return str(name or "").split(":")[-1]


def _skills_of(args: object) -> list[str]:
    """The skills a skill_manage call names, in order and once each: the `name` of every entry of
    `operations` (the only shape Hermes 0.23 advertises; the top-level `name` is its default),
    else the top-level `name` of the flat shape."""
    a = _args(args)
    default = a.get("name")
    try:
        ops: list[object] | None = _LIST.validate_python(a.get("operations"), strict=True)
    except ValidationError:
        ops = None
    names = [(_as_dict(op) or {}).get("name") or default for op in ops] if ops else [default]
    out: list[str] = []
    for n in names:
        key = _skill_key(n)
        if key and key not in out:
            out.append(key)
    return out


# ---------------------------------------------------------------- hooks
@_safe
def pre_llm_call(turn_id: object = None, **kwargs: object) -> None:
    """Turn start time, for turn_outcome.started_at (on_session_end does not carry it)."""
    if turn_id:
        _remember(_turn_start, str(turn_id), time.time())


@_safe
def post_tool_call(
    tool_name: object = None,
    args: object = None,
    result: object = None,
    session_id: object = None,
    tool_call_id: object = None,
    turn_id: object = None,
    duration_ms: object = None,
    status: object = None,
    error_type: object = None,
    error_message: object = None,
    **kwargs: object,
) -> None:
    if not tool_call_id or not tool_name:
        return
    now = time.time()
    home = _home()
    sig = signals.tool_error(_text(result))
    if not sig and status and status != "ok":
        reason = error_message or error_type or status
        sig = signals.norm_err(reason if isinstance(reason, str) else _text(reason) or "")
        sig = sig or str(status)
    store.writer(home).put(
        "insert or replace into tool_outcome(tool_call_id, session_id, ts, tool_name, status,"
        " error_type, error_message, duration_ms, origin, error_sig, error_category, derived_by)"
        " values (?, ?, ?, ?, ?, ?, ?, ?, 'hook', ?, ?, ?)",
        (
            str(tool_call_id),
            session_id or "",
            now,
            tool_name,
            status,
            error_type,
            (error_message or None) and str(error_message)[:500],
            duration_ms,
            sig,
            signals.err_category(sig) if sig else None,
            RULES if sig else None,
        ),
    )
    if tool_name == "skill_manage" and (status in (None, "ok")):
        for skill in _skills_of(args):
            _link_skill_edit(
                home, skill, session_id or "", turn_id, str(tool_call_id), now, duration_ms
            )


@_safe
def on_session_end(
    session_id: object = None,
    turn_id: object = None,
    completed: object = None,
    failed: object = None,
    interrupted: object = None,
    turn_exit_reason: object = None,
    **kwargs: object,
) -> None:
    now = time.time()
    home = _home()
    if turn_id and session_id:
        with _lock:
            started = _turn_start.pop(str(turn_id), None)
        store.writer(home).put(
            "insert or replace into turn_outcome(turn_id, session_id, started_at, ended_at,"
            " exit_reason, completed, interrupted, failed) values (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(turn_id),
                session_id,
                started or now,
                now,
                turn_exit_reason,
                None if completed is None else int(bool(completed)),
                None if interrupted is None else int(bool(interrupted)),
                None if failed is None else int(bool(failed)),
            ),
        )
    if after_turn is not None:
        after_turn(home)


@_safe
def on_skill_lifecycle(
    action: object = None,
    skill_name: object = None,
    provenance: object = None,
    session_id: object = None,
    **kwargs: object,
) -> None:
    skill = _skill_key(skill_name)
    if not (skill and provenance and session_id):
        return
    _remember(_provenance, (session_id, skill), provenance)
    # lifecycle fired after the skill_manage post_tool_call: fill the provenance of that fresh row
    store.writer(_home()).put(
        "update skill_edit set provenance = ? where session_id = ? and skill_name = ?"
        " and provenance is null and origin = 'hook' and ts >= ?",
        (provenance, session_id, skill, time.time() - 120),
    )


@_safe
def on_session_delete(
    session_id: object = None, deleted_ids: object = None, reason: object = None, **kwargs: object
) -> None:
    if reason == "archive":  # archived sessions keep their messages in state.db: keep their rows
        return
    ids = [s for s in _ids(deleted_ids, session_id) if s]
    if not ids:
        return
    ph = ",".join("?" * len(ids))
    w = store.writer(_home())
    for table in ("tool_outcome", "turn_outcome", "turn_intent", "run"):
        w.put(f"delete from {table} where session_id in ({ph})", tuple(ids))
    # the ledger entry and the skill change outlive the session: keep the edit and its review,
    # unlinked, as backfill keeps an edit whose session is gone
    w.put(
        "update skill_edit set session_id = null, turn_id = null, tool_call_id = null"
        f" where session_id in ({ph})",
        tuple(ids),
    )


# ---------------------------------------------------------------- skill_manage -> ledger entry
def _ledger_tail(home: Path) -> list[hermes_data.LedgerEntry]:
    path = hermes_data.ledger_path(home)
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - LEDGER_TAIL))
        data = f.read().decode("utf-8", "replace")
    lines = data.splitlines()
    if size > LEDGER_TAIL:
        lines = lines[1:]  # first line may be cut
    return [e for e in map(hermes_data.parse_ledger_line, lines) if e is not None]


def find_ledger_entries(
    home: Path, skill: str, at: float, duration_ms: object = None, session_id: object = None
) -> list[hermes_data.LedgerEntry]:
    """Ledger entries of `skill` written during the tool call that ended at `at` (one per operation
    of a batch), in ledger order; `duration_ms` (the hook's, untyped) widens the window when it is a
    number. Entries whose evidence names another session are left out."""
    lo = at - LEDGER_SLACK - (as_number(duration_ms) or 0) / 1000.0
    out: list[hermes_data.LedgerEntry] = []
    for e in _ledger_tail(home):
        if _skill_key(e.get("skill")) != skill or not lo <= e["ts_epoch"] <= at + LEDGER_SLACK:
            continue
        sid = (e.get("evidence") or {}).get("session_id")
        if sid and session_id and sid != session_id:
            continue
        out.append(e)
    return out


def _link_skill_edit(
    home: Path,
    skill: str,
    session_id: object,
    turn_id: object,
    tool_call_id: str,
    at: float,
    duration_ms: object,
) -> None:
    with _lock:
        prov = _provenance.get((session_id, skill))

    def link(conn: sqlite3.Connection) -> None:
        # an entry another tool call's hook already linked stays with that call
        for e in find_ledger_entries(home, skill, at, duration_ms, session_id):
            conn.execute(
                "insert into skill_edit(ledger_entry_id, ts, skill_name, provenance, session_id,"
                " turn_id, tool_call_id, origin) values (?, ?, ?, ?, ?, ?, ?, 'hook')"
                " on conflict(ledger_entry_id) do update set"
                " provenance = coalesce(excluded.provenance, skill_edit.provenance),"
                " session_id = excluded.session_id, turn_id = excluded.turn_id,"
                " tool_call_id = excluded.tool_call_id, origin = 'hook'"
                " where skill_edit.origin != 'hook'"
                " or skill_edit.tool_call_id = excluded.tool_call_id",
                (
                    e["id"],
                    e["ts_epoch"],
                    skill,
                    prov,
                    session_id,
                    turn_id and str(turn_id),
                    tool_call_id,
                ),
            )

    store.writer(home).call(link)
