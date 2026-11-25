"""Proposals must quote, verbatim, the turns of the intents they cite. A quote is checked against
what the person typed (Hermes' wrappers stripped) and the tool results of its intent's turns,
casefolded and whitespace-collapsed, on word boundaries, with 3+ words of which 2+ content words.
An intent without a valid quote leaves the evidence, and a proposal left with fewer than 2 quoted
intents and no cited error pattern of 5+ hits is dropped; a deleted session takes its quotes
along. No model: the answers are scripted."""

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Final

import pytest
from test_window import quiet
from typing_extensions import override

from argus import auto, backfill, capture, derive, signals, store
from argus.dbtypes import query, scalar
from argus.hermes_data import MessageRow, Turn
from argus.jsontypes import JsonList, JsonObject, as_list, loads, loads_object

DAY: Final = 86400.0
REQUESTS: Final = (
    "please plan the garden irrigation schedule for the coming spring season",
    "draft an email to the landlord about the broken heater in the kitchen",
    "compare three laptops for video editing that fit a modest budget",
    "summarise the quarterly sales report and flag the weakest region",
    "book a table for four near the station on friday evening",
)
ERROR: Final = '{"error": "permission denied while writing /srv/shared/output.txt"}'
REPLY: Final = "I could not finish because the shared folder rejected every write attempt."
SKILL_WRAPPED: Final = (
    f'{signals.SKILL_INVOKED}"gardening" skill, indicating they want you to follow its'
    f" instructions. {signals.SKILL_LOADED}\n\n---\nname: gardening\n---\n\n"
    "Always water the roses before noon on hot days.\n\n"
    f"{signals.SKILL_INSTRUCTION}Draft an email to the landlord about the heater"
)


def _turn(sid: str, msg_id: int, text: str, ts: float) -> Turn:
    return Turn(
        profile="default",
        sid=sid,
        msg_id=msg_id,
        ts=ts,
        text=text,
        view="",
        source="cli",
        cron="",
        reply="",
        tools=0,
        calls=[],
        runtime=[],
        end=ts,
    )


def _msg(mid: int, sid: str, role: str, content: str, ts: float) -> MessageRow:
    return MessageRow(
        id=mid,
        session_id=sid,
        role=role,
        content=content,
        tool_call_id=None,
        tool_calls=None,
        tool_name="terminal" if role == "tool" else None,
        timestamp=ts,
        finish_reason=None,
    )


# intent A: one turn in s1 (a speaker prefix Hermes adds, a failing tool, the reply), then a
# background-process notice and the tool output that follows it, which is no turn's evidence;
# intent B: one turn in s2, a /skill invocation wrapping the person's instruction
# J and Z: requests in Japanese and Chinese; E and F: two requests sharing a generic phrase
JA: Final = "東京駅の近くで金曜日の夜に四人分のテーブルを予約してください"
ZH: Final = "请帮我规划春季花园的灌溉时间表并提醒我"
SHARED: Final = "check current status of the deployment pipeline"
TURNS: Final = {
    "A": [_turn("s1", 1, "Plan the garden irrigation schedule please", 1.0)],
    "B": [_turn("s2", 7, "the cleaned skill message", 5.0)],
    "J": [_turn("s3", 8, JA, 6.0)],
    "Z": [_turn("s4", 9, ZH, 7.0)],
    "E": [_turn("s5", 10, "e", 8.0)],
    "F": [_turn("s6", 11, "f", 9.0)],
}
MESSAGES: Final = [
    _msg(1, "s1", "user", "[albertodvp] Plan the   garden\nirrigation schedule please", 1.0),
    _msg(2, "s1", "tool", ERROR, 2.0),
    _msg(3, "s1", "assistant", REPLY, 3.0),
    _msg(4, "s1", "user", "[IMPORTANT: Background process proc_1 finished]", 4.0),
    _msg(5, "s1", "tool", "background job wrote the quarterly totals to disk", 4.5),
    _msg(7, "s2", "user", SKILL_WRAPPED, 5.0),
    _msg(8, "s3", "user", JA, 6.0),
    _msg(9, "s4", "user", ZH, 7.0),
    _msg(10, "s5", "user", f"Please {SHARED} for the billing service", 8.0),
    _msg(11, "s6", "user", f"Then {SHARED} again for the search frontend", 9.0),
]
MSGS: Final = derive.turn_messages(MESSAGES, [t for tt in TURNS.values() for t in tt])
SOURCES: Final = {k: derive.quote_sources(tt, MSGS) for k, tt in TURNS.items()}


def _check(intent: str, text: str, cited: tuple[str, ...] = ("A", "B")) -> list[derive.Quote]:
    kept, bad = derive.valid_quotes([{"intent": intent, "text": text}], cited, SOURCES)
    assert bad == (0 if kept else 1)
    return kept


def test_normal_form() -> None:
    assert derive.qnorm('  "Plan THE\tgarden\n irrigation…" ') == "plan the garden irrigation"
    assert derive.qnorm("...the heater.") == "the heater"


def test_an_exact_quote_is_valid_with_its_location() -> None:
    [q] = _check("A", "the garden irrigation schedule")
    assert q == {
        "intent": "A",
        "text": "the garden irrigation schedule",
        "session_id": "s1",
        "message_id": 1,
        "ts": 1.0,
        "role": "user",
    }


def test_case_and_whitespace_do_not_matter() -> None:
    assert _check("A", "PLAN the garden\n\n   IRRIGATION")
    assert _check("B", "“draft an email to the LANDLORD”")


def test_the_whole_stored_tool_result_counts_not_only_the_excerpt() -> None:
    [q] = _check("A", "permission denied while writing")
    assert (q["role"], q["message_id"]) == ("tool", 2)


def test_a_quote_of_an_assistant_reply_is_invalid() -> None:
    """Evidence is what the model did not write: the user's messages and tool results."""
    assert not _check("A", "the shared folder rejected every write attempt")


def test_a_turn_ends_at_any_user_message() -> None:
    """A background-process notice starts no turn, but what follows it is not the turn's."""
    assert [m["id"] for m in MSGS[("s1", 1)]] == [1, 2, 3]
    assert not _check("A", "background job wrote the quarterly totals")


def test_only_what_the_person_typed_counts() -> None:
    """Hermes' wrappers (a /skill body, a speaker prefix) are not the request."""
    assert signals.human_text(SKILL_WRAPPED) == "Draft an email to the landlord about the heater"
    assert not _check("B", "always water the roses before noon")
    assert not _check("B", "they want you to follow its instructions")
    assert not _check("A", "[albertodvp] plan the garden")
    assert _check("B", "an email to the landlord")


def test_a_skill_body_mentioning_a_bundle_is_still_a_single_skill() -> None:
    """The kind of invocation is read from Hermes' header line only, not from the skill body."""
    body = SKILL_WRAPPED.replace(
        "Always water",
        "Part of the garden skill bundle, a list:\nUser instruction: no. Always water",
    )
    assert signals.human_text(body) == "Draft an email to the landlord about the heater"
    bundle = (
        f'{signals.SKILL_INVOKED}"garden" skill bundle, loading 2 skills together. Treat every'
        " skill below as active guidance for this turn.]\n\nSkills loaded: a, b\n\n"
        "User instruction: water the roses before noon\n\n[Loaded as part of the garden"
        " bundle]\nUser instruction: not this one"
    )
    assert signals.human_text(bundle) == "water the roses before noon"


def test_hermes_envelopes_without_a_request_count_for_nothing() -> None:
    bare = f'{signals.SKILL_INVOKED}"gardening" skill, ... {signals.SKILL_LOADED}\n\nbody'
    assert signals.human_text(bare) is None
    cron = f"{signals.CRON}. Do the thing."
    assert signals.human_text(cron) is None
    assert (
        signals.human_text(
            f"{signals.SKILL_INVOKED}x {signals.SKILL_LOADED}\n\n{signals.SKILL_INSTRUCTION}{cron}"
        )
        is None
    )
    assert signals.human_text("[SYSTEM: compacting]") is None


def test_a_quote_must_start_and_end_on_word_boundaries() -> None:
    assert not _check("A", "lan the garden irrigation")
    assert not _check("A", "plan the garden irrigat")
    assert _check("A", "permission denied while writing /srv")  # ends before the "/"


def test_a_short_or_generic_quote_is_invalid() -> None:
    assert not _check("A", "the garden")  # under 12 characters
    assert not _check("A", '"…plan…"')
    assert not derive.wordy("schedule please")  # 2 words
    assert not derive.wordy("it is in the")  # no content word
    assert not derive.wordy("please do it now")  # one content word (please is a stopword)
    assert derive.wordy("plan the garden")


def test_scripts_without_spaces_count_characters_not_words() -> None:
    assert _check("J", "金曜日の夜に四人分のテーブル", cited=("J",))  # mid-sentence: no boundary
    assert _check("Z", "规划春季花园的灌溉时间表", cited=("Z",))
    assert not _check("J", "東京駅の近く", cited=("J",))  # 6 characters
    assert derive.wordy("四人分のテーブルを予約")
    assert not derive.wordy("予約してくだ")  # 6 CJK characters
    assert derive.wordy("review deployment status 東京 now")  # mixed script: the word rule passes
    assert not derive.wordy("status 東京")  # neither rule


def test_a_phrase_shared_by_two_cited_intents_backs_neither() -> None:
    kept, bad = derive.valid_quotes(
        [{"intent": "E", "text": SHARED}, {"intent": "F", "text": SHARED}], ["E", "F"], SOURCES
    )
    assert (kept, bad) == ([], 2)
    assert _check("E", SHARED, cited=("E",))  # not shared with another cited intent
    assert _check("E", "status of the deployment pipeline for the billing", cited=("E", "F"))


def test_a_quote_of_another_intents_turns_is_invalid() -> None:
    assert not _check("B", "the garden irrigation schedule")
    assert not _check("A", "an email to the landlord")


def test_a_fabricated_or_paraphrased_quote_is_invalid() -> None:
    assert not _check("A", "the user wants a watering plan for spring")
    assert not _check("A", "plan the garden watering schedule")  # one word changed


def test_a_quote_of_an_intent_the_proposal_does_not_cite_is_invalid() -> None:
    assert not _check("B", "an email to the landlord", cited=("A",))


def test_repeats_count_once_and_at_most_three_per_intent_are_kept() -> None:
    texts = [
        "plan the garden",
        "PLAN THE GARDEN",
        "the garden irrigation",
        "irrigation schedule please",
        "permission denied while writing",
    ]
    kept, bad = derive.valid_quotes([{"intent": "A", "text": t} for t in texts], ["A"], SOURCES)
    assert bad == 0
    assert [q["text"] for q in kept] == texts[:1] + texts[2:4]


def test_excerpts_stay_within_their_budget_and_end_on_a_word() -> None:
    long = "Plan the garden irrigation schedule please " * 20
    msgs = {("s1", 1): [_msg(1, "s1", "user", long, 1.0), _msg(2, "s1", "tool", ERROR, 2.0)]}
    tt = TURNS["A"]
    lines = derive.excerpt_lines("A", tt, msgs, derive.INTENT_BUDGET)
    assert [line.split(": ", 1)[0] for line in lines] == ["  [A] user", "  [A] tool terminal error"]
    assert all(len(line.split(": ", 1)[1]) <= derive.EXCERPT_CHARS for line in lines)
    assert lines[0].split(": ", 1)[1] in derive.flat(long)
    assert derive.flat(long).startswith(lines[0].split(": ", 1)[1] + " ")
    assert sum(len(line) + 1 for line in lines) <= derive.INTENT_BUDGET
    small = derive.excerpt_lines("A", tt, msgs, 60)
    assert sum(len(line) + 1 for line in small) <= 60
    assert derive.excerpt_lines("A", tt, msgs, 20) == []
    [wrapped] = derive.excerpt_lines("B", TURNS["B"], MSGS, derive.INTENT_BUDGET)
    assert wrapped == "  [B] user: Draft an email to the landlord about the heater"


# ---------------------------------------------------------------- a deleted session
def _evidence(quotes: list[tuple[str, str]], hits: int = 0) -> str:
    """proposals/v2 evidence citing the intents of `quotes` ((intent, session) pairs)."""
    ev = derive.Evidence(
        intents=list(dict.fromkeys((i for i, _s in quotes), True)),
        quotes=[
            derive.Quote(
                intent=i, text=f"words of {i}", session_id=s, message_id=1, ts=0.0, role="user"
            )
            for i, s in quotes
        ],
        error_sigs=["sig"] if hits else [],
        pattern_hits=hits,
        overlaps=[],
    )
    return json.dumps(ev)


def test_deleting_a_session_scrubs_its_quotes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    conn = store.open_db(home)
    try:
        rows = {
            "two": _evidence([("A", "s1"), ("B", "s2")]),  # falls below the bar
            "pat": _evidence([("A", "s1"), ("B", "s2")], hits=7),  # still backed by its pattern
            "three": _evidence([("A", "s1"), ("B", "s2"), ("C", "s3"), ("B", "s1")]),
            "other": _evidence([("C", "s3"), ("D", "s4")]),  # another session: untouched
            "v1": '{"intents": ["A", "B"], "error_sigs": [], "overlaps": []}',
        }
        for pid, ev in rows.items():
            conn.execute(
                "insert into proposal (id, skill_name, rule, evidence, derived_by)"
                " values (?, 'sk', ?, ?, 'test')",
                (pid, f"rule {pid}", ev),
            )
        ids: list[str] = [*rows]
        store.meta_set(conn, auto.LAST_PASS, json.dumps(ids))
        assert derive.scrub_sessions(conn, {"s1"}) == 3
        conn.commit()
        got = {
            str(r["id"]): loads_object(str(r["evidence"]))
            for r in query(conn, "select id, evidence from proposal")
        }
        assert store.meta_get(conn, auto.LAST_PASS) == '["pat", "three", "other", "v1"]'
    finally:
        conn.close()
    assert got["two"]["intents"] == ["B"]
    assert [
        q["session_id"] for q in as_list(got["two"]["quotes"]) or [] if isinstance(q, dict)
    ] == ["s2"]
    assert got["pat"]["intents"] == ["B"]
    assert got["three"]["intents"] == ["B", "C"]  # B keeps its quote from s2
    assert got["other"] == loads_object(rows["other"])
    assert got["v1"] == loads_object(rows["v1"])
    assert "s1" not in json.dumps(got)


# ---------------------------------------------------------------- the gate, end to end
def _home(home: Path, now: float) -> None:
    """A state.db with one session per request: the request, a tool call failing with the same
    error each time (one error pattern, 5 hits), and a reply."""
    home.mkdir(parents=True)
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.executescript(
            "create table sessions (id text primary key, source text not null, title text,"
            " parent_session_id text, started_at real not null, ended_at real, end_reason text,"
            " message_count integer, tool_call_count integer, profile_name text, model text);"
            "create table messages (id integer primary key autoincrement,"
            " session_id text not null, role text not null, content text, tool_call_id text,"
            " tool_calls text, tool_name text, timestamp real not null, finish_reason text);"
        )
        for n, text in enumerate(REQUESTS):
            sid, t = f"s{n}", now - (len(REQUESTS) - n) * DAY
            conn.execute(
                "insert into sessions values (?, 'cli', ?, null, ?, ?, null, 4, 1, null, null)",
                (sid, f"session {n}", t, t + 60),
            )
            call = f'[{{"id": "c{n}", "type": "function", "function": {{"name": "terminal"}}}}]'
            q = (
                "insert into messages (session_id, role, content, tool_call_id, tool_calls,"
                " tool_name, timestamp, finish_reason) values (?, ?, ?, ?, ?, ?, ?, ?)"
            )
            conn.execute(q, (sid, "user", text, None, None, None, t + 1, None))
            conn.execute(q, (sid, "assistant", None, None, call, None, t + 2, "tool_calls"))
            conn.execute(q, (sid, "tool", ERROR, f"c{n}", None, "terminal", t + 3, None))
            conn.execute(q, (sid, "assistant", f"{REPLY} ({n})", None, None, None, t + 4, "stop"))
        conn.commit()
    finally:
        conn.close()


EXCERPT: Final = re.compile(r"^  \[([^\]]+)\] user: (.*)$", re.M)


class QuotingLlm:
    """Every intent new, judged partial; proposals quoting the excerpts of the prompt."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
        if purpose == "argus.assign":
            return {"turns": [{"turn": "T1", "intent": "NEW", "title": "a request"}]}
        if purpose == "argus.judge":
            return {"status": "partial", "friction": "the write kept failing"}
        if purpose != "argus.proposals":
            return None
        self.prompts.append(text)
        ex: dict[str, str] = {m.group(1): m.group(2) for m in EXCERPT.finditer(text)}
        a, b, c = sorted(ex)[:3]
        hit = re.search(r"^(\d+): .* — 5 hits", text, re.M)
        assert hit is not None
        pattern = int(hit.group(1))

        def prop(n: int, intents: list[str], quotes: JsonList, patterns: list[int]) -> JsonObject:
            return {
                "skill": f"new:skill-{n}",
                "rule": f"Rule {n}: check the target folder can be written first.",
                "why": "w",
                "intents": [*intents],
                "quotes": quotes,
                "patterns": [*patterns],
                "overlaps": [],
                "confidence": "high",
            }

        def words(t: str) -> str:
            return " ".join(t.split()[:6])  # a whole-word slice

        def q(i: str, t: str) -> JsonObject:
            return {"intent": i, "text": t}

        return {
            "proposals": [
                # kept: 2 intents quoted (one exactly, one with case and spacing changed)
                prop(1, [a, b], [q(a, words(ex[a])), q(b, "  " + words(ex[b]).upper())], []),
                # dropped: the second quote is made up
                prop(2, [a, b], [q(a, ex[a]), q(b, "the user asked for something else")], []),
                # dropped: the second quote comes from another intent's turns
                prop(3, [a, b], [q(a, ex[a]), q(b, ex[c])], []),
                # dropped: the second quote is too short
                prop(4, [a, b], [q(a, ex[a]), q(b, ex[b][:8])], []),
                # dropped: one quoted intent and no pattern
                prop(5, [a], [q(a, ex[a])], []),
                # kept: one quoted intent, but a cited pattern of 5 hits; its tool result quoted
                prop(6, [a, b], [q(a, words(ex[a]))], [pattern]),
                # dropped: the second quote is the assistant's reply, not evidence
                prop(7, [a, b], [q(a, ex[a]), q(b, "the shared folder rejected every write")], []),
            ]
        }


def test_unquoted_proposals_are_dropped_and_counted(tmp_path: Path) -> None:
    h = tmp_path / "hermes"
    _home(h, time.time())
    backfill.run(h, since_days=0, log=quiet)
    llm, lines = QuotingLlm(), list[str]()
    report = derive.run(h, llm, stages=["assign", "judge", "proposals"], log=lines.append)
    stats = report["profiles"]["default"]["stages"]["proposals"]
    assert (stats["proposed"], stats["unquoted"], stats["quotes_invalid"]) == (2, 5, 4), stats
    assert sum("argus: proposals: dropped" in x for x in lines) == 5
    [prompt] = llm.prompts
    assert sum(1 for _m in EXCERPT.finditer(prompt)) == len(REQUESTS)
    assert "tool terminal error: " + ERROR in prompt
    conn = store.open_db(h)
    try:
        rows = {
            str(r["rule"])[:6]: loads_object(str(r["evidence"]))
            for r in query(conn, "select rule, evidence from proposal")
        }
        assert set(rows) == {"Rule 1", "Rule 6"}
        assert {str(scalar(conn, "select derived_by from proposal where rule like 'Rule 1%'"))} == {
            derive.V_PROP
        }
        ids = as_list(loads(store.meta_get(conn, auto.LAST_PASS) or "null"))
        assert ids is not None
        assert len(ids) == 2
    finally:
        conn.close()
    one, six = rows["Rule 1"], rows["Rule 6"]
    q1 = as_list(one["quotes"]) or []
    assert len(as_list(one["intents"]) or []) == 2
    assert len(q1) == 2
    assert all(isinstance(q, dict) and q["role"] == "user" for q in q1)
    # 6: the intent without a quote left the evidence
    assert len(as_list(six["intents"]) or []) == 1
    assert len(as_list(six["quotes"]) or []) == 1
    assert six["error_sigs"]


class DeletingLlm(QuotingLlm):
    """Two proposals citing the first two intents, quoted: one for a new skill backed also by the
    error pattern, then one patching `sk`. While its patch is asked for (after the proposals
    answer and the quote check, before its insert), the second intent's session is deleted."""

    def __init__(self, home: Path) -> None:
        super().__init__()
        self.home, self.deleted = home, ""

    @override
    def __call__(self, instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
        if purpose == "argus.patch":
            capture.on_session_delete(deleted_ids=[self.deleted])
            assert store.writer(self.home).flush()
            return {"after_line": 1, "lines": ["- Check the folder first."]}
        if purpose != "argus.proposals":
            return super().__call__(instructions, text, schema, purpose)
        ex: dict[str, str] = {m.group(1): m.group(2) for m in EXCERPT.finditer(text)}
        a, b = sorted(ex)[:2]
        self.deleted = f"s{REQUESTS.index(ex[b])}"
        hit = re.search(r"^(\d+): .* — 5 hits", text, re.M)
        assert hit is not None
        quotes: JsonList = [{"intent": a, "text": ex[a]}, {"intent": b, "text": ex[b]}]
        return {
            "proposals": [
                {
                    "skill": skill,
                    "rule": f"Rule {n}: check the folder first.",
                    "why": "w",
                    "intents": [a, b],
                    "quotes": quotes,
                    "patterns": patterns,
                    "overlaps": [],
                    "confidence": "high",
                }
                for n, skill, patterns in ((1, "new:fresh", [int(hit.group(1))]), (2, "sk", []))
            ]
        }


def test_a_session_deleted_during_the_pass_does_not_come_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deletion lands after the proposals answer: the proposal already inserted is scrubbed,
    the one inserted later drops the deleted session's quote under the write lock, and falls
    below the bar; the latest pass lists only what is still backed."""
    h = tmp_path / "hermes"
    _home(h, time.time())
    md = h / "skills" / "sk" / "SKILL.md"
    md.parent.mkdir(parents=True)
    md.write_text("# sk\n\nStep one.\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(h))
    backfill.run(h, since_days=0, log=quiet)
    llm, lines = DeletingLlm(h), list[str]()
    report = derive.run(h, llm, stages=["assign", "judge", "proposals"], log=lines.append)
    stats = report["profiles"]["default"]["stages"]["proposals"]
    assert (stats["proposed"], stats["unquoted"], stats["patched"]) == (1, 1, 1), stats
    assert any("a quoted session was deleted" in x for x in lines)
    conn = store.open_db(h)
    try:
        [(rule, evidence)] = [
            (str(r["rule"]), str(r["evidence"]))
            for r in query(conn, "select rule, evidence from proposal")
        ]
        ev = derive.EVIDENCE_V2.validate_json(evidence)
        last = store.meta_get(conn, auto.LAST_PASS)
        dead = derive.tombstones(conn)
    finally:
        conn.close()
    assert rule.startswith("Rule 1")
    assert llm.deleted in dead
    assert [q["session_id"] for q in ev["quotes"]] != []
    assert llm.deleted not in {q["session_id"] for q in ev["quotes"]}
    assert len(ev["intents"]) == 1
    assert last is not None
    assert len(as_list(loads(last)) or []) == 1
