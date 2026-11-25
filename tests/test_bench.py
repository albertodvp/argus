"""The bench end to end on a tiny synthetic home: build the fixture, record a derive on it with a
fake model, write the expected numbers, then replay it (later, elsewhere) to the same plugin DB
and numbers; a changed number or a missing call fails `just bench` (bench.check)."""

import json
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_window import DAY, make_home

from argus import cassette, cli, hermes_data, store
from argus.dbtypes import Row, query
from argus.jsontypes import JsonObject, JsonValue, loads_object
from bench import check, fixture

NOW = 1_790_000_000.0
# derived tables and the wall-clock columns derive writes (they never reach a prompt)
TABLES = ("tool_outcome", "skill_edit", "intent", "turn_intent", "run", "proposal")
CLOCK = ("created_at", "updated_at", "judged_at")


class FakeModel:
    """A deterministic model: one new intent per session, every intent failed, every edit
    speculative; two proposals, one quoting the first two intents' requests verbatim (kept, with
    its patch) and one without quotes (dropped as unquoted)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
        self.calls += 1
        if purpose == "argus.assign":
            return {"turns": [{"turn": "T1", "intent": "NEW", "title": text[-30:], "kind": "task"}]}
        if purpose == "argus.consolidate":
            return {"ops": []}
        if purpose == "argus.judge":
            return {"status": "failed", "friction": "slow", "attention": 1}
        if purpose == "argus.edit_review":
            return {"learned": "check first", "trigger": "t", "evidence_in_run": "speculative"}
        if purpose == "argus.edit_learned":
            return {"learned": "from the diff"}
        if purpose == "argus.proposals":
            # excerpt lines of the prompt: `  [<intent id>] user: <what the person typed>`
            users = [x[3:].partition("] user: ") for x in text.splitlines() if x[:3] == "  ["]
            excerpts = list({iid: req for iid, sep, req in users if sep}.items())[:2]
            cited: list[JsonValue] = [iid for iid, _req in excerpts]
            quotes: list[JsonValue] = [
                {"intent": iid, "text": " ".join(req.split()[:6])} for iid, req in excerpts
            ]
            return {
                "proposals": [
                    {
                        "skill": "k",
                        "rule": "Check the lock first.",
                        "why": "w",
                        "intents": cited,
                        "quotes": quotes,
                    },
                    {"skill": "k", "rule": "Guess first.", "why": "w", "intents": cited},
                ]
            }
        if purpose == "argus.patch":
            return {"after_line": 1, "lines": ["- Check the lock first."]}
        return None


def _session(home: Path, sid: str, ts: float, text: str) -> None:
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute(
            "insert into sessions values (?, 'cli', ?, null, ?, ?, null, 2, 0, null, null)",
            (sid, sid, ts, ts + 60),
        )
        q = (
            "insert into messages (session_id, role, content, timestamp, finish_reason)"
            " values (?, ?, ?, ?, ?)"
        )
        conn.execute(q, (sid, "user", text, ts + 1, None))
        conn.execute(q, (sid, "assistant", "done", ts + 2, "stop"))
        conn.commit()
    finally:
        conn.close()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _ledger_line(eid: str, ts: float, actor: str, sid: str | None) -> str:
    evidence: JsonObject = {"session_id": sid} if sid else {}
    entry: JsonObject = {
        "id": eid,
        "ts": _iso(ts),
        "actor": actor,
        "action": "patch",
        "skill": "k",
        "evidence": evidence,
        "before": [{"path": "/elsewhere/skills/k/SKILL.md", "sha256": "b0"}],
        "after": [{"path": "/elsewhere/skills/k/SKILL.md", "sha256": "a0"}],
    }
    return json.dumps(entry)


def synthetic_home(h: Path) -> None:
    """make_home's three sessions (1-10 days old at NOW), one 40 days old that no edit names (left
    out of a 30-day fixture) and one 40 days old that an edit in the window names (kept), a skill,
    ledger entries in and before the window and their blobs."""
    make_home(h, NOW)
    _session(h, "ancient", NOW - 40 * DAY, "an old request nobody edits a skill for anymore")
    _session(h, "named", NOW - 40 * DAY, "an old request whose edit lands in the window")
    for rel, text in {
        "k/SKILL.md": "# k\nfirst line\n",
        "k/references/notes.md": "not read by derive\n",
        ".archive/z/SKILL.md": "# archived\n",
    }.items():
        (h / "skills" / rel).parent.mkdir(parents=True, exist_ok=True)
        (h / "skills" / rel).write_text(text, encoding="utf-8")
    blobs = h / ".curator_backups" / "blobs"
    blobs.mkdir(parents=True)
    for sha, text in (("b0", "# k\nold\n"), ("a0", "# k\nnew\n"), ("zz", "unreferenced\n")):
        (blobs / sha).write_text(text, encoding="utf-8")
    lines = [
        _ledger_line("before", NOW - 35 * DAY, "agent", "s0"),
        _ledger_line("run", NOW - 5 * DAY + 3, "agent", "s1"),
        _ledger_line("named", NOW - 2 * DAY, "agent", "named"),
        _ledger_line("curator", NOW - 2 * DAY, "curator", None),
        _ledger_line("person", NOW - 1 * DAY, "user", None),
    ]
    hermes_data.ledger_path(h).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _order(r: Row) -> str:
    return json.dumps(r, sort_keys=True, default=str)


def derived(home: Path) -> dict[str, list[Row]]:
    """The derived rows of a profile, wall-clock columns dropped, in a fixed order."""
    conn = store.open_db(home)
    try:
        out: dict[str, list[Row]] = {}
        for t in TABLES:
            rows = [
                {k: v for k, v in r.items() if k not in CLOCK}
                for r in query(conn, f"select * from {t}")
            ]
            out[t] = sorted(rows, key=_order)
        return out
    finally:
        conn.close()


@pytest.fixture()
def built(tmp_path: Path) -> Path:
    home = tmp_path / "source"
    synthetic_home(home)
    out = tmp_path / "bench" / "fixture.tar.xz"
    m = fixture.build(home, out, since_days=30, now=NOW)
    assert m["window_start"] == NOW - 30 * DAY
    # make_home's 3 sessions and `named`; ledger lines from the window on, their two blobs, the
    # one visible skill
    assert m["counts"] == {
        "sessions": 4,
        "messages": 14,
        "ledger": 4,
        "blobs": 2,
        "skills": 1,
    }
    return out


def _later(monkeypatch: pytest.MonkeyPatch, days: float) -> None:
    real: Callable[[], float] = time.time
    monkeypatch.setattr(time, "time", lambda: real() + days * DAY)


def test_record_then_replay_gives_the_same_db_and_numbers(
    tmp_path: Path, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # record: a scratch home from the fixture, derived through the CLI path with --record
    rec_home = tmp_path / "rec"
    fixture.prepare(built, rec_home)
    tape_path = tmp_path / "bench" / "cassette.jsonl"
    model = FakeModel()
    assert cli.run_derive(cassette.Recorder(model, tape_path), rec_home, None, None, True) == 0
    assert model.calls > 0
    recorded = derived(rec_home)
    assert {r["evidence"] for r in recorded["skill_edit"]} == {"speculative", "no-run", None}
    assert len(recorded["proposal"]) == 1

    exp = tmp_path / "bench" / "expected.json"
    args = ["--fixture", str(built), "--cassette", str(tape_path), "--expected", str(exp)]
    assert check.main([*args, "--write-expected"]) == 0
    want = loads_object(exp.read_text(encoding="utf-8"))
    assert want == {
        "intents": 3,
        "proposals": 1,
        "proposals_unquoted": 1,
        "skill_edits": {"no-run": 1, "speculative": 1, "unlabelled": 2},
        "speculative_pct": 100.0,
        "runs": {"ok": 3},
        "model_calls": model.calls,
    }

    # replay a year later in another directory: the frozen window gives the same prompts
    _later(monkeypatch, 365)
    assert check.main(args) == 0
    again = tmp_path / "elsewhere" / "replayed"
    fixture.prepare(built, again)
    tape = cassette.Replayer.load(tape_path)
    assert cli.run_replay(tape, again, None, None, True) == 0
    assert (tape.replayed, tape.missed, tape.unused()) == (model.calls, [], 0)
    assert derived(again) == recorded
    assert check.numbers(again, tape.replayed) == check.numbers(rec_home, model.calls)


def test_a_difference_or_a_miss_fails_the_bench(
    tmp_path: Path, built: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec_home = tmp_path / "rec"
    fixture.prepare(built, rec_home)
    tape_path = tmp_path / "cassette.jsonl"
    assert (
        cli.run_derive(cassette.Recorder(FakeModel(), tape_path), rec_home, None, None, True) == 0
    )
    exp = tmp_path / "expected.json"
    args = ["--fixture", str(built), "--cassette", str(tape_path), "--expected", str(exp)]
    assert check.main([*args, "--write-expected"]) == 0
    want = loads_object(exp.read_text(encoding="utf-8"))
    changed: JsonObject = {**want, "intents": 4}
    exp.write_text(json.dumps(changed), encoding="utf-8")
    capsys.readouterr()
    assert check.main(args) == 1
    assert "intents: expected 4, got 3" in capsys.readouterr().err
    exp.write_text(json.dumps(want), encoding="utf-8")
    lines = tape_path.read_text(encoding="utf-8").splitlines(keepends=True)
    tape_path.write_text("".join(lines[1:]), encoding="utf-8")  # the first assign call is gone
    assert check.main(args) == 1
    assert "argus.assign" in capsys.readouterr().err
    assert check.main([*args[:2], "--cassette", str(tmp_path / "none.jsonl"), *args[4:]]) == 2


def test_the_fixture_refuses_the_real_home_and_local_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    real = tmp_path / ".hermes"
    make_home(real, NOW)
    with pytest.raises(fixture.FixtureError, match="never from a real user's"):
        fixture.build(real, tmp_path / "f.tar.xz", since_days=30, now=NOW)
    home = tmp_path / "source"
    make_home(home, NOW)
    hermes_data.ledger_path(home).parent.mkdir(parents=True)
    entry: JsonObject = {"id": "e", "ts": "2026-09-20T10:00:00", "skill": "k"}
    naive = json.dumps(entry)
    hermes_data.ledger_path(home).write_text(naive + "\n", encoding="utf-8")
    with pytest.raises(fixture.FixtureError, match="no UTC offset"):
        fixture.build(home, tmp_path / "f.tar.xz", since_days=30, now=NOW)
    with pytest.raises(fixture.FixtureError, match="no Argus history window"):
        fixture.build(home, tmp_path / "f.tar.xz", now=NOW)
    with pytest.raises(fixture.FixtureError, match="not an empty directory"):
        fixture.unpack(tmp_path / "f.tar.xz", home)


@pytest.mark.parametrize(
    "rel",
    [
        "state.db",
        "skills/.curator_ledger.jsonl",
        ".curator_backups/blobs/b0",
        "skills/k/SKILL.md",
        "skills",
    ],
)
def test_the_fixture_refuses_symlinks(tmp_path: Path, rel: str) -> None:
    """A file (or a directory on its path) linked from outside the home is never archived."""
    home = tmp_path / "source"
    synthetic_home(home)
    target, outside = home / rel, tmp_path / "outside" / Path(rel).name
    outside.parent.mkdir()
    target.rename(outside)
    target.symlink_to(outside)
    out = tmp_path / "f.tar.xz"
    with pytest.raises(fixture.FixtureError, match="is a symlink"):
        fixture.build(home, out, since_days=30, now=NOW)
    assert not out.exists()
