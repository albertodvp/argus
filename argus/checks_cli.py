"""`hermes argus checks`: the deterministic checks of accepted rules (docs/enforce.md).

`list`, `show` and `draft` are open to anyone (draft makes model calls, like derive); `approve`,
`enforce`, `observe` and `revoke` are a person's: refused without a terminal or inside a Hermes
session (checks.person_gate), and confirmed by typing the first 8 characters of the check's
sha256.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from . import checks, derive, store
from .dbtypes import PROPOSAL_ROW, ProposalRow, query_as
from .exporttypes import CheckMode, RuleCheck
from .hermes_api import LlmFn

PERSON: Final[dict[str, CheckMode | None]] = {
    "approve": None,
    "enforce": "enforce",
    "observe": "observe",
    "revoke": "revoked",
}
SUBCOMMANDS: Final = ("list", "show", "draft", *PERSON)

ask: Callable[[str], str] = input
"""Reads the person's confirmation (tests replace it)."""


def setup(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """The `checks` subcommand of `hermes argus`."""
    p = sub.add_parser(
        "checks",
        help="deterministic checks of accepted rules, enforced in pre_tool_call: list, show,"
        " draft; a person only: approve, enforce, observe, revoke",
    )
    cs = p.add_subparsers(dest="checks_cmd", metavar="{" + ",".join(SUBCOMMANDS) + "}")
    cs.add_parser("list", help="every accepted rule's check: state, would-block and blocked calls")
    q = cs.add_parser("show", help="a rule's check: draft, approval, sha256, recent events")
    q.add_argument("id", help="proposal id (or its first characters)")
    q = cs.add_parser(
        "draft",
        help="draft checks now with the argus model (one call per rule): every accepted rule"
        " without a draft, or rule ID again",
    )
    q.add_argument("id", nargs="?", help="proposal id: draft this rule's check again")
    helps = {
        "approve": "approve the drafted check (mode observe: record would-block calls)",
        "enforce": "block the calls the approved check matches",
        "observe": "only record the calls the approved check matches",
        "revoke": "stop evaluating the check",
    }
    for name, text in helps.items():
        q = cs.add_parser(name, help=f"a person only: {text}")
        q.add_argument("id", help="proposal id (or its first characters)")


def _opt(args: argparse.Namespace, name: str) -> str | None:
    v: object = getattr(args, name, None)
    return v if isinstance(v, str) else None


def _when(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else "never"


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _rules(conn: sqlite3.Connection) -> list[tuple[ProposalRow, RuleCheck]]:
    out: list[tuple[ProposalRow, RuleCheck]] = []
    for p in query_as(conn, PROPOSAL_ROW, "select * from proposal order by skill_name, id"):
        rc = checks.rule_check(conn, p["id"])
        if rc is not None:
            out.append((p, rc))
    return out


def _what(rc: RuleCheck) -> str:
    a = rc["approval"]
    if a is not None:
        return a["describe"]
    return rc["draft_describe"] or rc["reason"] or "-"


def list_text(conn: sqlite3.Connection, home: Path) -> str:
    rows = _rules(conn)
    prof = store.profile_name(home)
    if not rows:
        return f"argus checks ({prof}): no accepted rule yet"
    lines = [f"argus checks ({prof}): {len(rows)} accepted rules"]
    for p, rc in rows:
        a = rc["approval"]
        version = f" v{a['version']}" if a else ""
        lines.append(
            f"  {p['id']}  {rc['state']}{version}  {p['skill_name']}: {_cut(p['rule'], 70)}"
        )
        counts = f"would block {rc['would_block']}, blocked {rc['blocked']}"
        stale = "; a newer draft differs" if rc["stale"] else ""
        lines.append(f"      {_cut(_what(rc), 90)}  ({counts}{stale})")
    return "\n".join(lines)


def show_text(conn: sqlite3.Connection, home: Path, p: ProposalRow, rc: RuleCheck) -> str:
    cmd = (
        "hermes"
        if store.profile_name(home) == "default"
        else f"hermes -p {store.profile_name(home)}"
    )
    lines = [
        f"rule {p['id']} ({p['skill_name']}): {p['rule']}",
        f"state: {rc['state']}",
        f"draft ({_when(rc['drafted_at'])}): {rc['draft_describe'] or 'none'}",
    ]
    if rc["draft"]:
        lines += [checks.spec_json(rc["draft"]), f"draft sha256: {rc['draft_sha256']}"]
    if rc["reason"]:
        lines.append(f"reason: {rc['reason']}")
    a = rc["approval"]
    if a is not None:
        lines += [
            f"approved: v{a['version']} mode {a['mode']} by {a['by']} {_when(a['at'])}",
            f"  {a['describe']}",
            f"  sha256 {a['sha256']}"
            + ("" if a["hash_ok"] else "  MISMATCH: the stored check changed, not enforced"),
        ]
    if rc["stale"]:
        lines.append(
            f"a newer draft differs from the approved check: `{cmd} argus checks approve {p['id']}`"
        )
    lines.append(
        f"events: would block {rc['would_block']}, blocked {rc['blocked']},"
        f" unverified {rc['unverified']}"
    )
    lines += [
        f"  {_when(e['ts'])} {e['outcome']} ({e['cause']}) {e['tool']}: {e['detail']}"
        for e in rc["events"]
    ]
    return "\n".join(lines)


def draft(conn: sqlite3.Connection, rules: list[ProposalRow], make_llm: Callable[[], LlmFn]) -> int:
    if not rules:
        print("argus checks draft: every accepted rule has a draft")
        return 0
    calls = derive.Calls(make_llm(), print)
    try:
        stats = derive.draft_checks(conn, calls, rules)
    except derive.ModelUnavailableError as e:
        print(f"argus checks draft: stopped: {e}", file=sys.stderr)
        return 1
    print(f"argus checks draft: {stats} ({calls.calls} model calls, {calls.failures} failed)")
    for p in rules:
        rc = checks.rule_check(conn, p["id"])
        if rc is not None:
            print(f"  {p['id']}  {rc['state']}  {_cut(_what(rc), 90)}")
    return 0 if calls.failures == 0 else 1


def person(conn: sqlite3.Connection, home: Path, action: str, p: ProposalRow, rc: RuleCheck) -> int:
    """approve | enforce | observe | revoke, by the person at this terminal."""
    try:
        who = checks.at_terminal()
    except checks.ApprovalError as e:
        print(
            f"argus checks {action}: {e}. Run it yourself in a terminal, or use the Argus"
            " dashboard tab.",
            file=sys.stderr,
        )
        return 1
    mode = PERSON[action]
    a = rc["approval"]
    text, sha = (
        (rc["draft"], rc["draft_sha256"])
        if mode is None
        else ((a["spec"], a["sha256"]) if a else (None, None))
    )
    if text is None or sha is None:
        what = "no checkable draft" if mode is None else "no approved check"
        print(f"argus checks {action}: rule {p['id']} has {what}", file=sys.stderr)
        return 1
    print(f"rule {p['id']} ({p['skill_name']}): {p['rule']}")
    print(checks.spec_json(text))
    print(f"sha256 {sha}")
    print(f"now: {rc['state']}; after: {'observe' if mode is None else mode}")
    try:
        typed = ask(f"type the first 8 characters of the sha256 to {action}: ")
    except EOFError:
        typed = ""
    if not checks.confirmed(sha, typed):
        print("not confirmed: nothing changed", file=sys.stderr)
        return 1
    try:
        row = (
            checks.approve(conn, home, p["id"], sha, who)
            if mode is None
            else checks.set_mode(conn, home, p["id"], mode, sha, who)
        )
    except checks.ApprovalError as e:
        print(f"argus checks {action}: refused: {e}", file=sys.stderr)
        return 1
    print(f"argus: check {p['id']} v{row['version']} is now {row['mode']}")
    return 0


def run(home: Path, args: argparse.Namespace, make_llm: Callable[[], LlmFn]) -> int:
    """`hermes argus checks ...` in the profile at `home`."""
    sub = _opt(args, "checks_cmd") or "list"
    if not store.db_path(home).exists():
        print("argus: no plugin data yet: run `hermes argus backfill` first", file=sys.stderr)
        return 1
    conn = store.open_db(home)
    try:
        if sub == "list":
            print(list_text(conn, home))
            return 0
        ref = _opt(args, "id")
        if sub == "draft" and ref is None:
            return draft(conn, checks.to_draft(conn), make_llm)
        try:
            pid = checks.resolve(conn, ref or "")
        except checks.ApprovalError as e:
            print(f"argus checks {sub}: {e}", file=sys.stderr)
            return 2
        p = query_as(conn, PROPOSAL_ROW, "select * from proposal where id = ?", (pid,))[0]
        rc = checks.rule_check(conn, pid)
        if sub == "draft":
            if rc is None:
                print(f"argus checks draft: rule {pid} is not accepted", file=sys.stderr)
                return 1
            return draft(conn, [p], make_llm)
        if rc is None:
            print(
                f"argus checks {sub}: rule {pid} is not accepted and has no check", file=sys.stderr
            )
            return 1
        if sub == "show":
            print(show_text(conn, home, p, rc))
            return 0
        return person(conn, home, sub, p, rc)
    finally:
        conn.close()


def status_line(home: Path) -> str:
    """The active profile's checks line of `hermes argus status`."""
    prof = store.profile_name(home)
    if not store.db_path(home).exists():
        return f"  checks ({prof}): none"
    conn = store.open_db(home)
    try:
        states = [rc["state"] for _p, rc in _rules(conn)]
    finally:
        conn.close()
    parts = [
        f"{states.count(s)} {s}"
        for s in ("enforce", "observe", "draft", "unverified", "suspended")
        if s in states
    ]
    return f"  checks ({prof}): {', '.join(parts) or 'none'}"
