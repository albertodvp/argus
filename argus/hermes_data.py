"""Read-only access to Hermes' own data: state.db, the curator ledger and its blob store.

Neither is a documented Hermes API: `open_state` checks the columns this plugin reads and raises
HermesFormatError naming what is missing; `load_dump` validates every row it reads against the
column types of Hermes' schema (TEXT -> str, REAL -> float, INTEGER -> int, nullable ones `| None`)
and raises HermesFormatError for a row that does not fit. A ledger line that is not an entry of the
shape below is skipped, like an unreadable one. Nothing here ever writes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final, NotRequired, TypeAlias, TypeVar

from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypedDict

from . import signals, store
from .dbtypes import SQLITE_ROW, iter_rows, query
from .jsontypes import loads

T = TypeVar("T")

SESSION_COLUMNS: Final = (
    "id",
    "source",
    "title",
    "parent_session_id",
    "started_at",
    "ended_at",
    "end_reason",
    "message_count",
    "tool_call_count",
    "profile_name",
    "model",
)
SESSION_OPTIONAL: Final = ("input_tokens", "output_tokens")
MESSAGE_COLUMNS: Final = (
    "id",
    "session_id",
    "role",
    "content",
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "timestamp",
    "finish_reason",
)
BLOB_MAX: Final = 400_000


class HermesFormatError(Exception):
    """state.db or the ledger no longer has the shape this plugin reads."""


# ---------------------------------------------------------------- shapes
class SessionRow(TypedDict):
    """One state.db `sessions` row: SESSION_COLUMNS, then the SESSION_OPTIONAL ones present."""

    id: str
    source: str
    title: str | None
    parent_session_id: str | None
    started_at: float
    ended_at: float | None
    end_reason: str | None
    message_count: int | None
    tool_call_count: int | None
    profile_name: str | None
    model: str | None
    input_tokens: NotRequired[int | None]
    output_tokens: NotRequired[int | None]


class MessageRow(TypedDict):
    """One state.db `messages` row (MESSAGE_COLUMNS); a BLOB content or tool_calls reads as None."""

    id: int
    session_id: str
    role: str
    content: str | None
    tool_call_id: str | None
    tool_calls: str | None
    tool_name: str | None
    timestamp: float
    finish_reason: str | None


class LedgerFile(TypedDict, total=False):
    """One file of a ledger entry's `before`/`after` side; `sha256` names its blob."""

    path: str
    sha256: str


class LedgerEvidence(TypedDict, total=False):
    """The `evidence` keys Argus reads (others are dropped)."""

    session_id: str | None
    rollback_target: str | None


class LedgerLine(TypedDict):
    """One line of the curator ledger (keys Argus does not read are dropped)."""

    id: str
    ts: str
    actor: NotRequired[str | None]
    action: NotRequired[str | None]
    skill: NotRequired[str | None]
    evidence: NotRequired[LedgerEvidence | None]
    before: NotRequired[list[LedgerFile] | None]
    after: NotRequired[list[LedgerFile] | None]


class LedgerEntry(LedgerLine):
    """A ledger line with its `ts` as epoch seconds; `skill` is the skill's directory name, and
    `qualified` the name skill_manage was given (`cat/sk` for a category path, else as `skill`)."""

    ts_epoch: float
    qualified: NotRequired[str | None]


class Dump(TypedDict):
    """One profile's history: sessions, their messages, ledger entries and the .md blobs."""

    profile: str
    home: str
    sessions: list[SessionRow]
    messages: list[MessageRow]
    ledger: list[LedgerEntry]
    blobs: dict[str, str]


ToolCall: TypeAlias = tuple[str, str | None, str | None, str | None]
"""One tool call of a turn: (tool_name, error_sig, excerpt of a failed result, tool_call_id)."""

SkillRead: TypeAlias = tuple[str, float, str]
"""(session id, timestamp, skill name) of one skill_view call."""


class Turn(TypedDict):
    """One human or cron turn (see build_turns); `i` is its position, set last."""

    profile: str
    sid: str
    msg_id: int
    ts: float
    text: str
    view: str
    source: str
    cron: str
    reply: str
    tools: int
    calls: list[ToolCall]
    runtime: list[str]
    end: float
    i: NotRequired[int]


class Session(TypedDict):
    """One session as build_turns sees it; `start`/`end` are its first/last message times."""

    id: str
    profile: str
    source: str
    title: str | None
    parent: str | None
    start: float
    end: float
    tools: int | None
    msgs: int | None


SESSION_ROW: Final = TypeAdapter(SessionRow)
MESSAGE_ROW: Final = TypeAdapter(MessageRow)
LEDGER_LINE: Final = TypeAdapter(LedgerLine)


def state_columns(conn: sqlite3.Connection) -> dict[str, list[str]]:
    return {
        t: [str(r["name"]) for r in query(conn, f"pragma table_info({t})")]
        for t in ("sessions", "messages")
    }


def open_state(home: Path) -> sqlite3.Connection:
    """Open the profile's state.db read-only and check the columns this plugin reads."""
    path = Path(home) / "state.db"
    if not path.exists():
        raise HermesFormatError(f"no state.db at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    conn.row_factory = SQLITE_ROW
    cols = state_columns(conn)
    missing = [f"sessions.{c}" for c in SESSION_COLUMNS if c not in cols["sessions"]] + [
        f"messages.{c}" for c in MESSAGE_COLUMNS if c not in cols["messages"]
    ]
    if missing:
        conn.close()
        raise HermesFormatError(
            f"{path}: unsupported Hermes state.db, missing columns: {', '.join(missing)}"
        )
    return conn


def sessions_with_messages(home: Path, ids: Collection[str]) -> set[str]:
    """Those of `ids` with at least one message in the profile's state.db, at any time (the
    history window aside)."""
    out: set[str] = set()
    if not ids:
        return out
    wanted = sorted(ids)
    conn = open_state(home)
    try:
        for i in range(0, len(wanted), 500):
            chunk = wanted[i : i + 500]
            for r in iter_rows(
                conn,
                "select distinct session_id from messages"
                f" where session_id in ({','.join('?' * len(chunk))})",
                chunk,
            ):
                sid = r["session_id"]
                if isinstance(sid, str):
                    out.add(sid)
    finally:
        conn.close()
    return out


# ---------------------------------------------------------------- ledger and blobs
def ledger_path(home: Path) -> Path:
    return Path(home) / "skills" / ".curator_ledger.jsonl"


def blob_dirs(home: Path) -> list[Path]:
    """The profile's blob store, then the install root's (the root profile keeps blobs there)."""
    out: list[Path] = []
    for d in (
        Path(home) / ".curator_backups" / "blobs",
        store.root_of(home) / ".curator_backups" / "blobs",
    ):
        if d not in out and d.is_dir():
            out.append(d)
    return out


def read_blob(dirs: list[Path], sha: str) -> str | None:
    if not re.fullmatch(r"[0-9A-Za-z_-]+", sha):  # a blob name, never a path out of the store
        return None
    for d in dirs:
        p = d / sha
        if p.is_file():
            data = p.read_bytes()
            return data.decode("utf-8", "replace") if len(data) < BLOB_MAX else None
    return None


def parse_ledger_line(line: str) -> LedgerEntry | None:
    """One ledger entry with `ts_epoch` added, or None when unreadable."""
    try:
        raw: object = json.loads(line)
        e = LEDGER_LINE.validate_python(raw, strict=True)
        ts_epoch = datetime.fromisoformat(e["ts"]).timestamp()
    except Exception:
        return None
    entry: LedgerEntry = {**e, "ts_epoch": ts_epoch, "qualified": e.get("skill")}
    if (skill := e.get("skill")) and "/" in skill:
        # skill_manage called with a category path (`cat/sk`, as Argus' apply does): the skill is
        # its directory name everywhere else
        entry["skill"] = skill.rstrip("/").rsplit("/", 1)[-1]
    return entry if entry["id"] else None


def changed_md(
    entry: LedgerLine, blobs: Mapping[str, str]
) -> dict[str, tuple[str | None, str | None]]:
    """The .md files a ledger entry changed: path -> (text before, text after) from the blob store
    (None: no blob); files whose two texts are both missing or equal are left out."""
    files: dict[str, dict[str, str | None]] = {}
    for side, items in (("before", entry.get("before")), ("after", entry.get("after"))):
        for f in items or []:
            if f.get("path", "").endswith(".md"):
                sha = f.get("sha256")
                files.setdefault(f["path"], {})[side] = blobs.get(sha) if sha is not None else None
    return {
        path: (d.get("before"), d.get("after"))
        for path, d in files.items()
        if not (d.get("before") is None and d.get("after") is None)
        and d.get("before") != d.get("after")
    }


def read_ledger(home: Path, since: float | None = None) -> list[LedgerEntry]:
    """Ledger entries (with `ts_epoch`) at or after `since`, in file order."""
    path = ledger_path(home)
    if not path.exists():
        return []
    out: list[LedgerEntry] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            e = parse_ledger_line(line)
            if e is not None and (since is None or e["ts_epoch"] >= since):
                out.append(e)
    return out


# ---------------------------------------------------------------- dump
def _validated(adapter: TypeAdapter[T], row: Mapping[str, object], what: str, home: Path) -> T:
    try:
        return adapter.validate_python(row, strict=True)
    except ValidationError as e:
        raise HermesFormatError(f"{home}: unsupported Hermes state.db {what}: {e}") from e


def load_dump(home: Path, since: float | None = None) -> Dump:
    """The history from `since` on (the profile's window; None or 0: all of it): sessions started
    at or after `since` or with a message at or after it, their messages from `since` on (full
    content), ledger entries in the window and the .md blobs they reference. Same shape as one
    element of pipeline/extract_remote.py dump."""
    home = Path(home)
    since = since or 0.0
    conn = open_state(home)
    try:
        have = state_columns(conn)["sessions"]
        scols = list(SESSION_COLUMNS) + [c for c in SESSION_OPTIONAL if c in have]
        # activity, not start: a resumed or long-running session stays in the window. One index
        # seek per session (messages(session_id, timestamp)); messages has no timestamp index
        active = (
            " or exists (select 1 from messages m where m.session_id = sessions.id"
            " and m.timestamp >= ?)"
            if since
            else ""
        )
        ss = [
            _validated(SESSION_ROW, dict(r), "sessions row", home)
            for r in iter_rows(
                conn,
                f"select {','.join(scols)} from sessions where started_at >= ?{active}"
                " order by started_at",
                (since, since) if since else (since,),
            )
        ]
        ids = [s["id"] for s in ss]
        ms: list[MessageRow] = []
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            q = (
                f"select {','.join(MESSAGE_COLUMNS)} from messages"
                f" where session_id in ({','.join('?' * len(chunk))})"
                + (" and timestamp >= ?" if since else "")
                + " order by session_id, id"
            )
            for r in iter_rows(conn, q, [*chunk, since] if since else chunk):
                m: dict[str, object] = dict(r)
                for k in ("content", "tool_calls"):
                    if isinstance(m[k], bytes):
                        m[k] = None
                ms.append(_validated(MESSAGE_ROW, m, "messages row", home))
    finally:
        conn.close()
    entries = read_ledger(home, since)
    dirs = blob_dirs(home)
    blobs: dict[str, str] = {}
    for e in entries:
        for side in (e.get("before"), e.get("after")):
            for it in side or []:
                h = it.get("sha256")
                if h and h not in blobs and (it.get("path") or "").endswith(".md"):
                    text = read_blob(dirs, h)
                    if text is not None:
                        blobs[h] = text
    return {
        "profile": store.profile_name(home),
        "home": str(home),
        "sessions": ss,
        "messages": ms,
        "ledger": entries,
        "blobs": blobs,
    }


# ---------------------------------------------------------------- turns
def build_turns(dumps: Sequence[Dump]) -> tuple[list[Turn], dict[str, Session], list[Session]]:
    """Flatten dumped profiles into ordered human/cron turns, sessions and child (subagent)
    sessions.

    Each turn: profile, sid, msg_id (state.db id of its user message), ts, end, text, view, source,
    cron, reply, tools, calls ((tool_name, error_sig|None, excerpt|None, tool_call_id)), runtime,
    i."""
    turns: list[Turn] = []
    sessions: dict[str, Session] = {}
    children: list[Session] = []
    for p in dumps:
        pname = p["profile"]
        by: dict[str, list[MessageRow]] = {}
        for m in p["messages"]:
            by.setdefault(m["session_id"], []).append(m)
        for s in p["sessions"]:
            ms = by.get(s["id"], [])
            sess: Session = {
                "id": s["id"],
                "profile": pname,
                "source": s["source"],
                "title": s["title"],
                "parent": s["parent_session_id"],
                "start": ms[0]["timestamp"] if ms else s["started_at"],
                "end": ms[-1]["timestamp"] if ms else s["started_at"],
                "tools": s["tool_call_count"],
                "msgs": s["message_count"],
            }
            sessions[s["id"]] = sess
            if s["source"] == "subagent" or (s["title"] or "").startswith("Subagent:"):
                children.append(sess)
                continue
            # per-turn reply and tool count
            cur: Turn | None = None
            prev_clean: str | None = None
            for m in ms:
                content = m["content"]
                if m["role"] == "user" and content:
                    text, view, hint = signals.clean_text(content)
                    if text is None:
                        continue
                    if hint == "cron":
                        text = "Cron job: " + signals.cron_key(s["title"])
                    if prev_clean == text:
                        continue
                    prev_clean = text
                    cur = {
                        "profile": pname,
                        "sid": s["id"],
                        "msg_id": m["id"],
                        "ts": m["timestamp"],
                        "text": text[:1200],
                        "view": view[:200],
                        "source": s["source"],
                        "cron": signals.cron_key(s["title"]) if hint == "cron" else "",
                        "reply": "",
                        "tools": 0,
                        "calls": [],
                        "runtime": [],
                        "end": m["timestamp"],
                    }
                    turns.append(cur)
                elif cur is not None and cur["sid"] == s["id"]:
                    cur["end"] = m["timestamp"]
                    if m["role"] == "assistant":
                        if content:
                            cur["reply"] = content[:2500]
                            sig = signals.runtime_signal(content)
                            if sig and sig not in cur["runtime"]:
                                cur["runtime"].append(sig)
                        tool_calls = m["tool_calls"]
                        if tool_calls:
                            try:
                                calls = loads(tool_calls)
                                if isinstance(calls, (list, dict, str)):
                                    cur["tools"] += len(calls)
                            except Exception:
                                pass
                    elif m["role"] == "tool":
                        err = signals.tool_error(content)
                        cur["calls"].append(
                            (
                                m["tool_name"] or "?",
                                err,
                                (content or "")[:300] if err else None,
                                m["tool_call_id"],
                            )
                        )
    turns.sort(key=lambda t: t["ts"])
    for t in turns:
        if not t["reply"].strip():
            t["runtime"].append("loop: no final reply")
    # mirrored sessions repeat the same turn: drop exact duplicates by (profile, text, ~time)
    out: list[Turn] = []
    seen: set[tuple[str, str, int]] = set()
    for t in turns:
        k = (t["profile"], t["text"][:300], round(t["ts"] / 10))
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    for i, t in enumerate(out):
        t["i"] = i
    return out, sessions, children


def skill_reads(dumps: Sequence[Dump]) -> list[SkillRead]:
    """Explicit skill reads: (session id, timestamp, skill name) for every skill_view call.
    Skills injected into the prompt without a skill_view call are not visible here."""
    out: list[SkillRead] = []
    for p in dumps:
        for m in p["messages"]:
            tool_calls = m["tool_calls"]
            if m["role"] != "assistant" or not tool_calls:
                continue
            try:
                calls = loads(tool_calls)
            except ValueError:
                continue
            for c in calls if isinstance(calls, list) else []:
                f = c.get("function") if isinstance(c, dict) else None
                if not isinstance(f, dict) or f.get("name") != "skill_view":
                    continue
                raw = f.get("arguments")
                try:
                    args = loads(raw) if isinstance(raw, str) else None
                except ValueError:
                    args = None
                name = args.get("name") if isinstance(args, dict) else None
                if isinstance(name, str) and name:
                    out.append((m["session_id"], m["timestamp"], name.split(":")[-1]))
    return out
