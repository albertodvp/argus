"""The pre_tool_call hook (argus/enforce.py): observe vs enforce, the approval hash gate, the
failure semantics of docs/enforce.md section 8, and the cached read of the approvals."""

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import argus
from argus import capture, checks, checkspec, enforce, hermes_internals, store
from argus.dbtypes import CheckApprovalRow, count, query

FORCE_PUSH: checks.DraftAnswer = {
    "checkable": True,
    "reason": "a force push is one terminal command",
    "tool": "terminal",
    "field": "command",
    "match": "command",
    "program": "git",
    "args": ["push"],
    "flags": ["--force", "-f"],
}
PLUGIN_DIR = Path(argus.__file__).resolve().parent


# ---------------------------------------------------------------- fixtures shared with test_checks
def add_rule(
    home: Path, pid: str = "p1", rule: str = "Never force-push.", action: str = "accept"
) -> None:
    """A proposal with a decision, as derive and the dashboard write them."""
    conn = store.open_db(home)
    try:
        conn.execute(
            "insert or ignore into proposal (id, skill_name, rule, evidence, derived_by)"
            " values (?, 'git-flow', ?, '{}', 'test')",
            (pid, rule),
        )
        conn.execute(
            "insert into decision (target_kind, target_id, action, target_snapshot, decided_at,"
            " decided_by) values ('proposal', ?, ?, '{}', ?, 'dashboard')",
            (pid, action, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def draft(home: Path, pid: str = "p1", answer: checks.DraftAnswer = FORCE_PUSH) -> str:
    """Record a draft; returns its sha256."""
    conn = store.open_db(home)
    try:
        assert checks.save_draft(conn, pid, answer) == "drafted"
        d = checks.draft_of(conn, pid)
        assert d is not None
        assert d["sha256"] is not None
        return d["sha256"]
    finally:
        conn.close()


def approve(
    home: Path, pid: str = "p1", answer: checks.DraftAnswer = FORCE_PUSH, mode: str = "observe"
) -> CheckApprovalRow:
    """An accepted rule with a drafted check, approved by a person, in `mode`."""
    add_rule(home, pid)
    sha = draft(home, pid, answer)
    conn = store.open_db(home)
    try:
        row = checks.approve(conn, home, pid, sha, checks.at_terminal())
        if mode == "enforce":
            row = checks.set_mode(conn, home, pid, "enforce", sha, by_dashboard())
        return row
    finally:
        conn.close()


def events(home: Path) -> list[tuple[str, str, str]]:
    assert store.writer(home).flush()
    conn = store.open_db(home)
    try:
        return [
            (str(r["proposal_id"]), str(r["outcome"]), str(r["cause"]))
            for r in query(conn, "select * from check_event order by id")
        ]
    finally:
        conn.close()


def _nobody_refuses() -> str | None:
    return None


AUTHORIZED = object()
"""Stands for a dashboard request Hermes' route guard authorized (the guard: hermes_internals)."""


def _authorized(request: object) -> bool:
    return request is AUTHORIZED


def by_dashboard() -> checks.Person:
    """The proof the dashboard's /check makes of an authorized request."""
    return checks.on_dashboard(AUTHORIZED)


def hook_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A profile home this process' hooks run in, with a fresh hook cache."""
    h = tmp_path / "hermes"
    h.mkdir()
    monkeypatch.setattr(hermes_internals, "hermes_home", lambda: h)
    # a person at a terminal outside any Hermes session (the gate itself: test_checks)
    monkeypatch.setattr(checks, "refusal_here", _nobody_refuses)
    monkeypatch.setattr(checks, "agent_session", _nobody_refuses)
    monkeypatch.setattr(hermes_internals, "dashboard_request_authorized", _authorized)
    enforce.reset()
    yield h
    enforce.reset()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield from hook_home(tmp_path, monkeypatch)


def call(command: object, tool: str = "terminal") -> dict[str, str] | None:
    return enforce.pre_tool_call(
        tool_name=tool, args={"command": command}, session_id="s1", tool_call_id="c1"
    )


# ---------------------------------------------------------------- registration
def test_the_hook_is_registered_and_declared() -> None:
    assert argus.HOOKS["pre_tool_call"] is enforce.pre_tool_call
    assert "  - pre_tool_call\n" in (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")


# ---------------------------------------------------------------- modes
def test_no_plugin_db_and_no_checks_allow(home: Path) -> None:
    assert call("git push -f") is None
    add_rule(home)
    assert call("git push -f") is None
    draft(home)  # a draft is inert until a person approves it
    assert call("git push -f") is None
    assert events(home) == []


def test_observe_records_and_lets_the_call_run(home: Path) -> None:
    approve(home)
    assert call("git push -f") is None
    assert call("git status") is None
    assert call("git push $(x)") is None  # cannot verify: recorded as would block
    assert events(home) == [("p1", "would_block", "match"), ("p1", "would_block", "unknown")]


def test_events_of_a_deleted_session_are_cleared(home: Path) -> None:
    approve(home)
    assert call("git push -f") is None
    assert events(home) == [("p1", "would_block", "match")]
    capture.on_session_delete(session_id="s1")
    assert events(home) == []


def test_enforce_blocks_with_rule_check_and_undo(home: Path) -> None:
    approve(home, mode="enforce")
    out = call("cd /repo && git push origin main --force")
    assert out is not None
    assert out["action"] == "block"
    msg = out["message"]
    for part in (
        "check p1 v1",
        "Never force-push.",
        "skill git-flow",
        "matches: git push origin main --force",
        "hermes argus checks observe p1",
        "hermes argus checks revoke p1",
    ):
        assert part in msg, part
    assert call("git push") is None
    assert call("git push -f", tool="execute_code") is None  # another tool: not this check
    unknown = call("git push `x`")
    assert unknown is not None
    assert "cannot be verified" in unknown["message"]
    assert events(home) == [("p1", "blocked", "match"), ("p1", "blocked", "unknown")]


def test_every_enforced_check_of_the_tool_is_evaluated(home: Path) -> None:
    approve(home, mode="enforce")
    rm: checks.DraftAnswer = {**FORCE_PUSH, "program": "rm", "args": [], "flags": ["-r"]}
    approve(home, "p2", rm, mode="enforce")
    out = call("rm -r x; git push -f")
    assert out is not None
    assert "check p1" in out["message"]
    assert "check p2" in out["message"]
    out = call("rm -r x")
    assert out is not None
    assert "check p2" in out["message"]
    assert "check p1" not in out["message"]


def test_a_rule_no_longer_accepted_suspends_its_check(home: Path) -> None:
    approve(home, mode="enforce")
    assert call("git push -f") is not None
    add_rule(home, action="dismiss")  # the cache sees another connection's commit
    assert call("git push -f") is None
    conn = store.open_db(home)
    try:
        rc = checks.rule_check(conn, "p1")
    finally:
        conn.close()
    assert rc is not None
    assert rc["state"] == "suspended"


def test_revoke_and_observe_lift_the_block(home: Path) -> None:
    row = approve(home, mode="enforce")
    conn = store.open_db(home)
    try:
        checks.set_mode(conn, home, "p1", "observe", row["sha256"], checks.at_terminal())
        assert call("git push -f") is None
        checks.set_mode(conn, home, "p1", "revoked", row["sha256"], checks.at_terminal())
        assert call("git push -f") is None
    finally:
        conn.close()
    assert events(home) == [("p1", "would_block", "match")]


# ---------------------------------------------------------------- the hash gate
def test_a_check_changed_after_approval_is_not_enforced_and_reported_once(home: Path) -> None:
    approve(home, mode="enforce")
    weaker = checkspec.canonical(
        checkspec.parse_spec({**checkspec.load(_spec(home)), "program": "nothing"})
    )
    conn = store.open_db(home)
    try:
        conn.execute("update check_approval set spec = ?", (weaker,))  # sha256 left as approved
        conn.commit()
    finally:
        conn.close()
    assert call("git push -f") is None
    assert call("git push -f") is None
    assert events(home) == [("p1", "unverified", "hash")]
    conn = store.open_db(home)
    try:  # a person approves the drafted check again: the copy is restored, enforced again
        d = checks.draft_of(conn, "p1")
        assert d is not None
        restored = checks.approve(conn, home, "p1", str(d["sha256"]), checks.at_terminal())
        assert (restored["version"], restored["mode"]) == (1, "observe")
        checks.set_mode(conn, home, "p1", "enforce", restored["sha256"], checks.at_terminal())
    finally:
        conn.close()
    assert call("git push -f") is not None
    conn = store.open_db(home)
    try:  # tamper again, for the mode-change refusals below
        conn.execute("update check_approval set spec = ?", (weaker,))
        conn.commit()
    finally:
        conn.close()
    conn = store.open_db(home)
    try:
        rc = checks.rule_check(conn, "p1")
        assert rc is not None
        assert rc["state"] == "unverified"
        assert rc["approval"] is not None
        assert not rc["approval"]["hash_ok"]
        with pytest.raises(checks.ApprovalError, match="already enforce"):
            checks.set_mode(
                conn, home, "p1", "enforce", rc["approval"]["sha256"], checks.at_terminal()
            )
        checks.set_mode(conn, home, "p1", "observe", rc["approval"]["sha256"], checks.at_terminal())
        with pytest.raises(checks.ApprovalError, match="does not match its sha256"):
            checks.set_mode(
                conn, home, "p1", "enforce", rc["approval"]["sha256"], checks.at_terminal()
            )
    finally:
        conn.close()


def _spec(home: Path) -> str:
    conn = store.open_db(home)
    try:
        row = checks.latest_approval(conn, "p1")
        assert row is not None
        return row["spec"]
    finally:
        conn.close()


def test_an_unverified_check_stays_unenforced_when_events_cannot_be_written(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(home, mode="enforce")
    conn = store.open_db(home)
    try:
        conn.execute("update check_approval set sha256 = ?", ("0" * 64,))
        conn.commit()
    finally:
        conn.close()

    def broken(_home: Path) -> store.Writer:
        raise RuntimeError("writer unavailable")

    monkeypatch.setattr(store, "writer", broken)
    assert call("git push -f") is None


def test_the_fallback_never_enforces_an_unverified_check(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even when reporting the mismatch raises past `_record` (the hook's last-resort fallback
    then decides from the cached snapshot), a check whose hash does not match stays unenforced;
    a verified enforced check on the same failure still blocks."""
    approve(home, mode="enforce")

    def broken(*_args: object) -> None:
        raise RuntimeError("cannot record")

    monkeypatch.setattr(enforce, "_record", broken)
    blocked = call("git push -f")  # verified: the error falls back to the enforced snapshot
    assert blocked is not None
    assert "Argus failed while checking" in blocked["message"]
    conn = store.open_db(home)
    try:
        conn.execute("update check_approval set sha256 = ?", ("0" * 64,))
        conn.commit()
    finally:
        conn.close()
    assert call("git push -f") is None
    assert call("ls") is None


def test_a_newer_format_check_is_bound_to_the_tool_its_text_names(home: Path) -> None:
    """A check text this version cannot evaluate (format v2) still has its `tool` column checked
    against the tool the text names: moved to another tool it is unverified, not enforced there."""
    approve(home, mode="enforce")
    newer = '{"match":"something-new","tool":"terminal","v":2}'
    conn = store.open_db(home)
    try:
        conn.execute(
            "update check_approval set spec = ?, sha256 = ?, tool = 'read_file'",
            (newer, checkspec.digest(newer)),
        )
        conn.commit()
        rc = checks.rule_check(conn, "p1")
        assert rc is not None
        assert rc["state"] == "unverified"
        assert checks.enforced_tools(conn) == {}
    finally:
        conn.close()
    assert call("ls", tool="read_file") is None
    assert call("ls") is None
    conn = store.open_db(home)
    try:  # in its own tool's place it is an enforced check Argus cannot evaluate: it blocks
        conn.execute("update check_approval set tool = 'terminal'")
        conn.commit()
    finally:
        conn.close()
    assert call("ls") is not None
    conn = store.open_db(home)
    try:  # a hash-valid text that is not even JSON names no tool: unverified
        conn.execute("update check_approval set spec = 'x', sha256 = ?", (checkspec.digest("x"),))
        conn.commit()
    finally:
        conn.close()
    assert call("ls") is None


def test_moving_an_approved_check_to_another_tool_unverifies_it(home: Path) -> None:
    approve(home, mode="enforce")
    conn = store.open_db(home)
    try:  # the hash covers the check's text, and the text names its tool
        conn.execute("update check_approval set tool = 'read_file'")
        conn.commit()
        rc = checks.rule_check(conn, "p1")
        assert rc is not None
        assert rc["state"] == "unverified"
        assert checks.enforced_tools(conn) == {}
    finally:
        conn.close()
    assert call("ls", tool="read_file") is None
    assert call("git push -f") is None
    assert events(home) == [("p1", "unverified", "hash")]


# ---------------------------------------------------------------- failure semantics
def _raise(*_args: object) -> checkspec.Verdict:
    raise RuntimeError("boom")


def test_an_error_in_an_enforced_check_blocks(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    approve(home, mode="enforce")
    monkeypatch.setattr(checkspec, "evaluate", _raise)
    out = call("ls")
    assert out is not None
    assert "failed while checking this call (RuntimeError: boom)" in out["message"]
    assert events(home) == [("p1", "blocked", "error")]


def test_an_error_in_an_observed_check_allows(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    approve(home)
    monkeypatch.setattr(checkspec, "evaluate", _raise)
    assert call("ls") is None
    assert events(home) == [("p1", "would_block", "error")]


def test_an_unreadable_approved_text_fails_closed_for_its_tool(home: Path) -> None:
    approve(home, mode="enforce")
    conn = store.open_db(home)
    try:  # a text from a newer format, hashed consistently: readable hash, unreadable check
        text = '{"v":2,"tool":"terminal","match":"tool"}'
        conn.execute(
            "update check_approval set spec = ?, sha256 = ?", (text, checkspec.digest(text))
        )
        conn.commit()
    finally:
        conn.close()
    out = call("ls")
    assert out is not None
    assert "cannot be read" in out["message"]
    assert call("ls", tool="read_file") is None


def _corrupt(home: Path) -> None:
    path = store.db_path(home)
    for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        p.unlink(missing_ok=True)
    path.write_bytes(b"this is not a database" * 100)


def test_a_malformed_approval_row_is_an_unreadable_db(home: Path) -> None:
    approve(home, mode="enforce")
    assert call("git push -f") is not None  # read once
    conn = store.open_db(home)
    try:
        conn.execute("update check_approval set version = 'not-an-int'")
        conn.commit()
    finally:
        conn.close()
    assert call("git push -f") is not None  # the last snapshot stands
    enforce.reset()  # a new process: the sidecar's enforced tools are blocked
    out = call("ls")
    assert out is not None
    assert "cannot read its approved checks" in out["message"]
    assert call("ls", tool="read_file") is None


def test_the_sidecar_follows_what_the_hook_verified(home: Path) -> None:
    """A check unverified after its sidecar entry was written: the next read of the approvals
    drops it from the sidecar, so a later process that cannot read the DB does not block for it."""
    approve(home, mode="enforce")
    assert checks.read_sidecar(home) == {"terminal": ["p1"]}
    conn = store.open_db(home)
    try:
        conn.execute("update check_approval set sha256 = ?", ("0" * 64,))
        conn.commit()
    finally:
        conn.close()
    assert call("git push -f") is None  # unverified: not enforced; the sidecar follows
    assert checks.read_sidecar(home) == {}
    _corrupt(home)
    enforce.reset()  # a new process that cannot read the DB
    assert call("git push -f") is None


def test_a_failed_sidecar_sync_is_retried_on_the_next_call(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(home, mode="enforce")
    conn = store.open_db(home)
    try:
        conn.execute("update check_approval set sha256 = ?", ("0" * 64,))
        conn.commit()
    finally:
        conn.close()
    real = checks.write_sidecar_tools

    def denied(_home: Path, _tools: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(checks, "write_sidecar_tools", denied)
    assert call("git push -f") is None
    assert checks.read_sidecar(home) == {"terminal": ["p1"]}  # could not be rewritten
    monkeypatch.setattr(checks, "write_sidecar_tools", real)
    assert call("ls") is None  # no DB change since: retried anyway
    assert checks.read_sidecar(home) == {}


def test_an_unreadable_sidecar_is_rewritten(home: Path) -> None:
    approve(home, mode="enforce")
    conn = store.open_db(home)
    try:
        conn.execute("update check_approval set sha256 = ?", ("0" * 64,))
        conn.commit()
    finally:
        conn.close()
    checks.sidecar_path(home).write_text("not json", encoding="utf-8")
    assert checks.read_sidecar(home) is None
    assert call("git push -f") is None
    assert checks.read_sidecar(home) == {}  # rewritten from what this read verified


def test_the_hook_creates_no_sidecar_without_checks(home: Path) -> None:
    add_rule(home)
    assert call("git push -f") is None
    assert not checks.sidecar_path(home).exists()


def test_unreadable_db_uses_the_last_snapshot(home: Path) -> None:
    approve(home, mode="enforce")
    assert call("git push -f") is not None  # read once
    _corrupt(home)
    assert call("git push -f") is not None
    assert call("git status") is None


def test_unreadable_db_without_snapshot_blocks_only_sidecar_tools(home: Path) -> None:
    approve(home, mode="enforce")
    assert checks.read_sidecar(home) == {"terminal": ["p1"]}
    _corrupt(home)
    enforce.reset()  # a new process: never read the approvals
    out = call("ls")
    assert out is not None
    assert "cannot read its approved checks" in out["message"]
    assert call("ls", tool="read_file") is None
    checks.sidecar_path(home).unlink()
    assert call("ls") is None  # nothing says a check is enforced: a broken install allows


def test_a_deleted_db_with_a_sidecar_blocks_enforced_tools(home: Path) -> None:
    approve(home, mode="enforce")
    store.writer(home).flush()
    for p in store.data_dir(home).glob("data.db*"):
        p.unlink()
    out = call("ls")
    assert out is not None
    assert "the plugin DB is missing" in out["message"]
    assert call("ls", tool="read_file") is None


def test_observed_checks_are_not_in_the_sidecar(home: Path) -> None:
    approve(home)
    assert checks.read_sidecar(home) == {}


def test_an_old_schema_db_has_no_checks(home: Path) -> None:
    conn = store.open_db(home)
    try:
        for t in ("check_event", "check_draft", "check_approval"):
            conn.execute(f"drop table {t}")
        store.meta_set(conn, "schema_version", 2)  # as an Argus before checks left it
    finally:
        conn.close()
    conn = sqlite3.connect(store.db_path(home))
    try:
        assert enforce.read_snapshot(conn) == enforce.EMPTY
    finally:
        conn.close()


def _drop_approvals(home: Path, *, forget_version: bool = False) -> None:
    conn = store.open_db(home)
    try:
        conn.execute("drop table if exists check_approval")
        if forget_version:
            conn.execute("delete from meta where key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()


def test_a_missing_check_table_in_a_checks_schema_db_is_unreadable(home: Path) -> None:
    """Only the schema version proves a DB predates checks: a check_approval dropped from a DB
    of the checks schema is an unreadable DB, and never shrinks the sidecar."""
    approve(home, mode="enforce")
    assert call("git push -f") is not None  # read once
    store.writer(home).flush()
    _drop_approvals(home)
    assert call("git push -f") is not None  # the last snapshot stands
    assert checks.read_sidecar(home) == {"terminal": ["p1"]}
    for forget_version in (False, True):  # a checks-schema version, then none at all
        _drop_approvals(home, forget_version=forget_version)
        enforce.reset()  # a new process: never read the approvals
        out = call("git push -f")
        assert out is not None
        assert "cannot read its approved checks" in out["message"]
        assert call("ls", tool="read_file") is None
        assert checks.read_sidecar(home) == {"terminal": ["p1"]}


def test_an_unexpected_error_blocks_only_where_a_check_is_enforced(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(home, mode="enforce")
    assert call("ls") is None  # the snapshot is read
    monkeypatch.setattr(enforce, "decide", _raise)
    out = call("ls")
    assert out is not None
    assert "fail closed" in out["message"]
    assert call("ls", tool="read_file") is None
    monkeypatch.setattr(hermes_internals, "hermes_home", _raise)
    assert call("ls") is None  # no home: nothing known to be enforced


def test_the_hook_never_raises_on_odd_payloads(home: Path) -> None:
    approve(home, mode="enforce")
    assert enforce.pre_tool_call() is None
    assert enforce.pre_tool_call(tool_name=3, args={"command": "git push -f"}) is None
    # arguments that are not a dict: every field is missing, so the check cannot verify
    for args in (None, "git push -f", ["git", "push", "-f"], {1: "x"}):
        out = enforce.pre_tool_call(tool_name="terminal", args=args)
        assert out is not None
        assert "argument command is missing" in out["message"]


# ---------------------------------------------------------------- the hot path
def test_approvals_are_read_again_only_after_a_commit(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(home)
    reads: list[int] = []
    real = enforce.read_snapshot

    def counting(conn: sqlite3.Connection) -> enforce.Snapshot:
        reads.append(1)
        return real(conn)

    monkeypatch.setattr(enforce, "read_snapshot", counting)
    for _ in range(5):
        call("ls")
    assert len(reads) == 1
    conn = store.open_db(home)
    try:
        checks.set_mode(conn, home, "p1", "enforce", _sha(conn), checks.at_terminal())
    finally:
        conn.close()
    assert call("git push -f") is not None  # seen at once
    assert len(reads) == 2


def _sha(conn: sqlite3.Connection) -> str:
    row = checks.latest_approval(conn, "p1")
    assert row is not None
    return row["sha256"]


def test_a_replaced_db_file_is_reopened(home: Path) -> None:
    approve(home, mode="enforce")
    assert call("git push -f") is not None
    store.writer(home).flush()
    for p in store.data_dir(home).glob("data.db*"):
        p.unlink()
    checks.sidecar_path(home).unlink()
    add_rule(home)  # a new plugin DB (a new inode) without approvals
    assert call("git push -f") is None
    conn = store.open_db(home)
    try:
        assert count(conn, "select count(*) from check_approval") == 0
    finally:
        conn.close()
