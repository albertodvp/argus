"""Drafting, approving and the person gate of enforced checks (argus/checks.py, argus/checks_cli.py,
the dashboard's /check), and the schema migration that adds their tables."""

import argparse
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import types
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from starlette.requests import Request
from test_enforce import AUTHORIZED, FORCE_PUSH, add_rule, approve, by_dashboard, draft, hook_home
from test_window import make_home, quiet

from argus import (
    apply,
    backfill,
    checks,
    checks_cli,
    cli,
    derive,
    enforce,
    hermes_api,
    hermes_internals,
    store,
)
from argus.dashboard import plugin_api
from argus.dbtypes import count, query
from argus.jsontypes import JsonObject


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield from hook_home(tmp_path, monkeypatch)


def _conn(home: Path) -> sqlite3.Connection:
    return store.open_db(home)


# ---------------------------------------------------------------- drafting
class DraftLlm:
    """Answers every check-drafting call with `answer`; counts the calls by purpose."""

    def __init__(self, answer: JsonObject) -> None:
        self.answer = answer
        self.purposes: Counter[str] = Counter()
        self.texts: list[str] = []

    def __call__(self, instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
        self.purposes[purpose] += 1
        self.texts.append(text)
        return self.answer if purpose == "argus.check" else None


ANSWER: JsonObject = {
    "checkable": True,
    "reason": "one terminal command shows it",
    "tool": "terminal",
    "field": "command",
    "match": "command",
    "value": "",
    "under": "",
    "program": "git",
    "args": ["push"],
    "flags": ["--force", "-f"],
}


def test_derive_drafts_accepted_rules_once_and_estimates_it(tmp_path: Path) -> None:
    h = tmp_path / "hermes"
    make_home(h, time.time())
    backfill.run(h, since_days=30, log=quiet)
    add_rule(h, "p1")
    add_rule(h, "p2", "Prefer the staging profile.", action="dismiss")
    est = derive.estimate(h)
    assert est is not None
    assert est["checks"] == 1
    assert "1 accepted rules to draft checks for" in derive.estimate_lines(est)[0]
    llm = DraftLlm(ANSWER)
    report = derive.run(h, llm, stages=["checks"], log=quiet)
    assert report["profiles"]["default"]["stages"]["checks"]["drafted"] == 1
    assert llm.purposes == Counter({"argus.check": 1})
    assert "RULE: Never force-push." in llm.texts[0]
    derive.run(h, llm, stages=["checks"], log=quiet)
    assert llm.purposes["argus.check"] == 1  # drafted once
    est = derive.estimate(h)
    assert est is not None
    assert est["checks"] == 0
    conn = _conn(h)
    try:
        assert count(conn, "select count(*) from check_approval") == 0  # drafting never approves
        rc = checks.rule_check(conn, "p1")
        assert rc is not None
        assert rc["state"] == "draft"
        assert rc["draft_describe"] == "terminal.command runs `git push` with --force|-f"
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("answer", "outcome", "reason"),
    [
        ({"checkable": False, "reason": "an order of steps"}, "uncheckable", "an order of steps"),
        ({**ANSWER, "program": "/usr/bin/git"}, "refused", "does not parse"),
        ({**ANSWER, "match": "regex"}, "refused", "does not parse"),
        ({**ANSWER, "tool": "terminal; rm"}, "refused", "not a tool name"),
    ],
)
def test_a_draft_that_is_not_a_check_is_recorded_as_such(
    home: Path, answer: JsonObject, outcome: str, reason: str
) -> None:
    add_rule(home)
    conn = _conn(home)
    try:
        assert checks.save_draft(conn, "p1", checks.DRAFT_ANSWER.validate_python(answer)) == outcome
        rc = checks.rule_check(conn, "p1")
        assert rc is not None
        assert rc["state"] == "uncheckable"
        assert reason in rc["reason"]
        with pytest.raises(checks.ApprovalError, match="not checkable"):
            checks.approve(conn, home, "p1", "0" * 64, checks.at_terminal())
    finally:
        conn.close()


def test_spec_of_answer_keeps_only_the_fields_of_its_kind() -> None:
    raw: JsonObject = {**ANSWER, "match": "path", "under": "/etc"}
    answer = checks.DRAFT_ANSWER.validate_python(raw)
    spec = checks.spec_of_answer(answer)
    assert spec == {
        "v": 1,
        "tool": "terminal",
        "field": "command",
        "match": "path",
        "under": "/etc",
    }


# ---------------------------------------------------------------- approval
def test_approval_is_bound_to_the_hash_shown(home: Path) -> None:
    conn = _conn(home)
    try:
        with pytest.raises(checks.ApprovalError, match="not accepted"):
            checks.approve(conn, home, "p1", "0" * 64, checks.at_terminal())
        add_rule(home)
        with pytest.raises(checks.ApprovalError, match="no drafted check"):
            checks.approve(conn, home, "p1", "0" * 64, checks.at_terminal())
        sha = draft(home)
        with pytest.raises(checks.ApprovalError, match="changed since it was shown"):
            checks.approve(conn, home, "p1", "0" * 64, checks.at_terminal())
        for unminted in (
            "cli",
            "auto",
            checks.Person("cli", object()),
        ):  # a string, an unminted proof
            with pytest.raises(checks.ApprovalError, match="a person's"):
                checks.approve(conn, home, "p1", sha, unminted)  # type: ignore[arg-type]
        row = checks.approve(conn, home, "p1", sha.upper(), checks.at_terminal())
        assert (row["version"], row["mode"], row["decided_by"], row["sha256"]) == (
            1,
            "observe",
            "cli",
            sha,
        )
        assert row["rule"] == "Never force-push."
        assert row["tool"] == "terminal"
        with pytest.raises(checks.ApprovalError, match="already approved"):
            checks.approve(conn, home, "p1", sha, checks.at_terminal())
    finally:
        conn.close()


def test_a_new_draft_needs_a_new_approval_and_versions(home: Path) -> None:
    v1 = approve(home)
    rm: checks.DraftAnswer = {**FORCE_PUSH, "flags": ["--force"]}
    sha2 = draft(home, "p1", rm)
    conn = _conn(home)
    try:
        rc = checks.rule_check(conn, "p1")
        assert rc is not None
        assert rc["stale"]
        assert rc["state"] == "observe"
        assert checks.latest_approval(conn, "p1") == {**v1, "id": v1["id"]}  # the copy stands
        v2 = checks.approve(conn, home, "p1", sha2, by_dashboard())
        assert v2["version"] == 2
        checks.set_mode(conn, home, "p1", "revoked", sha2, checks.at_terminal())
        sha1 = draft(home, "p1", FORCE_PUSH)
        assert (
            checks.approve(conn, home, "p1", sha1, checks.at_terminal())["version"] == 1
        )  # same text, same version
    finally:
        conn.close()


def test_the_person_gate_holds_at_the_approval_functions(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_rule(home)
    sha = draft(home)
    conn = _conn(home)
    try:
        cli_proof, dashboard_proof = checks.at_terminal(), by_dashboard()
        monkeypatch.setattr(checks, "refusal_here", lambda: "no terminal")
        with pytest.raises(checks.ApprovalError, match="no terminal"):
            checks.at_terminal()
        # a proof minted earlier does not outlive the gate: approve checks it again
        with pytest.raises(checks.ApprovalError, match="no terminal"):
            checks.approve(conn, home, "p1", sha, cli_proof)
        monkeypatch.setattr(checks, "agent_session", lambda: "HERMES_SESSION_ID is set")
        with pytest.raises(checks.ApprovalError, match="HERMES_SESSION_ID"):
            by_dashboard()
        with pytest.raises(checks.ApprovalError, match="HERMES_SESSION_ID"):
            checks.approve(conn, home, "p1", sha, dashboard_proof)
        monkeypatch.setattr(checks, "agent_session", lambda: None)
        with pytest.raises(checks.ApprovalError, match="not a request the Hermes dashboard"):
            checks.on_dashboard({"X-Hermes-Session-Token": "guessed"})
        assert count(conn, "select count(*) from check_approval") == 0
        monkeypatch.setattr(checks, "refusal_here", lambda: None)
        checks.approve(conn, home, "p1", sha, cli_proof)
        monkeypatch.setattr(checks, "refusal_here", lambda: "no terminal")
        with pytest.raises(checks.ApprovalError, match="no terminal"):
            checks.set_mode(conn, home, "p1", "enforce", sha, cli_proof)
        assert count(conn, "select count(*) from check_approval") == 1
    finally:
        conn.close()


AGENT_PROBE = r"""
import os, sys
from pathlib import Path
from argus import checks, hermes_internals, store
home = Path(sys.argv[1])
sha = sys.argv[2]
conn = store.open_db(home)
minted = lambda by: checks.Person(by, checks._MINT)  # built outside the gate
as_ui = minted("dashboard")
refused = []
for name, call in (
    ("at_terminal", lambda: checks.at_terminal()),
    ("on_dashboard", lambda: checks.on_dashboard(object())),
    ("approve(str)", lambda: checks.approve(conn, home, "p1", sha, "cli")),
    ("approve(unchecked)", lambda: checks.approve(conn, home, "p1", sha, minted("cli"))),
    ("set_mode(unchecked)", lambda: checks.set_mode(conn, home, "p1", "enforce", sha, as_ui)),
):
    try:
        call()
    except checks.ApprovalError as e:
        refused.append(f"{name}: {e}")
row = dict(
    id=0, proposal_id="p1", version=1, tool="terminal", skill_name="git", rule="r",
    spec="{}", sha256=sha, mode="enforce", decided_at=0.0, decided_by="cli",
)
for name, call in (
    ("_insert(str)", lambda: checks._insert(conn, home, row, "cli")),
    ("_insert(unchecked)", lambda: checks._insert(conn, home, row, minted("cli"))),
):
    try:
        call()
    except checks.ApprovalError as e:
        refused.append(f"{name}: {e}")
# as if this process served the dashboard and Hermes authorized the request: the marker decides
hermes_internals.dashboard_request_authorized = lambda request: True
try:
    checks.approve(conn, home, "p1", sha, checks.Person("dashboard", checks._MINT, object()))
except checks.ApprovalError as e:
    refused.append(f"approve(authorized request): {e}")
n = conn.execute("select count(*) from check_approval").fetchone()[0]
print(len(refused), n)
print("\n".join(refused))
"""


@pytest.mark.parametrize(
    "marker",
    ["HERMES_SESSION_ID", "HERMES_RPC_SOCKET"],  # the terminal tool; execute_code
)
def test_an_agent_process_cannot_approve_through_argus_functions(
    tmp_path: Path, marker: str
) -> None:
    """A process like the agent's terminal or execute_code sandbox runs (a Hermes marker set, no
    TTY): every way to an approval through Argus' own functions is refused, even a proof built
    with the private mint."""
    h = tmp_path / "hermes"
    add_rule(h)
    sha = draft(h)
    env = {k: v for k, v in os.environ.items() if k not in checks.MARKERS}
    env.update({marker: "agent", "HERMES_HOME": str(h)})
    env["PYTHONPATH"] = os.pathsep.join([str(Path(checks.__file__).parents[1]), *sys.path])
    done = subprocess.run(
        [sys.executable, "-c", AGENT_PROBE, str(h), sha],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    head, *why = done.stdout.splitlines()
    assert head == "8 0", done.stdout + done.stderr
    assert all("TTY" in w or marker in w or "dashboard" in w for w in why), why
    assert why[-1].startswith("approve(authorized request)"), why
    assert marker in why[-1], why


def _never(_request: object) -> bool:
    return False


def test_a_dashboard_proof_needs_its_request_authorized_at_the_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even where no session marker is visible (a sandbox whose markers were scrubbed and whose
    parents carry none), a dashboard proof holds only while Hermes' dashboard authorizes the
    request it carries: a proof built with the private mint has none."""
    h = tmp_path / "hermes"
    add_rule(h)
    sha = draft(h)
    monkeypatch.setattr(checks, "agent_session", lambda: None)
    conn = _conn(h)
    try:
        for proof in (
            checks.Person("dashboard", checks._MINT),
            checks.Person("dashboard", checks._MINT, {"x-hermes-session-token": "guessed"}),
        ):
            with pytest.raises(checks.ApprovalError, match="not a request the Hermes dashboard"):
                checks.approve(conn, h, "p1", sha, proof)
        monkeypatch.setattr(hermes_internals, "dashboard_request_authorized", _authorized)
        proof = checks.on_dashboard(AUTHORIZED)
        monkeypatch.setattr(hermes_internals, "dashboard_request_authorized", _never)
        with pytest.raises(checks.ApprovalError, match="not a request the Hermes dashboard"):
            checks.approve(conn, h, "p1", sha, proof)  # checked again at the boundary
        assert count(conn, "select count(*) from check_approval") == 0
    finally:
        conn.close()


def test_the_row_writer_checks_the_person_itself(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_insert`, the only statement that writes approvals, refuses without a person's proof and
    records the proof's kind, whatever its caller wrote in the row."""
    row = approve(home)
    conn = _conn(home)
    try:
        for unminted in ("cli", None, checks.Person("cli", object())):
            with pytest.raises(checks.ApprovalError, match="a person's"):
                checks._insert(conn, home, {**row, "mode": "enforce"}, unminted)
        monkeypatch.setattr(checks, "refusal_here", lambda: "no terminal")
        with pytest.raises(checks.ApprovalError, match="no terminal"):
            checks._insert(
                conn, home, {**row, "mode": "enforce"}, checks.Person("cli", checks._MINT)
            )
        written = checks._insert(conn, home, {**row, "decided_by": "cli"}, by_dashboard())
        assert written["decided_by"] == "dashboard"
        assert count(conn, "select count(*) from check_approval") == 2
    finally:
        conn.close()


def test_only_the_gated_row_writer_inserts_approvals() -> None:
    """One statement in Argus writes check_approval, inside `_insert`, after `_decider`: no
    unchecked helper a caller could use instead."""
    for path in Path(checks.__file__).parent.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        pattern = r"(?:insert|update|delete|replace)[^\n\"]*check_approval"
        writes = [m.group(0) for m in re.finditer(pattern, text, re.I)]
        if path.name != "checks.py":
            assert writes == [], path
            continue
        assert len(writes) == 1, writes
        body = text[text.index("def _insert(") :]
        body = body[: body.index("\ndef ")]
        assert "insert into check_approval" in body
        assert body.index("_decider(person)") < body.index("insert into check_approval")


def test_a_failed_sidecar_write_changes_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sidecar is written before the approval commits: when it cannot be, the mode change is
    refused and rolled back (no enforcement without the hook's fallback list)."""
    row = approve(home)
    real = checks.write_sidecar

    def denied(_home: Path, _conn: sqlite3.Connection) -> None:
        raise OSError("sidecar denied")

    monkeypatch.setattr(checks, "write_sidecar", denied)
    conn = _conn(home)
    try:
        with pytest.raises(checks.ApprovalError, match="sidecar denied"):
            checks.set_mode(conn, home, "p1", "enforce", row["sha256"], checks.at_terminal())
        latest = checks.latest_approval(conn, "p1")
        assert latest is not None
        assert latest["mode"] == "observe"
        assert count(conn, "select count(*) from check_approval") == 1
        monkeypatch.setattr(checks, "write_sidecar", real)
        checks.set_mode(conn, home, "p1", "enforce", row["sha256"], checks.at_terminal())
        assert checks.read_sidecar(home) == {"terminal": ["p1"]}
    finally:
        conn.close()


def test_mode_changes(home: Path) -> None:
    row = approve(home)
    sha = row["sha256"]
    conn = _conn(home)
    try:
        with pytest.raises(checks.ApprovalError, match="changed since it was shown"):
            checks.set_mode(conn, home, "p1", "enforce", "f" * 64, checks.at_terminal())
        with pytest.raises(checks.ApprovalError, match="already observe"):
            checks.set_mode(conn, home, "p1", "observe", sha, checks.at_terminal())
        assert (
            checks.set_mode(conn, home, "p1", "enforce", sha, checks.at_terminal())["mode"]
            == "enforce"
        )
        assert checks.read_sidecar(home) == {"terminal": ["p1"]}
        checks.set_mode(conn, home, "p1", "revoked", sha, by_dashboard())
        assert checks.read_sidecar(home) == {}
        with pytest.raises(checks.ApprovalError, match="approve it again"):
            checks.set_mode(conn, home, "p1", "enforce", sha, checks.at_terminal())
        add_rule(home, "p2")
        sha2 = draft(home, "p2")
        checks.approve(conn, home, "p2", sha2, checks.at_terminal())
        add_rule(home, "p2", action="dismiss")
        with pytest.raises(checks.ApprovalError, match="no longer accepted"):
            checks.set_mode(conn, home, "p2", "enforce", sha2, checks.at_terminal())
        modes = [str(r["mode"]) for r in query(conn, "select mode from check_approval order by id")]
        assert modes == ["observe", "enforce", "revoked", "observe"]  # append-only
    finally:
        conn.close()


def test_resolve_a_prefix(home: Path) -> None:
    add_rule(home, "abcd1234")
    add_rule(home, "abcd5678")
    conn = _conn(home)
    try:
        assert checks.resolve(conn, "abcd1") == "abcd1234"
        for ref, error in (
            ("ab", "at least its first 4"),
            ("abcd", "2 rules"),
            ("zzzz", "0 rules"),
            ("ab%d", "0 rules"),
        ):
            with pytest.raises(checks.ApprovalError, match=error):
                checks.resolve(conn, ref)
    finally:
        conn.close()


# ---------------------------------------------------------------- the person gate
@pytest.mark.parametrize(
    ("env", "tty", "ancestors", "refused"),
    [
        ({}, (True, True), [], None),
        ({}, (False, True), [], "needs a person at a terminal"),
        ({}, (True, False), [], "needs a person at a terminal"),
        ({"HERMES_SESSION_ID": "s1"}, (True, True), [], "HERMES_SESSION_ID is set"),
        ({"HERMES_CRON_SESSION": "1"}, (True, True), [], "HERMES_CRON_SESSION is set"),
        ({"HERMES_KANBAN_TASK": "t"}, (True, True), [], "HERMES_KANBAN_TASK is set"),
        ({"HERMES_RPC_SOCKET": "/s"}, (True, True), [], "HERMES_RPC_SOCKET is set"),  # execute_code
        ({"HERMES_SESSION_ID": ""}, (True, True), [], None),  # empty: not set
        ({}, (True, True), [{"PATH": "/bin"}, {"HERMES_SESSION_KEY": "k"}], "parent process 101"),
    ],
)
def test_person_gate(
    env: dict[str, str],
    tty: tuple[bool, bool],
    ancestors: list[dict[str, str]],
    refused: str | None,
) -> None:
    why = checks.person_gate(env, tty[0], tty[1], iter(enumerate(ancestors, start=100)))
    assert (why is None) if refused is None else (why is not None and refused in why)


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="no /proc")
def test_the_environment_at_exec_is_read() -> None:
    env = checks.exec_environ()
    assert env.get("PATH") == os.environ.get("PATH") or "PATH" in env


def test_this_test_process_is_refused() -> None:
    # pytest captures stdin: not a terminal, whatever the environment
    assert checks.refusal_here() is not None


def test_every_ancestor_up_to_pid_1_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """No depth limit: a marker 500 processes up still refuses; a cycle in the parent links ends
    the walk."""
    parents = {pid: pid + 1 for pid in range(1000, 1600)} | {999: 1000, 1600: 1, 1: 0}
    marked = {1500: {"HERMES_SESSION_ID": "s"}}

    def ppid(pid: int) -> int | None:
        return parents.get(pid)

    def environ_of(pid: int) -> dict[str, str]:
        return marked.get(pid, {})

    monkeypatch.setattr(checks, "_ppid", ppid)
    monkeypatch.setattr(checks, "_environ_of", environ_of)
    why = checks.session_gate({}, checks.ancestor_environs(999))
    assert why is not None
    assert "parent process 1500" in why
    assert [p for p, _ in checks.ancestor_environs(999)][-2:] == [1600, 1]  # pid 1 included
    parents[1600] = 1000  # a cycle
    assert len(list(checks.ancestor_environs(999))) == 601


CHAIN = """
if [ "$1" -gt 0 ]; then
  sh "$0" $(($1 - 1))
  true
else
  exec "$PYTHON" -c 'from argus import checks; print(checks.agent_session())'
fi
"""


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="no /proc")
def test_a_marker_40_processes_up_is_found(tmp_path: Path) -> None:
    """A real process chain: the marked process starts 40 nested shells without the marker."""
    chain = tmp_path / "chain.sh"
    chain.write_text(CHAIN, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in checks.MARKERS}
    env["PYTHON"] = sys.executable
    env["PYTHONPATH"] = os.pathsep.join([str(Path(checks.__file__).parents[1]), *sys.path])
    # `; true` keeps the marked shell alive as the chain's parent (no exec); the marker is one
    # this test's own parents do not carry, so only the chain can explain a refusal naming it
    marked = f"env -u HERMES_KANBAN_TASK sh {chain} 40; true"
    done = subprocess.run(
        ["sh", "-c", marked],
        env={**env, "HERMES_KANBAN_TASK": "argus-test-chain"},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "runs inside a Hermes session (HERMES_KANBAN_TASK is set)" in done.stdout, done.stdout


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="no /proc")
def test_ancestor_environments_are_read() -> None:
    found = list(checks.ancestor_environs())
    assert found
    assert found[0][0] == os.getppid()


def test_confirmed() -> None:
    sha = "ab12cd34" + "0" * 56
    assert checks.confirmed(sha, " AB12CD34\n")
    assert not checks.confirmed(sha, "ab12cd3")
    assert not checks.confirmed(sha, "")


def _guard(request: object) -> None:
    if request != "signed-in":
        raise PermissionError("401")


def test_dashboard_authorization_is_hermes_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    """hermes_internals asks the dashboard server's own guard, or, in Hermes' plugin host, the
    guard decision Hermes put in the request's ASGI state; nothing in any other process."""
    for name in ("hermes_cli.web_server", "hermes_cli.plugin_host_http_auth"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    signed: dict[str, object] = {"type": "http", "state": {"hosted_http_authenticated": True}}
    assert not hermes_internals.dashboard_request_authorized(Request(signed))
    assert not hermes_internals.dashboard_request_authorized("signed-in")
    host = types.ModuleType("hermes_cli.plugin_host_http_auth")
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_host_http_auth", host)
    assert hermes_internals.dashboard_request_authorized(Request(signed))
    unsigned: dict[str, object] = {"type": "http", "state": {"hosted_http_authenticated": "yes"}}
    assert not hermes_internals.dashboard_request_authorized(Request(unsigned))
    bare: dict[str, object] = {"type": "http"}
    assert not hermes_internals.dashboard_request_authorized(Request(bare))
    assert not hermes_internals.dashboard_request_authorized(object())
    server = types.ModuleType("hermes_cli.web_server")
    setattr(server, "_require_token", _guard)  # noqa: B010
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", server)
    assert hermes_internals.dashboard_request_authorized("signed-in")
    assert not hermes_internals.dashboard_request_authorized(Request(signed))  # the guard decides


# ---------------------------------------------------------------- CLI
def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    return parser.parse_args(["checks", *argv])


def no_llm() -> hermes_api.LlmFn:
    raise AssertionError("no model call expected")


def test_cli_approve_is_refused_outside_a_terminal(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    add_rule(home)
    draft(home)
    monkeypatch.setattr(checks, "refusal_here", lambda: "stdin is not a TTY")
    assert checks_cli.run(home, parse("approve", "p1"), no_llm) == 1
    assert "refused" in capsys.readouterr().err
    conn = _conn(home)
    try:
        assert count(conn, "select count(*) from check_approval") == 0
    finally:
        conn.close()


def test_cli_approve_enforce_and_show(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    add_rule(home)
    sha = draft(home)
    typed = ["nope"]

    def ask(_prompt: str) -> str:
        return typed[0]

    monkeypatch.setattr(checks_cli, "ask", ask)
    assert checks_cli.run(home, parse("approve", "p1"), no_llm) == 1
    assert "not confirmed" in capsys.readouterr().err
    typed[0] = sha[:8]
    assert checks_cli.run(home, parse("approve", "p1"), no_llm) == 0
    assert checks_cli.run(home, parse("enforce", "p1"), no_llm) == 0
    out = capsys.readouterr().out
    assert sha in out
    assert "is now enforce" in out
    assert checks_cli.run(home, parse("show", "p1"), no_llm) == 0
    assert checks_cli.run(home, parse("list"), no_llm) == 0
    out = capsys.readouterr().out
    assert "state: enforce" in out
    assert "p1  enforce v1  git-flow: Never force-push." in out
    assert "checks (default): 1 enforce" in checks_cli.status_line(home)


def test_cli_draft_uses_the_model(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    add_rule(home)
    llm = DraftLlm(ANSWER)
    assert checks_cli.run(home, parse("draft"), lambda: llm) == 0
    assert checks_cli.run(home, parse("draft"), no_llm) == 0  # nothing left to draft
    assert checks_cli.run(home, parse("draft", "p1"), lambda: llm) == 0  # again on request
    assert llm.purposes["argus.check"] == 2
    assert "every accepted rule has a draft" in capsys.readouterr().out


# ---------------------------------------------------------------- dashboard
@pytest.fixture()
def root(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (home / "state.db").touch()  # a profile of the install
    monkeypatch.setattr(plugin_api, "_root", lambda: home)
    # the dashboard backend imports Argus under a package name of its own
    monkeypatch.setattr(f"{plugin_api._PKG}.checks.agent_session", lambda: None)
    monkeypatch.setattr(
        f"{plugin_api._PKG}.hermes_internals.dashboard_request_authorized", _authorized
    )
    return home


def _authorized(request: object) -> bool:
    return request is AUTHORIZED


def dashboard_proof() -> checks.Person:
    """What the /check route makes of an authorized request, in the dashboard's package copy."""
    who = plugin_api._person(AUTHORIZED)
    assert not isinstance(who, str), who
    return who


def test_dashboard_check_is_refused_inside_a_hermes_session(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_rule(root)
    sha = draft(root)
    body = {"profile": "default", "proposal_id": "p1", "action": "approve", "sha256": sha}
    who = dashboard_proof()
    monkeypatch.setattr(
        f"{plugin_api._PKG}.checks.agent_session", lambda: "HERMES_SESSION_ID is set"
    )
    code, out = plugin_api._check(body, who)
    assert code == 409
    assert "HERMES_SESSION_ID" in str(out)
    refused = plugin_api._person(AUTHORIZED)
    assert isinstance(refused, str)
    assert "HERMES_SESSION_ID" in refused
    # a proof of the test's own argus package is not one of the dashboard's package
    monkeypatch.setattr(f"{plugin_api._PKG}.checks.agent_session", lambda: None)
    code, out = plugin_api._check(body, checks.at_terminal())
    assert (code, "a person's" in str(out)) == (409, True)


def test_dashboard_route_refuses_a_request_hermes_did_not_authorize(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_rule(root)
    sha = draft(root)
    body = {"profile": "default", "proposal_id": "p1", "action": "approve", "sha256": sha}
    monkeypatch.setattr(
        f"{plugin_api._PKG}.hermes_internals.dashboard_request_authorized", _test_header
    )
    out = asyncio.run(plugin_api.check(_post(body, {})))
    assert out.status_code == 403
    assert b"not a request the Hermes dashboard authenticated" in out.body
    signed_in = _post(body, {"x-hermes-session-token": "t"})
    _SIGNED_IN.append(signed_in)
    out = asyncio.run(plugin_api.check(signed_in))
    assert out.status_code == 200, out.body


def test_an_unauthenticated_decision_cannot_turn_a_check_off_or_on(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check is in force only while its rule is accepted, so /decision is gated like /check: a
    request Hermes did not authorize can neither dismiss the rule (disabling its enforced check)
    nor accept it again (reactivating it)."""
    approve(root, mode="enforce")
    monkeypatch.setattr(
        f"{plugin_api._PKG}.hermes_internals.dashboard_request_authorized", _test_header
    )
    push = {"tool_name": "terminal", "args": {"command": "git push -f"}}
    assert enforce.pre_tool_call(**push) is not None
    body: dict[str, object] = {
        "profile": "default",
        "target_kind": "proposal",
        "target_id": "p1",
        "snapshot": {"rule": "Never force-push."},
    }
    out = asyncio.run(plugin_api.decision(_post({**body, "action": "dismiss"}, {})))
    assert out.status_code == 403
    assert b"not a request the Hermes dashboard authenticated" in out.body
    assert enforce.pre_tool_call(**push) is not None  # still enforced
    signed_in = _post({**body, "action": "dismiss"}, {"x-hermes-session-token": "t"})
    _SIGNED_IN.append(signed_in)
    out = asyncio.run(plugin_api.decision(signed_in))
    assert out.status_code == 200, out.body
    assert enforce.pre_tool_call(**push) is None  # suspended by a person's dismiss
    out = asyncio.run(plugin_api.decision(_post({**body, "action": "accept"}, {})))
    assert out.status_code == 403
    out = asyncio.run(plugin_api.decision(_post({**body, "action": "clear"}, {})))
    assert out.status_code == 403
    assert enforce.pre_tool_call(**push) is None  # stays suspended
    conn = _conn(root)
    try:
        assert apply.current_decision(conn, "proposal", "p1") == "dismiss"
    finally:
        conn.close()


_SIGNED_IN: list[object] = []
"""Requests Hermes' route guard would authorize, by identity (the route must pass its own)."""


def _test_header(request: object) -> bool:
    """Stands for Hermes' route guard."""
    return any(request is r for r in _SIGNED_IN)


def _post(body: Mapping[str, object], headers: dict[str, str]) -> Request:
    """A POST request as the dashboard server hands it to a route."""
    raw = json.dumps(body).encode()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": raw, "more_body": False}

    scope: dict[str, object] = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope, receive)


def test_dashboard_check_needs_the_hash_shown(root: Path) -> None:
    add_rule(root)
    sha = draft(root)
    body: dict[str, object] = {
        "profile": "default",
        "proposal_id": "p1",
        "action": "approve",
        "sha256": sha,
    }
    for change, code, error in (
        ({"action": "accept"}, 400, "action must be one of"),
        ({"sha256": "abc"}, 400, "64-character"),
        ({"profile": "x"}, 400, "unknown profile"),
        ({"proposal_id": ""}, 400, "proposal_id"),
        ({"sha256": "0" * 64}, 409, "changed since it was shown"),
        ({"action": "enforce"}, 409, "no approved check"),
    ):
        out = plugin_api._check({**body, **change}, dashboard_proof())
        assert out[0] == code, change
        assert error in str(out[1]), change
    assert plugin_api._check([body], dashboard_proof())[0] == 400
    code, done = plugin_api._check(body, dashboard_proof())
    assert code == 200
    assert done == {
        "ok": True,
        "profile": "default",
        "proposal_id": "p1",
        "action": "approve",
        "version": 1,
        "mode": "observe",
        "sha256": sha,
    }
    assert plugin_api._check({**body, "action": "enforce"}, dashboard_proof())[0] == 200
    conn = _conn(root)
    try:
        row = checks.latest_approval(conn, "p1")
        assert row is not None
        assert (row["mode"], row["decided_by"]) == ("enforce", "dashboard")
    finally:
        conn.close()


# ---------------------------------------------------------------- migration
def test_an_older_db_migrates_in_place(tmp_path: Path) -> None:
    h = tmp_path / "hermes"
    conn = store.open_db(h)  # then made a version-2 DB: without the check tables
    try:
        for t in ("check_event", "check_draft", "check_approval"):
            conn.execute(f"drop table {t}")
        conn.execute(
            "insert into proposal (id, skill_name, rule, evidence, derived_by)"
            " values ('p1', 's', 'r', '{}', 'x')"
        )
        conn.execute("update meta set value = '2' where key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()
    raw = sqlite3.connect(store.db_path(h))
    try:
        assert store.migrate(raw) == 2
        assert store.migrate(raw) == 3  # once
        tables = {
            str(r["name"])
            for r in query(raw, "select name from sqlite_master where type = 'table'")
        }
        assert {"check_draft", "check_approval", "check_event"} <= tables
        assert count(raw, "select count(*) from proposal") == 1
    finally:
        raw.close()
    conn = store.open_db(h)
    try:
        assert store.meta_get(conn, "schema_version") == "3"
        assert checks.to_draft(conn) == []
    finally:
        conn.close()


def test_a_new_or_newer_db(tmp_path: Path) -> None:
    raw = sqlite3.connect(tmp_path / "new.db")
    try:
        assert store.migrate(raw) == 0
        store.meta_set(raw, "schema_version", 99)
        assert store.migrate(raw) == 99  # a newer Argus' DB is left as it is
    finally:
        raw.close()
