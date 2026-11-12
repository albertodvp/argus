"""Opt-in auto-apply: carry out some proposals without waiting for a person.

Off unless the profile's setting `auto_apply` is `true`. At the end of a derive pass of that
profile (under its derive.lock, so one pass at a time), Argus accepts and applies, through the same
`apply.run` a person's click uses, the proposals that pass every rule below, and records who
decided: the decision's `decided_by` and the attempt's `applied_by` are `auto` (the ledger entry
Hermes writes still says actor `user`, the only actor a plugin can write as).
`hermes argus auto-apply` shows what it would apply now, and writes nothing.

A proposal is applied automatically only when all of these hold:

- the latest proposals pass emitted it (meta `proposals_last_ids`), even if that pass ran before the
  setting was turned on; older proposals (the backlog) are never worked through;
- its confidence is `high`, it cites at least `MIN_INTENTS` intents, and it patches an existing
  skill with lines it only adds (one hunk, nothing removed): never a new skill;
- nobody decided on it or tried to apply it, nor on another proposal for the same skill that cites
  one of its intents: the proposals pass rewords one lesson from the same intents, and that lesson
  was already answered;
- no change to its skill was rolled back since auto-apply first wrote or staged one there: undoing
  is a no, and auto-apply leaves that skill to the person for good;
- fewer than `auto_apply_per_week` automatic attempts that may have changed a skill (applied,
  staged or `unknown`) in the last 7 days;
- `apply`'s own checks pass, in a dry run first (skill resolved to one writable SKILL.md, context
  unchanged, rule not yet in the file, no identical pending write).

The rules are read again before each attempt (an attempt, a person's decision or a rollback can
change them during a pass), and the automatic accept is inserted only while the proposal still has
no decision and no attempt, in one statement, so a person's dismiss in the meantime wins. When
the apply is refused or fails, that decision is removed again (the attempt stays in apply_log, so
it is never retried) and the proposal is the person's again. An `unknown` outcome (the write may
have happened, e.g. with `skills.ledger` off) keeps the decision, counts against the budget and
ends the pass.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Final

from . import apply, hermes_data, store
from .dbtypes import APPLY_LOG_ROW, PROPOSAL_ROW, ProposalRow, count, query_as
from .jsontypes import as_list, loads, loads_object

BY: Final = "auto"
"""decision.decided_by and apply_log.applied_by of an automatic apply."""
POLICY: Final = "auto/v1"
MIN_INTENTS: Final = 2
WEEK: Final = 7 * 86400.0
SPENT: Final = ("applied", "staged", "unknown")
"""Outcomes of an automatic attempt that may have changed a skill: they use the budget."""
PER_WEEK: Final = 3
"""Default of the setting auto_apply_per_week."""
LAST_PASS: Final = "proposals_last_ids"
"""meta key: JSON list of the proposal ids the latest proposals pass emitted (derive.proposals)."""
REACH_BACK: Final = 3600.0
"""apply_log records an attempt after its write: the rollback stop reaches back this far for the
ledger entry the write left."""


def setting_on(value: object) -> bool:
    """The setting auto_apply: only a real `true` turns it on."""
    return value is True


def setting_per_week(value: object) -> int:
    """The setting auto_apply_per_week: a non-negative integer, else the default."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return PER_WEEK


def additive(diff: str | None) -> bool:
    """Whether a proposal's diff only inserts lines: one change hunk, no removed line."""
    hs = [h for h in apply.hunks(diff) if any(g in "+-" for g, _ in h)]
    return len(hs) == 1 and all(g != "-" for g, _ in hs[0]) and any(g == "+" for g, _ in hs[0])


def intents_of(p: ProposalRow) -> set[str]:
    """Distinct intent ids a proposal's evidence cites."""
    try:
        ev = loads_object(p["evidence"])
    except ValueError:
        return set()
    return {i for i in as_list(ev.get("intents")) or [] if isinstance(i, str) and i}


def last_pass(conn: sqlite3.Connection) -> set[str]:
    """Ids the latest proposals pass emitted; empty before the first pass of this version."""
    try:
        ids = as_list(loads(store.meta_get(conn, LAST_PASS) or "[]")) or []
    except ValueError:
        return set()
    return {i for i in ids if isinstance(i, str)}


def used(conn: sqlite3.Connection, now: float) -> int:
    """Automatic attempts that may have changed a skill (SPENT) in the 7 days before `now`."""
    return count(
        conn,
        "select count(*) from apply_log where applied_by = ? and outcome in"
        " ('applied', 'staged', 'unknown') and applied_at >= ?",
        (BY, now - WEEK),
    )


def _skill(p: ProposalRow) -> str:
    return p["skill_name"].removeprefix("new:").strip()


def stopped_skills(conn: sqlite3.Connection, home: Path) -> set[str]:
    """Skills where the ledger holds a rollback of a change made since auto-apply first wrote or
    staged one there (less REACH_BACK). Read from the ledger itself, so a staged write approved
    later (whose ledger entry apply_log never learns) counts too, and so does a person's own change
    rolled back there: when in doubt, the skill is left to the person."""
    rows = query_as(
        conn,
        APPLY_LOG_ROW,
        "select * from apply_log where applied_by = ? and target_kind = 'proposal' and outcome in"
        " ('applied', 'staged', 'unknown')",
        (BY,),
    )
    props = {p["id"]: p for p in query_as(conn, PROPOSAL_ROW, "select * from proposal")}
    since: dict[str, float] = {}
    for r in rows:
        if r["target_id"] in props:
            sk, t = _skill(props[r["target_id"]]), r["applied_at"] - REACH_BACK
            since[sk] = min(since.get(sk, t), t)
    if not since:
        return set()
    entries = hermes_data.read_ledger(home, since=min(since.values()))
    written = {e["id"]: (e.get("skill") or "", e["ts_epoch"]) for e in entries}
    out: set[str] = set()
    for e in entries:
        target = (e.get("evidence") or {}).get("rollback_target")
        if e.get("action") == "rollback" and target in written:
            sk, ts = written[target]
            if sk in since and ts >= since[sk]:
                out.add(sk)
    return out


def candidates(conn: sqlite3.Connection, home: Path) -> list[ProposalRow]:
    """Proposals the rules allow, best evidence first; the budget and apply's checks come later."""
    latest = last_pass(conn)
    if not latest:
        return []
    marks = ",".join("?" * len(latest))
    rows = query_as(
        conn,
        PROPOSAL_ROW,
        f"select * from proposal p where id in ({marks}) and confidence = 'high'"
        " and is_new_skill = 0 and skill_name not like 'new:%'"
        " and not exists (select 1 from decision d where d.target_kind = 'proposal'"
        " and d.target_id = p.id)"
        " and not exists (select 1 from apply_log a where a.target_kind = 'proposal'"
        " and a.target_id = p.id)",
        tuple(sorted(latest)),
    )
    # per skill, the intents of every proposal someone decided on or tried to apply: a proposal
    # citing one of them words a lesson already answered
    answered: dict[str, set[str]] = {}
    for q in query_as(
        conn,
        PROPOSAL_ROW,
        "select * from proposal p where exists (select 1 from decision d where d.target_kind ="
        " 'proposal' and d.target_id = p.id) or exists (select 1 from apply_log a where"
        " a.target_kind = 'proposal' and a.target_id = p.id)",
    ):
        answered.setdefault(_skill(q), set()).update(intents_of(q))
    stopped = stopped_skills(conn, home)
    ok = [
        p
        for p in rows
        if additive(p["diff"])
        and len(intents_of(p)) >= MIN_INTENTS
        and _skill(p) not in stopped
        and not intents_of(p) & answered.get(_skill(p), set())
    ]
    return sorted(ok, key=_rank)


def _rank(p: ProposalRow) -> tuple[int, str]:
    """More intents first, then by id (a stable order)."""
    return -len(intents_of(p)), p["id"]


def _decide(home: Path, p: ProposalRow) -> int | None:
    """Record the automatic accept, with what it decided on, only if the proposal still has no
    decision and no apply attempt (checked in the same statement); its decision id, or None when
    someone got there first."""
    snap = {
        "skill": p["skill_name"],
        "rule": p["rule"],
        "why": p["why"],
        "confidence": p["confidence"],
        "file": p["file"],
        "diff": p["diff"],
        "policy": POLICY,
    }
    conn = store.open_db(home)
    try:
        cur = conn.execute(
            "insert into decision (target_kind, target_id, action, target_snapshot, decided_at,"
            " decided_by, note) select 'proposal', ?, 'accept', ?, ?, ?, ? where not exists"
            " (select 1 from decision where target_kind = 'proposal' and target_id = ?) and not"
            " exists (select 1 from apply_log where target_kind = 'proposal' and target_id = ?)",
            (
                p["id"],
                json.dumps(snap, ensure_ascii=False),
                time.time(),
                BY,
                POLICY,
                p["id"],
                p["id"],
            ),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount == 1 else None
    finally:
        conn.close()


def _undecide(home: Path, did: int) -> None:
    conn = store.open_db(home)
    try:
        conn.execute("delete from decision where id = ? and decided_by = ?", (did, BY))
        conn.commit()
    finally:
        conn.close()


def _next(home: Path, tried: set[str]) -> ProposalRow | None:
    """The best proposal the rules allow now, among those not tried in this pass. Read fresh for
    each attempt: an earlier attempt, a person's decision or a rollback since can change which
    lessons are answered and which skills are stopped."""
    conn = store.open_db(home)
    try:
        return next((p for p in candidates(conn, home) if p["id"] not in tried), None)
    finally:
        conn.close()


def _allowed(home: Path, pid: str) -> bool:
    """Whether proposal `pid` still passes the rules, right before the automatic decision."""
    conn = store.open_db(home)
    try:
        return any(p["id"] == pid for p in candidates(conn, home))
    finally:
        conn.close()


def run(
    home: Path, per_week: int, dry_run: bool = False
) -> list[tuple[ProposalRow, apply.ApplyResult]]:
    """Apply what the rules allow in the profile at `home` (this process' HERMES_HOME, holding its
    derive.lock): each proposal tried, with its apply result. The rules are read again before each
    attempt. With `dry_run`, decide and write nothing: the results say what would be applied, as if
    each one shown had been applied (a later one teaching the same lesson is not shown)."""
    home = Path(home)
    if not store.db_path(home).exists():
        return []
    conn = store.open_db(home)
    try:
        left = per_week - used(conn, time.time())
    finally:
        conn.close()
    out: list[tuple[ProposalRow, apply.ApplyResult]] = []
    tried: set[str] = set()
    shown: dict[str, set[str]] = {}  # dry run: skill -> intents of the proposals shown
    while left > 0 and (p := _next(home, tried)) is not None:
        tried.add(p["id"])
        if intents_of(p) & shown.get(_skill(p), set()):
            continue  # the lesson of a proposal already shown in this dry run
        check = apply.proposal(home, p["id"], dry_run=True)
        if check["outcome"] != "dry-run":
            continue  # apply would refuse: leave it to the person, try nothing
        if dry_run:
            out.append((p, check))
            shown.setdefault(_skill(p), set()).update(intents_of(p))
            left -= 1
            continue
        if not _allowed(home, p["id"]):
            continue  # a decision or a rollback since the read: the person's answer stands
        did = _decide(home, p)
        if did is None:
            continue  # a person decided (or someone applied) since the policy read
        r = apply.run(home, "proposal", p["id"], applied_by=BY)
        out.append((p, r))
        if r["outcome"] in SPENT:
            left -= 1
        else:
            _undecide(home, did)
        if r["outcome"] == "unknown":
            break  # the write may have happened without a ledger entry: stop here
    return out
