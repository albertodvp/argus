"""derive.estimate counts the model calls of the next derive without making any: exactly what
assign and edit_review send to the model, plus the intents and runs already due for judging;
backfill and status print it, with a warning above derive.LARGE calls."""

import json
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_window import DAY, make_home, quiet

from argus import backfill, cli, derive, hermes_data
from argus.jsontypes import JsonObject

PATH = "/h/skills/k/SKILL.md"


class Llm:
    """Counts the calls by purpose and answers none of them: every unit of work stays pending."""

    def __init__(self) -> None:
        self.purposes: Counter[str] = Counter()

    def __call__(self, instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
        self.purposes[purpose] += 1
        return None


def add_session(
    home: Path, sid: str, source: str, at: float, texts: tuple[str, ...], title: str = ""
) -> None:
    """One more session in make_home's state.db: each text a user turn with a reply."""
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute(
            "insert into sessions values (?, ?, ?, null, ?, ?, null, ?, 0, null, null)",
            (sid, source, title or sid, at, at + 60, 2 * len(texts)),
        )
        q = (
            "insert into messages (session_id, role, content, timestamp, finish_reason)"
            " values (?, ?, ?, ?, ?)"
        )
        for n, text in enumerate(texts):
            conn.execute(q, (sid, "user", text, at + 10 * n + 1, None))
            conn.execute(q, (sid, "assistant", f"reply {n}", at + 10 * n + 2, "stop"))
        conn.commit()
    finally:
        conn.close()


def write_ledger(home: Path, edits: list[tuple[str, str, str, str | None, str]]) -> None:
    """Ledger entries (id, actor, action, session id, sha of the new SKILL.md), half a day ago."""
    blobs = home / ".curator_backups" / "blobs"
    blobs.mkdir(parents=True)
    for sha, text in (("b0", "# skill\nold\n"), ("a0", "# skill\nnew\n")):
        (blobs / sha).write_text(text, encoding="utf-8")
    when = datetime.fromtimestamp(time.time() - 0.5 * DAY, UTC).isoformat()
    lines = [
        {
            "id": eid,
            "ts": when,
            "actor": actor,
            "action": action,
            "skill": "k",
            "evidence": {"session_id": sid} if sid else {},
            "before": [{"path": PATH, "sha256": "b0"}],
            "after": [{"path": PATH, "sha256": after}],
        }
        for eid, actor, action, sid, after in edits
    ]
    hermes_data.ledger_path(home).parent.mkdir(parents=True)
    hermes_data.ledger_path(home).write_text(
        "".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8"
    )


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """make_home's three sessions (one model call each) plus: a fourth session the model decides
    with a follow-up the rules assign; a repeat of s1's request; a cron run; a session older than
    the window. Edits of every edit_review kind."""
    h, now = tmp_path / "hermes", time.time()
    make_home(h, now)
    add_session(
        h,
        "s0b",
        "cli",
        now - 0.9 * DAY,
        ("book a plumber for the bathroom leak next tuesday morning", "ok thanks"),
    )
    add_session(
        h,
        "s3",
        "cli",
        now - 0.8 * DAY,
        ("draft an email to the landlord about the broken heater in the kitchen",),
    )
    add_session(h, "s4", "cron", now - 0.7 * DAY, ("run the nightly report",), "nightly report")
    add_session(h, "old", "cli", now - 40 * DAY, ("an old request from before the window",))
    write_ledger(
        h,
        [
            ("run", "agent", "patch", "s2", "a0"),  # judged on its run
            ("review", "curator", "patch", "s2", "a0"),  # judged on its run
            ("gone", "curator", "patch", "gone", "a0"),  # no run: the diff alone
            ("nosession", "curator", "patch", None, "a0"),  # no run: the diff alone
            ("undo", "user", "rollback", None, "a0"),  # a person's: not reviewed
            ("manual", "user", "patch", None, "a0"),  # a person's: not reviewed
            ("same", "agent", "patch", "s2", "b0"),  # no diff
            ("earlier", "agent", "patch", "old", "a0"),  # its run precedes the window: waits
        ],
    )
    return h


def test_estimate_matches_the_calls_derive_makes(home: Path) -> None:
    assert derive.estimate(home) is None  # never backfilled: derive skips the profile
    backfill.run(home, since_days=30, log=quiet)

    est = derive.estimate(home)
    assert est is not None
    known = (est["assign"], est["review"], est["learn"], est["unread"], est["due"], est["calls"])
    assert known == (4, 2, 2, 0, 0, 8)  # assign, review, learn, unread, due, calls
    llm = Llm()
    derive.run(home, llm, log=quiet)
    assert llm.purposes["argus.assign"] == est["assign"]
    assert llm.purposes["argus.edit_review"] == est["review"]
    assert llm.purposes["argus.edit_learned"] == est["learn"]
    # the cron run's routine, made during the pass: inside the bound for the new turns
    assert llm.purposes["argus.judge"] == 1 <= est["new"]

    # nothing was answered: the same turns and edits wait, and the routine is now due
    est = derive.estimate(home)
    assert est is not None
    known = (est["assign"], est["review"], est["learn"], est["unread"], est["due"], est["calls"])
    assert known == (4, 2, 2, 0, 1, 9)
    llm = Llm()
    derive.run(home, llm, log=quiet)
    assert sum(llm.purposes.values()) == est["calls"]
    assert llm.purposes["argus.judge"] + llm.purposes["argus.runs"] == est["due"]


def test_ledger_entries_after_the_backfill_count_as_unread(home: Path) -> None:
    # derive catches up with them first, so the next pass reviews them
    backfill.run(home, since_days=30, log=quiet)
    entry: JsonObject = {
        "id": "later",
        "ts": datetime.now(UTC).isoformat(),
        "actor": "agent",
        "action": "patch",
        "skill": "k",
        "evidence": {},
        "before": [{"path": PATH, "sha256": "b0"}],
        "after": [{"path": PATH, "sha256": "a0"}],
    }
    with hermes_data.ledger_path(home).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    est = derive.estimate(home)
    assert est is not None
    assert est["unread"] == 1
    assert est["most"] >= est["calls"] + 1


def test_estimate_is_read_only(home: Path) -> None:
    backfill.run(home, since_days=30, log=quiet)
    db = home / "plugin-data" / "argus" / "data.db"
    before = (db.read_bytes(), (home / "state.db").read_bytes())
    derive.estimate(home)
    assert (db.read_bytes(), (home / "state.db").read_bytes()) == before


def test_backfill_and_status_print_the_estimate(home: Path) -> None:
    logs: list[str] = []
    backfill.run(home, since_days=30, log=logs.append)
    assert logs[-1].startswith("default: next derive: about 8 model calls known now, up to")
    assert not any("warning" in x for x in logs)
    assert "next derive: about 8 model calls" in cli.status(home, home)


def test_warning_above_the_threshold(tmp_path: Path) -> None:
    h = tmp_path / "hermes"
    make_home(h, time.time())
    n = derive.LARGE + 1
    write_ledger(h, [(f"e{i}", "curator", "patch", None, "a0") for i in range(n)])

    logs: list[str] = []
    backfill.run(h, since_days=3, log=logs.append)
    assert f"default: next derive: about {n + 1} model calls" in logs[1]
    assert logs[2].startswith(
        "default: warning: this can be a large pass on the auxiliary.argus model, over the"
        " window this backfill set; a smaller --since keeps it cheaper"
    )
    assert "cheaper model for auxiliary.argus" in logs[3]

    logs.clear()
    backfill.run(h, since_days=7, log=logs.append)
    assert "over the window this backfill widened" in logs[2]
    logs.clear()
    backfill.run(h, log=logs.append)  # the window stays: still large, said without blame
    assert logs[2].startswith(
        "default: warning: this can be a large pass on the auxiliary.argus model;"
    )
    assert "warning: this can be a large pass" in cli.status(h, h)

    below = derive.Estimate(
        assign=0,
        review=derive.LARGE,
        learn=0,
        unread=0,
        due=0,
        calls=derive.LARGE,
        new=0,
        most=derive.LARGE,
    )
    assert len(derive.estimate_lines(below)) == 1


class OneIntentLlm:
    """Puts every new turn in the first candidate intent (a new one when there is none), so one
    intent collects every session and its runs get judged; answers nothing else usefully."""

    def __init__(self) -> None:
        self.purposes: Counter[str] = Counter()

    def __call__(self, instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
        self.purposes[purpose] += 1
        if purpose != "argus.assign":
            return {}
        head, _, body = text.partition("NEW TURNS")
        cands = [ln.split(":", 1)[0] for ln in head.splitlines()[1:] if ": " in ln]
        turns = [w for ln in body.splitlines() if (w := ln.split(" ", 1)[0])[1:].isdigit()]
        pick = cands[0] if cands else "NEW"
        answer: JsonObject = {
            "turns": [{"turn": t, "intent": pick, "title": "one goal"} for t in turns]
        }
        return answer


def test_the_pass_stays_within_the_upper_bound(home: Path) -> None:
    # the first derive judges the intents its own turns open, and the runs of one with 3+
    # sessions: none of it is known before assign, all of it must fit the printed bound
    backfill.run(home, since_days=30, log=quiet)
    est = derive.estimate(home)
    assert est is not None
    llm = OneIntentLlm()
    derive.run(home, llm, log=quiet)
    made = sum(llm.purposes.values())
    assert llm.purposes["argus.runs"] >= 3  # the gap the old estimate did not count
    assert est["calls"] < made <= est["most"]
