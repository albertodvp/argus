"""Backfill layer 1 from history, read-only: tool_outcome from state.db tool messages, skill_edit
from the curator ledger. Idempotent (insert or ignore: rows written by hooks are richer and win);
the cursor in meta narrows the next run to what is new.

Backfill also keeps the profile's history window (meta `window_start`, see store.WINDOW), which
derive and export read from. An explicit --since DAYS sets it the first time (also on a profile
backfilled before without one) and widens it when a later --since asks for more history (`--since
0`: the whole history); a request for less is reported and not applied. The since_days setting
(used without --since) only sets the window of a profile's first backfill. Without a request the
window stays as it is. Each profile's log ends with the model calls the next derive will make
(derive.estimate), and a warning when they are many."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, Literal, NotRequired, TypeAlias

from pydantic import TypeAdapter
from typing_extensions import TypedDict

from . import derive, hermes_data, signals, store
from .dbtypes import iter_rows, query_one

CURSOR: Final = store.BACKFILL_CURSOR
# re-read the last hour: messages and ledger lines can land slightly out of order
OVERLAP: Final = 3600.0
DAY: Final = 86400.0


class BackfillCursor(TypedDict, total=False):
    """meta `backfill_cursor`: the newest message and ledger times read, and when."""

    messages_ts: int | float | None
    ledger_ts: int | float | None
    at: int | float | None


class BackfillDone(TypedDict):
    since: float
    tool_outcome: int
    skill_edit: int
    cursor: BackfillCursor
    # the profile's history window after this run (0 = the whole history, requested explicitly);
    # absent while no window was ever requested
    window_start: NotRequired[float]


class BackfillFailed(TypedDict):
    error: str


ProfileReport: TypeAlias = BackfillDone | BackfillFailed

BACKFILL_CURSOR: Final = TypeAdapter(BackfillCursor)


def _cursor(conn: sqlite3.Connection) -> BackfillCursor:
    try:
        raw: object = json.loads(store.meta_get(conn, CURSOR) or "{}")
    except ValueError:
        return {}
    return BACKFILL_CURSOR.validate_python(raw, strict=True)


def _since(cur: BackfillCursor, key: Literal["messages_ts", "ledger_ts"], start: float) -> float:
    """Start of the reads for one source: the window start, narrowed by the cursor of the last
    run."""
    mark = cur.get(key) or cur.get("at") or 0.0
    return max(start, mark - OVERLAP) if mark else start


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def window(
    have: float | None,
    since_days: float | None,
    now: float,
    *,
    explicit: bool = True,
    fresh: bool = True,
) -> tuple[float | None, bool, str | None]:
    """The profile's window after a request: (window start, whether backfill must read from that
    start ignoring its cursor, note for the log).

    `have` is the stored window (None: never set; 0: the whole history). `since_days` None asks
    nothing (the window stays), 0 the whole history, DAYS > 0 the last DAYS days from `now`.
    `explicit`: the request is --since; else it is the since_days setting, which only applies to a
    `fresh` profile (never backfilled). An unset window takes the request; a set window only
    widens: an explicit request for less history (a later start) is reported in the note, never
    applied."""
    if since_days is None or not (explicit or (fresh and have is None)):
        return have, False, None
    if since_days < 0:
        raise ValueError(f"since_days must be 0 (all) or a positive number of days: {since_days}")
    want = now - since_days * DAY if since_days else 0.0
    if have is None or want < have:
        # the cursor of an earlier backfill covered a later start: read the window in full
        return want, True, None
    if want > have:  # only an explicit request gets here with a window set
        kept = f"window kept from {_when(have)}" if have else "whole history kept"
        return have, False, f"{kept} (--since {since_days:g} would narrow it)"
    return have, False, None


def _tool_outcomes(
    state: sqlite3.Connection, conn: sqlite3.Connection, since: float
) -> tuple[int, float]:
    n, last = 0, 0.0
    rows = iter_rows(
        state,
        "select session_id, tool_call_id, tool_name, content, timestamp from messages"
        " where role = 'tool' and tool_call_id is not null and timestamp >= ? order by timestamp",
        (since,),
    )
    for r in rows:
        content = r["content"] if isinstance(r["content"], str) else None
        sig = signals.tool_error(content)
        cur = conn.execute(
            "insert or ignore into tool_outcome(tool_call_id, session_id, ts, tool_name, status,"
            " origin, error_sig, error_category, derived_by)"
            " values (?, ?, ?, ?, ?, 'backfill', ?, ?, ?)",
            (
                r["tool_call_id"],
                r["session_id"],
                r["timestamp"],
                r["tool_name"] or "?",
                "error" if sig else "ok",
                sig,
                signals.err_category(sig) if sig else None,
                "rules/v1",
            ),
        )
        n += cur.rowcount
        ts = r["timestamp"]
        if isinstance(ts, (str, bytes)):
            raise TypeError(f"'>' not supported between instances of '{type(ts).__name__}'")
        last = max(last, ts or 0.0)
    return n, last


def _skill_edits(
    state: sqlite3.Connection, conn: sqlite3.Connection, home: Path, since: float
) -> tuple[int, float]:
    """skill_edit rows from the ledger; turn_id = str(state.db id of the last user message before
    the edit). A session no longer in state.db (deleted) is not linked: the edit is kept, its
    session is not."""
    n, last = 0, 0.0
    for e in hermes_data.read_ledger(home, since):
        last = max(last, e["ts_epoch"])
        skill = e.get("skill")
        if not skill:
            continue
        sid = (e.get("evidence") or {}).get("session_id") or None
        if sid and query_one(state, "select 1 from sessions where id = ?", (sid,)) is None:
            sid = None
        turn = None
        if sid:
            r = query_one(
                state,
                "select id from messages where session_id = ? and role = 'user' and timestamp <= ?"
                " order by timestamp desc, id desc limit 1",
                (sid, e["ts_epoch"]),
            )
            turn = str(r["id"]) if r else None
        cur = conn.execute(
            "insert or ignore into skill_edit(ledger_entry_id, ts, skill_name, session_id, turn_id,"
            " origin) values (?, ?, ?, ?, ?, 'backfill')",
            (e["id"], e["ts_epoch"], skill, sid, turn),
        )
        n += cur.rowcount
    return n, last


def run(
    root: Path,
    profiles: Sequence[str] | None = None,
    since_days: float | None = None,
    log: Callable[[str], object] = print,
    default_days: float | None = None,
    estimate: bool = True,
) -> dict[str, ProfileReport]:
    """Backfill every profile under `root` (or the named ones). Returns a per-profile report.

    `since_days` is an explicit window request (--since, see `window`): None asks nothing, 0 the
    whole history, DAYS > 0 the last DAYS days. Without it, `default_days` (the since_days setting)
    is the request, as a default. Reads start at the window (the whole history when unset),
    narrowed by the cursor unless the window was just set or widened. `estimate`: log the next
    derive's calls (derive's own catch-up skips it)."""
    report: dict[str, ProfileReport] = {}
    for name, home in store.profile_homes(Path(root)).items():
        if profiles and name not in profiles:
            continue
        try:
            state = hermes_data.open_state(home)
        except hermes_data.HermesFormatError as e:
            log(f"{name}: skipped: {e}")
            report[name] = {"error": str(e)}
            continue
        conn = store.open_db(home)
        changed: Literal["set", "widened"] | None = None  # what this run did to the window
        try:
            prev = _cursor(conn)
            have = store.window_start(conn)
            explicit = since_days is not None
            start, reread, note = window(
                have,
                since_days if explicit else default_days,
                time.time(),
                explicit=explicit,
                fresh=store.meta_get(conn, CURSOR) is None,
            )
            read_from = prev if not reread else BackfillCursor()
            msince = _since(read_from, "messages_ts", start or 0.0)
            lsince = _since(read_from, "ledger_ts", start or 0.0)
            tools, mts = _tool_outcomes(state, conn, msince)
            edits, lts = _skill_edits(state, conn, home, lsince)
            conn.commit()
            cursor: BackfillCursor = {
                "messages_ts": max(mts, prev.get("messages_ts") or 0.0),
                "ledger_ts": max(lts, prev.get("ledger_ts") or 0.0),
                "at": time.time(),
            }
            store.meta_set(conn, CURSOR, json.dumps(cursor))
            store.meta_set(conn, "state_schema", json.dumps(hermes_data.state_columns(state)))
            done: BackfillDone = {
                "since": min(msince, lsince),
                "tool_outcome": tools,
                "skill_edit": edits,
                "cursor": cursor,
            }
            span = ""
            if start is not None:
                if start != have:
                    store.meta_set(conn, store.WINDOW, repr(start))
                done["window_start"] = start
                span = f", window {'from ' + _when(start) if start else 'whole history'}"
            report[name] = done
            log(
                f"{name}: +{tools} tool outcomes, +{edits} skill edits"
                f" (since {time.strftime('%Y-%m-%d %H:%M', time.gmtime(min(msince, lsince)))} UTC"
                f"{span})"
            )
            if note:
                log(f"{name}: {note}")
            if start is not None and start != have:
                changed = "set" if have is None else "widened"
        finally:
            conn.close()
            state.close()
        if not estimate:
            continue
        try:
            est = derive.estimate(home)
        except hermes_data.HermesFormatError as e:
            log(f"{name}: next derive: no estimate: {e}")
            continue
        for line in derive.estimate_lines(est, changed) if est is not None else ():
            log(f"{name}: {line}")
    return report
