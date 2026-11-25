"""Enforced checks of accepted rules, in the plugin DB (docs/enforce.md): which rules to draft and
what the drafting model reads, the drafts it leaves (layer 2), a person's approvals and mode
changes (layer 3), the gate that keeps them a person's, the sidecar the hook falls back to, and a
rule's check as the CLI and the dashboard show it.

The hook (`argus.enforce`) reads the approvals in force with IN_FORCE; the drafting model call is
derive's stage `checks` (derive.draft_checks).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from pydantic import TypeAdapter
from typing_extensions import TypedDict

from . import apply, checkspec, hermes_internals, store
from .dbtypes import (
    CHECK_APPROVAL_ROW,
    CHECK_DRAFT_ROW,
    CHECK_EVENT_ROW,
    PROPOSAL_ROW,
    CheckApprovalRow,
    CheckDraftRow,
    ProposalRow,
    count,
    query,
    query_as,
    query_one,
)
from .exporttypes import (
    CheckApproval,
    CheckEvent,
    CheckMode,
    CheckState,
    DecisionAction,
    DecisionKind,
    RuleCheck,
)
from .jsontypes import as_list, as_object, loads, loads_object

log = logging.getLogger("hermes.plugins.argus")

V_CHECK: Final = "checks/v1"
MARKERS: Final = (
    "HERMES_SESSION_ID",
    "HERMES_SESSION_KEY",
    "HERMES_CRON_SESSION",
    "HERMES_KANBAN_TASK",
    "HERMES_RPC_SOCKET",
)
"""Environment variables Hermes sets in the processes its agent runs: the terminal tool bridges the
session's, cron's and kanban's context into its commands; execute_code scrubs those from its
sandbox but gives it HERMES_RPC_SOCKET (its tool-call channel). A person's own shell has none."""
SIDECAR: Final = "enforced.json"
EVENTS_SHOWN: Final = 10


class ApprovalError(Exception):
    """A person's approval, mode change or decision that Argus refuses (the reason is the
    message)."""


# ---------------------------------------------------------------- rules to draft
def accepted(conn: sqlite3.Connection) -> list[ProposalRow]:
    """Proposals whose latest decision is `accept` (a person's, or the auto_apply policy's)."""
    return query_as(
        conn,
        PROPOSAL_ROW,
        "select p.* from proposal p where (select d.action from decision d where"
        " d.target_kind = 'proposal' and d.target_id = p.id"
        " order by d.decided_at desc, d.id desc limit 1) = 'accept' order by p.id",
    )


def to_draft(conn: sqlite3.Connection) -> list[ProposalRow]:
    """Accepted rules without a draft: one drafting call each (derive stage `checks`)."""
    try:
        drafted = {
            str(r["proposal_id"]) for r in query(conn, "select proposal_id from check_draft")
        }
    except sqlite3.OperationalError:  # a read-only DB this version has not migrated yet
        drafted = set()
    return [p for p in accepted(conn) if p["id"] not in drafted]


def _evidence_ids(p: ProposalRow, key: str) -> list[str]:
    try:
        ev = loads_object(p["evidence"])
    except ValueError:
        return []
    return [i for i in as_list(ev.get(key)) or [] if isinstance(i, str)]


def draft_input(conn: sqlite3.Connection, p: ProposalRow) -> str:
    """What the drafting model reads: the rule, its evidence, and the tools the profile calls."""
    intents = _evidence_ids(p, "intents")
    sigs = _evidence_ids(p, "error_sigs")
    lines = [f"RULE: {p['rule']}", f"WHY: {p['why'] or ''}", f"SKILL: {p['skill_name']}", ""]
    if intents:
        marks = ",".join("?" * len(intents))
        titles = query(conn, f"select title from intent where id in ({marks})", tuple(intents))
        lines += ["INTENTS:", *(f"- {r['title']!s}" for r in titles), ""]
    if sigs:
        marks = ",".join("?" * len(sigs))
        rows = query(
            conn,
            "select tool_name, error_sig, count(*) n from tool_outcome"
            f" where error_sig in ({marks}) group by tool_name, error_sig order by n desc limit 20",
            tuple(sigs),
        )
        lines += ["ERRORS (tool: signature):"]
        lines += [f"- {r['tool_name']!s}: {r['error_sig']!s}" for r in rows] or [
            f"- {s}" for s in sigs
        ]
        lines += [""]
    tools = query(
        conn,
        "select tool_name, count(*) n from tool_outcome group by tool_name"
        " order by n desc limit 40",
    )
    lines.append("TOOLS THIS AGENT CALLS: " + ", ".join(str(r["tool_name"]) for r in tools))
    return "\n".join(lines)


class DraftAnswer(TypedDict, total=False):
    """The drafting model's answer (prompts.CHECK_SCHEMA)."""

    checkable: bool
    reason: str
    tool: str
    field: str
    match: str
    value: str
    under: str
    program: str
    args: list[str]
    flags: list[str]


DRAFT_ANSWER: Final = TypeAdapter(DraftAnswer)


def spec_of_answer(a: DraftAnswer) -> checkspec.Spec:
    """The check of a drafting answer, through the closed parser (SpecError when it is none).
    The schema asks every field of every kind; only the fields of the answer's kind are kept."""
    kind = a.get("match", "")
    raw: dict[str, object] = {"v": checkspec.VERSION, "tool": a.get("tool", ""), "match": kind}
    if kind != "tool":
        raw["field"] = a.get("field", "")
    if kind == "equals":
        raw["value"] = a.get("value", "")
    elif kind == "path":
        raw["under"] = a.get("under", "")
    elif kind == "command":
        raw["program"] = a.get("program", "")
        raw["args"] = a.get("args", [])
        raw["flags"] = a.get("flags", [])
    return checkspec.parse_spec(raw)


def save_draft(
    conn: sqlite3.Connection, pid: str, a: DraftAnswer
) -> Literal["drafted", "uncheckable", "refused"]:
    """Record the draft of rule `pid` from a drafting answer; commits."""
    reason = (a.get("reason") or "").strip()[:1000]
    spec: str | None = None
    outcome: Literal["drafted", "uncheckable", "refused"] = "uncheckable"
    if a.get("checkable"):
        try:
            spec = checkspec.canonical(spec_of_answer(a))
            outcome = "drafted"
        except checkspec.SpecError as e:
            reason, outcome = f"the drafted check does not parse: {e}", "refused"
    conn.execute(
        "insert or replace into check_draft (proposal_id, spec, sha256, reason, drafted_at,"
        " derived_by) values (?, ?, ?, ?, ?, ?)",
        (pid, spec, checkspec.digest(spec) if spec else None, reason, time.time(), V_CHECK),
    )
    conn.commit()
    return outcome


# ---------------------------------------------------------------- approvals
class InForceRow(CheckApprovalRow):
    """The latest approval row of a rule, with the rule's current decision."""

    decision: str | None


IN_FORCE_ROW: Final = TypeAdapter(InForceRow)
IN_FORCE: Final = (
    "select a.*, (select d.action from decision d where d.target_kind = 'proposal'"
    " and d.target_id = a.proposal_id order by d.decided_at desc, d.id desc limit 1) decision"
    " from check_approval a"
    " where a.id = (select max(b.id) from check_approval b where b.proposal_id = a.proposal_id)"
)
"""The approval in force of every rule that has one; the hook keeps the rows that are not revoked
and whose rule is still accepted."""


def hash_ok(row: CheckApprovalRow) -> bool:
    """Whether the stored check text still has the sha256 it was approved with, and the row's
    `tool` is the tool that text names (the hook selects checks by it), also for a text this
    version cannot evaluate (a newer format): its JSON `tool` field must equal the column."""
    if checkspec.digest(row["spec"]) != row["sha256"]:
        return False
    try:
        return checkspec.load(row["spec"])["tool"] == row["tool"]
    except checkspec.SpecError:
        pass
    try:
        named = loads_object(row["spec"]).get("tool")
    except ValueError:
        return False
    return isinstance(named, str) and named == row["tool"]


def draft_of(conn: sqlite3.Connection, pid: str) -> CheckDraftRow | None:
    rows = query_as(
        conn, CHECK_DRAFT_ROW, "select * from check_draft where proposal_id = ?", (pid,)
    )
    return rows[0] if rows else None


def latest_approval(conn: sqlite3.Connection, pid: str) -> CheckApprovalRow | None:
    rows = query_as(
        conn,
        CHECK_APPROVAL_ROW,
        "select * from check_approval where proposal_id = ? order by id desc limit 1",
        (pid,),
    )
    return rows[0] if rows else None


def resolve(conn: sqlite3.Connection, ref: str) -> str:
    """The proposal id a person typed: the id, or a unique prefix of at least 4 characters."""
    ref = ref.strip()
    if query_one(conn, "select id from proposal where id = ?", (ref,)) is not None:
        return ref
    if len(ref) < 4:
        raise ApprovalError("give the rule's proposal id, or at least its first 4 characters")
    ids = [
        str(r["id"])
        for r in query(conn, "select id from proposal where id like ? escape '\\'", (_like(ref),))
    ]
    if len(ids) != 1:
        raise ApprovalError(f"{len(ids)} rules start with {ref!r}: give more of the id")
    return ids[0]


def _like(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _version(conn: sqlite3.Connection, pid: str, sha: str) -> int:
    """The version of check text `sha` for rule `pid`: the one it had, else the next one."""
    same = query_one(
        conn,
        "select version from check_approval where proposal_id = ? and sha256 = ? limit 1",
        (pid, sha),
    )
    if same is not None and isinstance(same["version"], int):
        return same["version"]
    return (
        count(
            conn, "select count(distinct sha256) from check_approval where proposal_id = ?", (pid,)
        )
        + 1
    )


def _insert(
    conn: sqlite3.Connection,
    home: Path,
    row: CheckApprovalRow,
    person: object,
) -> CheckApprovalRow:
    """Write an approval row, the only code that does: the person gate is checked here again
    (_decider), whoever calls, and the row is recorded as decided by that proof's kind. The row
    is inserted, the sidecar written from this connection's view (which holds the row), then the
    transaction commits: an approval is never in force without the sidecar the hook falls back
    to. On any failure nothing changes (rolled back; the sidecar restored, best effort)."""
    checked: CheckApprovalRow = {**row, "decided_by": _decider(person)}
    try:
        cur = conn.execute(
            "insert into check_approval (proposal_id, version, tool, skill_name, rule, spec,"
            " sha256, mode, decided_at, decided_by) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                checked["proposal_id"],
                checked["version"],
                checked["tool"],
                checked["skill_name"],
                checked["rule"],
                checked["spec"],
                checked["sha256"],
                checked["mode"],
                checked["decided_at"],
                checked["decided_by"],
            ),
        )
        write_sidecar(home, conn)
        conn.commit()
    except Exception as e:
        conn.rollback()
        _restore_sidecar(home, conn)
        if isinstance(e, OSError):
            raise ApprovalError(f"cannot write {SIDECAR} ({e}): nothing changed") from e
        raise
    return {**checked, "id": cur.lastrowid or 0}


def _restore_sidecar(home: Path, conn: sqlite3.Connection) -> None:
    """After a failed write: the sidecar as the database stands (best effort; a sidecar listing
    a tool too many only blocks more while the database cannot be read)."""
    try:
        write_sidecar(home, conn)
    except OSError:
        log.warning("argus: cannot restore %s in %s", SIDECAR, home)


_MINT: Final = object()


@dataclass(frozen=True)
class Person:
    """Proof that a person decides, the only thing `approve` and `set_mode` accept as their
    decider. Made only by `at_terminal()` (the CLI's person gate passed) and `on_dashboard()`
    (Hermes authorized the dashboard request, which the proof carries); `approve` and `set_mode`
    check that it was minted here and run the whole gate of its kind again themselves."""

    by: Literal["cli", "dashboard"]
    mint: object = field(repr=False, compare=False)
    request: object = field(default=None, repr=False, compare=False)


def _gate(by: Literal["cli", "dashboard"], request: object) -> None:
    """The person gate of a decider kind, in this process (ApprovalError when it does not pass):
    the CLI's is refusal_here (a TTY, no Hermes session marker here or in a parent); the
    dashboard's is Hermes' authorization of the request (hermes_internals) and no session marker."""
    if by == "dashboard" and not hermes_internals.dashboard_request_authorized(request):
        raise ApprovalError("refused: not a request the Hermes dashboard authenticated")
    if (why := refusal_here() if by == "cli" else agent_session()) is not None:
        raise ApprovalError(f"refused: {why}")


def at_terminal() -> Person:
    """The person at this process' terminal (refusal_here); ApprovalError otherwise."""
    _gate("cli", None)
    return Person("cli", _MINT)


def on_dashboard(request: object) -> Person:
    """The person behind `request` to the Argus dashboard API, once Hermes authorized it as it
    authorizes its own sensitive routes (its session token or its login) and the process runs
    outside any Hermes session; ApprovalError otherwise."""
    _gate("dashboard", request)
    return Person("dashboard", _MINT, request)


def _decider(person: object) -> Literal["cli", "dashboard"]:
    """Who decides, checked at the mutation boundary: a Person minted by at_terminal or
    on_dashboard, whose gate this process still passes (for the dashboard, with the request the
    proof carries: a proof without an authorized request is refused in any process)."""
    if not isinstance(person, Person) or person.mint is not _MINT:
        raise ApprovalError(
            "approvals are a person's: pass checks.at_terminal() or checks.on_dashboard(request)"
        )
    _gate(person.by, person.request)
    return person.by


def approve(
    conn: sqlite3.Connection, home: Path, pid: str, sha: str, person: Person
) -> CheckApprovalRow:
    """A person approves the drafted check of rule `pid` whose sha256 they were shown: a new
    approval row in mode `observe`. Refused when `person` is not a person's proof (_decider), the
    rule is not accepted, has no checkable draft, or the draft is not the one shown."""
    decider = _decider(person)
    if apply.current_decision(conn, "proposal", pid) != "accept":
        raise ApprovalError(f"rule {pid} is not accepted: accept it first")
    draft = draft_of(conn, pid)
    if draft is None:
        raise ApprovalError(f"rule {pid} has no drafted check: `hermes argus checks draft {pid}`")
    if draft["spec"] is None or draft["sha256"] is None:
        raise ApprovalError(f"rule {pid} is not checkable: {draft['reason'] or 'no reason given'}")
    spec = checkspec.load(draft["spec"])
    if (
        checkspec.canonical(spec) != draft["spec"]
        or checkspec.digest(draft["spec"]) != draft["sha256"]
    ):
        raise ApprovalError(f"the draft of rule {pid} is not in canonical form: draft it again")
    if sha.strip().lower() != draft["sha256"]:
        raise ApprovalError(
            f"the draft of rule {pid} changed since it was shown (now {draft['sha256'][:8]})"
        )
    prev = latest_approval(conn, pid)
    # the same text again only after a revoke, or to restore a stored copy changed since
    if (
        prev is not None
        and prev["sha256"] == draft["sha256"]
        and prev["mode"] != "revoked"
        and hash_ok(prev)
    ):
        raise ApprovalError(f"this check of rule {pid} is already approved ({prev['mode']})")
    rows = query_as(conn, PROPOSAL_ROW, "select * from proposal where id = ?", (pid,))
    if not rows:
        raise ApprovalError(f"no rule {pid}")
    p = rows[0]
    row = CheckApprovalRow(
        id=0,
        proposal_id=pid,
        version=_version(conn, pid, draft["sha256"]),
        tool=spec["tool"],
        skill_name=p["skill_name"],
        rule=p["rule"],
        spec=draft["spec"],
        sha256=draft["sha256"],
        mode="observe",
        decided_at=time.time(),
        decided_by=decider,
    )
    return _insert(conn, home, row, person)


def set_mode(
    conn: sqlite3.Connection, home: Path, pid: str, mode: CheckMode, sha: str, person: Person
) -> CheckApprovalRow:
    """A person switches rule `pid`'s approved check (sha256 `sha`, as shown) to `mode`; refused
    when `person` is not a person's proof (_decider)."""
    decider = _decider(person)
    prev = latest_approval(conn, pid)
    if prev is None:
        raise ApprovalError(f"rule {pid} has no approved check: approve it first")
    if sha.strip().lower() != prev["sha256"]:
        raise ApprovalError(f"the check of rule {pid} changed since it was shown")
    if prev["mode"] == mode:
        raise ApprovalError(f"the check of rule {pid} is already {mode}")
    if prev["mode"] == "revoked":
        raise ApprovalError(f"the check of rule {pid} was revoked: approve it again")
    if mode == "enforce":
        if apply.current_decision(conn, "proposal", pid) != "accept":
            raise ApprovalError(f"rule {pid} is no longer accepted: its check stays suspended")
        if not hash_ok(prev):
            raise ApprovalError(
                f"the stored check of rule {pid} does not match its sha256: approve it again"
            )
        checkspec.load(prev["spec"])
    row: CheckApprovalRow = {**prev, "mode": mode, "decided_at": time.time(), "decided_by": decider}
    return _insert(conn, home, row, person)


def record_decision(
    conn: sqlite3.Connection,
    kind: DecisionKind,
    tid: str,
    action: DecisionAction | Literal["clear"],
    snapshot: str | None,
    person: Person,
) -> int:
    """A person's decision on a proposal or a skill edit (layer 3), or with action `clear` the
    undo of every decision on that target; the rows written or deleted. A check is in force only
    while its rule's latest decision is `accept` (section 4), so a decision turns a check on or
    off: like `approve` and `set_mode` it takes a person's proof and checks it here (_decider),
    whoever calls, and records the proof's kind as `decided_by`."""
    decider = _decider(person)
    if action == "clear":
        n = conn.execute(
            "delete from decision where target_kind = ? and target_id = ?", (kind, tid)
        ).rowcount
    else:
        if snapshot is None:
            raise ApprovalError("a decision records the snapshot it was made on")
        conn.execute(
            "insert into decision (target_kind, target_id, action, target_snapshot, decided_at,"
            " decided_by) values (?, ?, ?, ?, ?, ?)",
            (kind, tid, action, snapshot, time.time(), decider),
        )
        n = 1
    conn.commit()
    return n


# ---------------------------------------------------------------- the hook's fallback
def sidecar_path(home: Path) -> Path:
    return store.data_dir(home) / SIDECAR


def enforced_tools(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """tool -> rules with an enforced check in force on it."""
    out: dict[str, list[str]] = {}
    for r in query_as(conn, IN_FORCE_ROW, IN_FORCE):
        if r["mode"] == "enforce" and r["decision"] == "accept" and hash_ok(r):
            out.setdefault(r["tool"], []).append(r["proposal_id"])
    return out


def write_sidecar(home: Path, conn: sqlite3.Connection) -> None:
    """Write `enforced.json` (tool -> rules enforced on it, verified ones only), read by the hook
    only when it cannot read the plugin DB; replaced atomically."""
    write_sidecar_tools(home, enforced_tools(conn))


def write_sidecar_tools(home: Path, tools: Mapping[str, list[str]]) -> None:
    """Write the sidecar with `tools` (tool -> rules), atomically. The hook also calls it after
    each re-read of the approvals that finds a different verified enforced set (a hash mismatch
    since, say), so the sidecar follows the last state Argus could verify."""
    path = sidecar_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{SIDECAR}.{os.getpid()}.{threading.get_ident()}")
    body: dict[str, object] = {"v": 1, "tools": {t: list(p) for t, p in tools.items()}}
    tmp.write_text(json.dumps(body), encoding="utf-8")
    tmp.replace(path)


def read_sidecar(home: Path) -> dict[str, list[str]] | None:
    """The sidecar's tool -> rules; None when it is missing or unreadable."""
    try:
        tools = as_object(loads_object(sidecar_path(home).read_text(encoding="utf-8")).get("tools"))
    except (OSError, ValueError):
        return None
    if tools is None:
        return None
    return {t: [p for p in as_list(v) or [] if isinstance(p, str)] for t, v in tools.items()}


# ---------------------------------------------------------------- the person gate
def _ppid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # pid (comm) state ppid ...: comm may hold spaces and parentheses
    fields = stat.rsplit(")", 1)[-1].split()
    return int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None


def ancestor_environs(of: int | None = None) -> Iterator[tuple[int, dict[str, str]]]:
    """(pid, environment at exec) of each parent process up to and including pid 1 (no depth
    limit; a pid seen twice ends the walk), where /proc shows it; an unreadable environment counts
    as empty. Pid 1 is included: in a container it may be Hermes itself."""
    seen: set[int] = set()
    pid = _ppid(os.getpid() if of is None else of)
    while pid is not None and pid >= 1 and pid not in seen:
        seen.add(pid)
        yield pid, _environ_of(pid)
        pid = _ppid(pid)


def _environ_of(pid: int) -> dict[str, str]:
    try:
        return _environ(Path(f"/proc/{pid}/environ").read_bytes())
    except OSError:
        return {}


def _marker(env: Mapping[str, str]) -> str | None:
    return next((m for m in MARKERS if env.get(m)), None)


def session_gate(
    env: Mapping[str, str], ancestors: Iterator[tuple[int, dict[str, str]]]
) -> str | None:
    """Why a process with this environment and these parents runs inside a Hermes session (the
    agent, cron or kanban); None when it does not."""
    if m := _marker(env):
        return f"{m} is set: this command runs inside a Hermes session (the agent, cron or kanban)"
    for pid, anc in ancestors:
        if m := _marker(anc):
            return f"parent process {pid} runs inside a Hermes session ({m} is set)"
    return None


def person_gate(
    env: Mapping[str, str],
    stdin_tty: bool,
    stdout_tty: bool,
    ancestors: Iterator[tuple[int, dict[str, str]]],
) -> str | None:
    """Why this process may not approve or change a check from the CLI (None: it may): a person
    approves from a terminal of their own, not from a command the agent, cron or kanban runs."""
    if not (stdin_tty and stdout_tty):
        return "approving a check needs a person at a terminal (stdin and stdout must be a TTY)"
    return session_gate(env, ancestors)


def exec_environ() -> Mapping[str, str]:
    """The environment this process was started with (/proc/self/environ), else os.environ.
    Hermes sets HERMES_SESSION_ID in its own os.environ while it runs a session in-process (a
    dashboard serving chats): the environment at exec says who started the process."""
    try:
        raw = Path("/proc/self/environ").read_bytes()
    except OSError:
        return os.environ
    return _environ(raw)


def _environ(raw: bytes) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in raw.split(b"\x00"):
        k, sep, v = item.decode("utf-8", "replace").partition("=")
        if sep:
            env[k] = v
    return env


def agent_session() -> str | None:
    """`session_gate` for this process."""
    return session_gate(exec_environ(), ancestor_environs())


def refusal_here() -> str | None:
    """`person_gate` for this process."""
    return person_gate(exec_environ(), os.isatty(0), os.isatty(1), ancestor_environs())


def confirmed(sha: str, typed: str) -> bool:
    """Whether a person typed the first 8 characters of the sha256 they were shown."""
    return len(sha) >= 8 and typed.strip().lower() == sha[:8].lower()


# ---------------------------------------------------------------- what a person sees
def _describe(text: str | None) -> str:
    if text is None:
        return ""
    try:
        return checkspec.describe(checkspec.load(text))
    except checkspec.SpecError as e:
        return f"unreadable check: {e}"


def _state(
    approval: CheckApprovalRow | None, draft: CheckDraftRow | None, decision: str | None
) -> CheckState:
    if approval is not None:
        if approval["mode"] == "revoked":
            return "revoked"
        if not hash_ok(approval):
            return "unverified"
        return approval["mode"] if decision == "accept" else "suspended"
    if draft is None:
        return "undrafted"
    return "draft" if draft["spec"] else "uncheckable"


def _events(conn: sqlite3.Connection, pid: str) -> tuple[dict[str, int], list[CheckEvent]]:
    counts = {
        str(r["outcome"]): int(str(r["n"]))
        for r in query(
            conn,
            "select outcome, count(*) n from check_event where proposal_id = ? group by outcome",
            (pid,),
        )
    }
    last: list[CheckEvent] = [
        {
            "ts": e["ts"],
            "outcome": e["outcome"],
            "cause": e["cause"],
            "tool": e["tool_name"],
            "detail": e["detail"] or "",
            "sid": e["session_id"],
        }
        for e in query_as(
            conn,
            CHECK_EVENT_ROW,
            "select * from check_event where proposal_id = ? order by ts desc, id desc limit ?",
            (pid, EVENTS_SHOWN),
        )
    ]
    return counts, last


def rule_check(conn: sqlite3.Connection, pid: str) -> RuleCheck | None:
    """Rule `pid`'s check as the CLI and the dashboard show it; None when the rule is not
    accepted and has neither draft nor approval. Tolerates a DB without the check tables."""
    decision = apply.current_decision(conn, "proposal", pid)
    try:
        draft, approval = draft_of(conn, pid), latest_approval(conn, pid)
        counts, last = _events(conn, pid)
    except sqlite3.OperationalError:  # read-only, not migrated to SCHEMA_VERSION 3 yet
        draft, approval, counts, last = None, None, {}, []
    if decision != "accept" and draft is None and approval is None:
        return None
    shown: CheckApproval | None = None
    if approval is not None:
        shown = {
            "version": approval["version"],
            "mode": approval["mode"],
            "spec": approval["spec"],
            "describe": _describe(approval["spec"]),
            "sha256": approval["sha256"],
            "hash_ok": hash_ok(approval),
            "at": approval["decided_at"],
            "by": approval["decided_by"],
        }
    return {
        "state": _state(approval, draft, decision),
        "draft": draft["spec"] if draft else None,
        "draft_sha256": draft["sha256"] if draft else None,
        "draft_describe": _describe(draft["spec"] if draft else None),
        "reason": (draft["reason"] or "") if draft else "",
        "drafted_at": draft["drafted_at"] if draft else None,
        "approval": shown,
        "stale": bool(
            draft and draft["sha256"] and approval and draft["sha256"] != approval["sha256"]
        ),
        "would_block": counts.get("would_block", 0),
        "blocked": counts.get("blocked", 0),
        "unverified": counts.get("unverified", 0),
        "events": last,
    }


def spec_json(text: str) -> str:
    """A stored check text, indented for a person."""
    try:
        return json.dumps(loads(text), indent=2, sort_keys=True)
    except ValueError:
        return text
