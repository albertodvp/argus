"""The `pre_tool_call` hook: approved checks of accepted rules, evaluated on every tool call
(docs/enforce.md, sections 5-8).

Hot path: no model call, no network. Per call one `stat` of the plugin DB, a lock, and `pragma
data_version` on a long-lived read-only connection; the approvals are read again only when another
connection committed since. Events go to the profile's asynchronous store.Writer.

The hook never raises: Hermes blocks a call whose `pre_tool_call` callback raised, so a raising
Argus would block every tool call. Every failure maps to a decision (section 8).
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, TypeAlias

from pydantic import TypeAdapter, ValidationError

from . import checks, checkspec, hermes_internals, store
from .dbtypes import SqlValue, query, scalar

log = logging.getLogger("hermes.plugins.argus")

Directive: TypeAlias = dict[str, str]
"""`{"action": "block", "message": ...}`; None lets the call run."""

READ_TIMEOUT: Final = 2.0  # seconds a read of the approvals waits for a lock (Hermes allows 30)
WARN_EVERY: Final = 60.0
CHECKS_SCHEMA: Final = 3  # the store.SCHEMA_VERSION that added the check tables
EMPTY_ARGS: Final[Mapping[str, object]] = {}
_DICT: Final = TypeAdapter(dict[object, object])


@dataclass(frozen=True)
class Active:
    """One approval in force (not revoked, its rule still accepted)."""

    approval_id: int
    proposal_id: str
    version: int
    tool: str
    skill: str
    rule: str
    mode: str  # observe | enforce
    spec: checkspec.Spec | None  # None: the text does not parse (spec_error says why)
    spec_error: str
    sha_ok: bool


@dataclass(frozen=True)
class Snapshot:
    """The approvals in force of one profile, by tool."""

    by_tool: dict[str, list[Active]] = field(default_factory=dict)

    def enforced(self, tool: str) -> list[Active]:
        """The checks in force on `tool` in enforce mode whose text has its approved hash."""
        return [a for a in self.by_tool.get(tool, []) if a.mode == "enforce" and a.sha_ok]

    def enforced_tools(self) -> dict[str, list[str]]:
        """tool -> rules with a verified enforced check on it (what the sidecar should list)."""
        out = {t: [a.proposal_id for a in self.enforced(t)] for t in self.by_tool}
        return {t: pids for t, pids in out.items() if pids}


EMPTY: Final = Snapshot()


class UnreadableError(Exception):
    """The plugin DB exists but its approvals cannot be read (the cause is the message)."""


class _Home:
    """The cached view of one profile's plugin DB."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.conn: sqlite3.Connection | None = None
        self.inode: tuple[int, int] | None = None
        self.data_version: SqlValue = None
        self.snapshot: Snapshot | None = None
        self.sidecar_synced = True  # False: the last sidecar rewrite failed, retry on each call


_homes: dict[str, _Home] = {}
_homes_lock = threading.Lock()
_unverified_reported: set[tuple[str, int]] = set()
_last_warning: dict[str, float] = {}


def _warn(key: str, msg: str, *args: object) -> None:
    """A warning at most once a minute per `key` (a broken DB fails on every call)."""
    now = time.monotonic()
    if now - _last_warning.get(key, -WARN_EVERY) >= WARN_EVERY:
        _last_warning[key] = now
        log.warning(msg, *args)


def _home(home: Path) -> _Home:
    with _homes_lock:
        return _homes.setdefault(str(home), _Home())


# ---------------------------------------------------------------- reading the approvals
def _active(row: checks.InForceRow) -> Active:
    try:
        spec: checkspec.Spec | None = checkspec.load(row["spec"])
        error = ""
    except checkspec.SpecError as e:
        spec, error = None, str(e)
    return Active(
        approval_id=row["id"],
        proposal_id=row["proposal_id"],
        version=row["version"],
        tool=row["tool"],
        skill=row["skill_name"],
        rule=row["rule"],
        mode=row["mode"],
        spec=spec,
        spec_error=error,
        sha_ok=checks.hash_ok(row),
    )


def read_snapshot(conn: sqlite3.Connection) -> Snapshot:
    """The approvals in force; EMPTY for a DB whose schema predates checks, which only its meta
    `schema_version` proves: a missing table in a DB of the checks schema (or with no readable
    version) is a damaged DB and raises, so the caller falls back as for an unreadable one."""
    try:
        raw = query(conn, checks.IN_FORCE)  # every row read before any is validated
    except sqlite3.OperationalError as e:
        if "no such table" in str(e) and _predates_checks(conn):
            return EMPTY
        raise
    rows = [checks.IN_FORCE_ROW.validate_python(r, strict=True) for r in raw]
    by_tool: dict[str, list[Active]] = {}
    for r in rows:
        if r["mode"] != "revoked" and r["decision"] == "accept":
            a = _active(r)
            # under the tool its hashed text names; the column only for a text it cannot read
            tool = a.spec["tool"] if a.spec is not None else a.tool
            by_tool.setdefault(tool, []).append(a)
    return Snapshot(by_tool)


def _predates_checks(conn: sqlite3.Connection) -> bool:
    """The DB's meta `schema_version` is one from before the check tables (store.py)."""
    stored = store.meta_get(conn, "schema_version")
    return stored is not None and stored.isdigit() and int(stored) < CHECKS_SCHEMA


def _close(h: _Home) -> None:
    if h.conn is not None:
        with contextlib.suppress(sqlite3.Error):
            h.conn.close()
    h.conn, h.inode, h.data_version = None, None, None


def snapshot(home: Path) -> Snapshot:
    """The approvals in force in the profile at `home`, re-read only when the DB changed. Raises
    UnreadableError when the DB exists and cannot be read and this process never read it."""
    path = store.db_path(home)
    h = _home(home)
    try:
        st = path.stat()
    except FileNotFoundError:
        with h.lock:
            _close(h)
            h.snapshot = None
        # deleted while the sidecar still lists enforced checks: as unreadable (section 8)
        if checks.sidecar_path(home).exists():
            raise UnreadableError("the plugin DB is missing") from None
        return EMPTY
    except OSError as e:
        return _stale(h, home, e)
    with h.lock:
        try:
            if h.conn is None or h.inode != (st.st_dev, st.st_ino):
                _close(h)
                h.conn = sqlite3.connect(
                    f"file:{path}?mode=ro", uri=True, timeout=READ_TIMEOUT, check_same_thread=False
                )
                h.inode = (st.st_dev, st.st_ino)
            version = scalar(h.conn, "pragma data_version")
            if h.snapshot is None or version != h.data_version:
                h.snapshot = read_snapshot(h.conn)
                h.data_version = version
                h.sidecar_synced = False
            if not h.sidecar_synced:
                h.sidecar_synced = _sync_sidecar(home, h.snapshot)
            return h.snapshot
        except (sqlite3.Error, ValueError, TypeError) as e:  # a row that does not decode, too
            _close(h)
            return _stale(h, home, e)


def _sync_sidecar(home: Path, snap: Snapshot) -> bool:
    """Make the sidecar list what this read verified, when it lists something else: a check
    unverified (or revoked, suspended) since it was written must not be blocked by the fallback
    of a later process that cannot read the DB. False when the rewrite failed (logged): the
    caller retries on every call until it succeeds."""
    tools = snap.enforced_tools()
    try:
        if not tools and not checks.sidecar_path(home).exists():
            return True  # nothing enforced, nothing listed: no file to create
        if checks.read_sidecar(home) != tools:  # unreadable (None) counts as different
            checks.write_sidecar_tools(home, tools)
    except OSError as e:
        _warn(
            f"sidecar-write:{home}", "argus: cannot update %s in %s (%s)", checks.SIDECAR, home, e
        )
        return False
    return True


def _stale(h: _Home, home: Path, e: Exception) -> Snapshot:
    if h.snapshot is not None:
        _warn(f"stale:{home}", "argus: cannot read checks of %s (%s): using the last read", home, e)
        return h.snapshot
    raise UnreadableError(f"{type(e).__name__}: {e}") from e


# ---------------------------------------------------------------- decisions
def _profile_cmd(home: Path) -> str:
    name = store.profile_name(home)
    return "hermes" if name == "default" else f"hermes -p {name}"


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def block_message(home: Path, a: Active, tool: str, cause: str, detail: str) -> str:
    """What the agent reads instead of the tool's result (docs/enforce.md, section 6)."""
    what = checkspec.describe(a.spec) if a.spec is not None else f"unreadable: {a.spec_error}"
    head = (
        f"Argus blocked this {tool} call: check {a.proposal_id} v{a.version} ({what}) of the"
        f' accepted rule "{_cut(a.rule, 300)}" (skill {a.skill})'
    )
    if cause == "match":
        why = f" matches: {detail}."
    elif cause == "unknown":
        why = (
            f" cannot be verified for this call: {detail}. Argus blocks what it cannot read;"
            " write the call so that it can (an absolute path, no $(...) or backticks, no"
            " script piped into a shell)."
        )
    else:
        why = f" failed while checking this call ({detail}); an enforced check fails closed."
    cmd = _profile_cmd(home)
    return (
        f"{head}{why} A person approved this check in enforce mode; do not try to reach the same"
        f" effect another way. To lift it, a person runs `{cmd} argus checks observe"
        f" {a.proposal_id}` (record only) or `{cmd} argus checks revoke {a.proposal_id}`, or uses"
        " the Argus dashboard tab."
    )


def _record(
    home: Path,
    a: Active,
    tool: str,
    outcome: str,
    cause: str,
    detail: str,
    ids: tuple[object, object],
) -> None:
    session_id, tool_call_id = ids
    try:  # events are evidence, not the decision: a failure to record one changes nothing
        _put_event(home, a, tool, outcome, cause, detail, session_id, tool_call_id)
    except Exception:
        _warn(f"event:{home}", "argus: cannot record a check event in %s", home)


def _put_event(
    home: Path,
    a: Active,
    tool: str,
    outcome: str,
    cause: str,
    detail: str,
    session_id: object,
    tool_call_id: object,
) -> None:
    store.writer(home).put(
        "insert into check_event (approval_id, proposal_id, ts, session_id, tool_call_id,"
        " tool_name, outcome, cause, detail) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            a.approval_id,
            a.proposal_id,
            time.time(),
            None if session_id is None else str(session_id),
            None if tool_call_id is None else str(tool_call_id),
            tool,
            outcome,
            cause,
            _cut(detail, checkspec.SHOWN),
        ),
    )


def _verdict(a: Active, args: Mapping[str, object]) -> tuple[str, str]:
    """(cause, detail) when check `a` holds the call (match | unknown | error); ("", "") when
    it does not."""
    if a.spec is None:
        return "error", f"the approved check text cannot be read: {a.spec_error}"
    try:
        kind, detail = checkspec.evaluate(a.spec, args)
    except Exception as e:  # the check itself failed: the mode decides (section 8)
        return "error", f"{type(e).__name__}: {e}"
    return ("", "") if kind == "no" else ((kind, detail))


def decide(
    home: Path, tool: str, args: Mapping[str, object], ids: tuple[object, object]
) -> Directive | None:
    """Evaluate the checks in force on `tool`: record each that holds the call, block when one of
    them is enforced. Raises UnreadableError (see `snapshot`)."""
    blocks: list[str] = []
    for a in snapshot(home).by_tool.get(tool, []):
        if not a.sha_ok:
            _unverified(home, a, tool, ids)
            continue
        cause, detail = _verdict(a, args)
        if not cause:
            continue
        enforce = a.mode == "enforce"
        _record(home, a, tool, "blocked" if enforce else "would_block", cause, detail, ids)
        if enforce:
            blocks.append(block_message(home, a, tool, cause, detail))
    return {"action": "block", "message": "\n\n".join(blocks)} if blocks else None


def _unverified(home: Path, a: Active, tool: str, ids: tuple[object, object]) -> None:
    key = (str(home), a.approval_id)
    if key in _unverified_reported:
        return
    _unverified_reported.add(key)
    log.warning(
        "argus: check %s v%s does not match its approved sha256: not enforced",
        a.proposal_id,
        a.version,
    )
    _record(home, a, tool, "unverified", "hash", "stored check text differs from its approval", ids)


def _unreadable(home: Path, tool: str, cause: str) -> Directive | None:
    """No approvals were ever read here: block the tools the sidecar lists as enforced."""
    tools = checks.read_sidecar(home)
    if tools is None:
        _warn(
            f"sidecar:{home}",
            "argus: cannot read checks of %s (%s) nor %s: allowing",
            home,
            cause,
            checks.SIDECAR,
        )
        return None
    pids = tools.get(tool)
    if not pids:
        return None
    _warn(
        f"unreadable:{home}",
        "argus: cannot read checks of %s (%s): blocking enforced tools",
        home,
        cause,
    )
    return {
        "action": "block",
        "message": (
            f"Argus cannot read its approved checks ({cause}), and this {tool} call has an"
            f" enforced check (rule {', '.join(pids)}): it is blocked until Argus can read them."
            f" A person can look with `{_profile_cmd(home)} argus checks`."
        ),
    }


def _fallback(home: Path | None, tool: str, e: Exception) -> Directive | None:
    """After an unexpected error: block only when this process knows an enforced check on the
    tool; otherwise allow."""
    _warn("hook", "argus: pre_tool_call failed (%s: %s)", type(e).__name__, e)
    if home is None:
        return None
    snap = _home(home).snapshot
    enforced = snap.enforced(tool) if snap is not None else []
    if not enforced:
        return None
    rules = ", ".join(a.proposal_id for a in enforced)
    return {
        "action": "block",
        "message": (
            f"Argus failed while checking this {tool} call ({type(e).__name__}) and an enforced"
            f" check (rule {rules}) applies to {tool}: the call is blocked (fail closed)."
            f" A person can look with `{_profile_cmd(home)} argus checks`."
        ),
    }


def _args(args: object) -> Mapping[str, object]:
    """Hermes passes a dict; anything else is a call without arguments (every field missing)."""
    try:
        raw = _DICT.validate_python(args, strict=True)  # a copy: another hook may hold the dict
    except ValidationError:
        return EMPTY_ARGS
    return {k: v for k, v in raw.items() if isinstance(k, str)}


def pre_tool_call(
    tool_name: object = None,
    args: object = None,
    session_id: object = None,
    tool_call_id: object = None,
    **kwargs: object,
) -> Directive | None:
    """Hermes `pre_tool_call`: block the call when an enforced check holds it (never raises)."""
    home: Path | None = None
    tool = tool_name if isinstance(tool_name, str) else ""
    try:
        if not tool:
            return None
        home = hermes_internals.hermes_home()
        try:
            return decide(home, tool, _args(args), (session_id, tool_call_id))
        except UnreadableError as e:
            return _unreadable(home, tool, str(e))
    except Exception as e:
        return _fallback(home, tool, e)


def reset() -> None:
    """Forget every cached snapshot and report (tests; a new process starts this way)."""
    with _homes_lock:
        for h in _homes.values():
            with h.lock:
                _close(h)
        _homes.clear()
    _unverified_reported.clear()
    _last_warning.clear()
