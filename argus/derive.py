"""Derivation passes over one Hermes install, per profile (README "Intents" and "Learning").

Stages, in order: assign, consolidate, judge, runs, edit_review, proposals. They read state.db and
the curator ledger through hermes_data (read-only) and write only layer 2 of the plugin DB plus the
derived columns of skill_edit. Every model call goes through the injected `llm(instructions, text,
schema, purpose) -> dict | None`; a failed call is logged and its unit of work skipped, never fatal.
Each stage commits on its own and every run writes a report to
<home>/plugin-data/argus/runs/<utc>.json. `estimate` counts the model calls of a profile's next
run by the same rules, without making any (backfill and status print it).

A model answer is validated once, when it arrives (`Calls.ask`), into the types of its prompt's
schema: an answer that does not fit is a failed call, like no answer, and only its unit of work is
skipped. A missing field gets the stage's fallback; an enum field keeps only the schema's values.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import traceback
from collections.abc import Callable, Collection, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, NotRequired, TypeAlias, TypeGuard, TypeVar

from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypedDict

from . import auto, hermes_data, prompts, signals, store
from .dbtypes import (
    INTENT_ROW,
    RUN_ROW,
    SKILL_EDIT_ROW,
    IntentRow,
    RunRow,
    SkillEditRow,
    query_as,
    query_one,
)
from .hermes_api import LlmFn
from .hermes_data import Dump, LedgerEntry, MessageRow, Session, Turn
from .jsontypes import JsonObject, as_number, as_object, loads, loads_object

T = TypeVar("T")

STAGES: Final = ("assign", "consolidate", "judge", "runs", "edit_review", "proposals")
V_ASSIGN: Final = "assign/v1"
V_CONS: Final = "consolidate/v1"
V_JUDGE: Final = "judge/v1"
V_RUNS: Final = "runs/v1"
V_REVIEW: Final = "edit_review/v2"
V_PROP: Final = "proposals/v1"

SHORT_FOLLOWUP: Final = re.compile(
    r"^(ok|okay|si|sì|yes|y|vai|go|procedi|proceed|perfetto|perfect|autorizzo|invia|continua|continue|"
    r"confermo|confirmed|fallo|do it|esatto|grazie|thanks|bene|good)\b",
    re.I,
)
RECENT: Final = 20  # assign: candidate list size (recently active + keyword matches)
OLDER: Final = 10
CONS_CHUNK: Final = 120  # consolidate: intents per call
WORD: Final = re.compile(r"[a-zà-ÿ0-9_\-]{4,}")
STOP: Final = {
    "this",
    "that",
    "with",
    "from",
    "have",
    "what",
    "when",
    "your",
    "please",
    "about",
    "there",
    "which",
    "would",
    "could",
    "should",
    "them",
    "then",
    "than",
    "will",
    "into",
    "also",
    "just",
    "make",
    "some",
    "more",
    "only",
    "like",
    "want",
    "need",
    "here",
    "were",
    "been",
    "does",
    "done",
    "each",
    "other",
    "after",
    "before",
    "again",
    "still",
    "sono",
    "della",
    "delle",
    "degli",
    "questo",
    "questa",
    "come",
    "fare",
    "anche",
    "perché",
    "dove",
    "quando",
    "tutti",
    "tutte",
    "nella",
    "nelle",
    "alla",
    "allo",
    "agli",
    "cosa",
    "ogni",
    "deve",
    "devi",
    "puoi",
    "dopo",
    "prima",
    "ancora",
    "solo",
    "molto",
}
KINDS: Final = ("task", "probe", "chat")
WHOS: Final = ("real-user", "team", "automated-test", "scheduled")
NO_DIFF_ACTIONS: Final = ("archive", "delete", "remove_file", "purge")
EVIDENCE: Final = ("speculative", "user-correction", "observed-failure", "observed-success")
NO_RUN: Final = "no-run"  # set by edit_review, not the model: state.db holds no run for the edit
CONFIDENCE: Final = ("high", "medium", "low")
STATUSES: Final = ("achieved", "partial", "failed", "unclear")
STABLES: Final = ("stable", "unstable", "single-run")
# never backfilled: no window bounds the history yet, and a whole long history costs thousands of
# model calls (the idle trigger and dashboard refresh land here too)
UNBACKFILLED: Final = "no history window: run `hermes argus backfill --since DAYS` first"
LARGE: Final = 200  # model calls of one derive above which backfill and status warn

TurnKey: TypeAlias = tuple[str, int]
"""(session id, state.db id of the user message): one turn."""

Ref: TypeAlias = str | tuple[str, TurnKey]
"""assign: an intent id, or ('?', turn key) while the model decides that turn."""

Stats: TypeAlias = dict[str, int]
"""What one stage did (counts)."""

StageReport: TypeAlias = dict[str, int | float | str]
"""A stage's Stats plus llm_calls, llm_failures and seconds; {error: repr} when it raised."""

Gate: TypeAlias = Literal["cron", "follow-up", "repeat", "llm"]
"""assign: the rule that assigns a turn; `llm` leaves it to the model."""

ReviewStep: TypeAlias = Literal[
    "no_diff", "by_person", "skipped", "relabel_no_run", "no_run", "relabel", "review"
]
"""edit_review: what one pending edit gets; `no_run` and `review` are one model call each."""

Log: TypeAlias = Callable[[str], object]


class ProfileReport(TypedDict):
    home: str
    stages: dict[str, StageReport]
    error: NotRequired[str]
    seconds: NotRequired[float]
    window_start: NotRequired[float]  # the profile's history window, when backfill set one
    skipped: NotRequired[str]  # why the profile was not derived (never backfilled)


class RunReport(TypedDict):
    started_at: str
    stages: list[str]
    profiles: dict[str, ProfileReport]
    llm_calls: NotRequired[int]
    llm_failures: NotRequired[int]
    seconds: NotRequired[float]
    aborted: NotRequired[str]  # why the run stopped early: the model kept raising


class ErrorPattern(TypedDict):
    tool: str
    sig: str
    cat: signals.ErrCategory
    hits: int
    sessions: set[str]
    intents: dict[str, int]


class EditStep(TypedDict):
    edit: SkillEditRow
    step: ReviewStep
    sid: str  # the edit's session ('' when none); for `review`, one whose messages are loaded
    head: str  # model input: the skill, actor and action
    diff: str  # model input: the diff


class Estimate(TypedDict):
    """The model calls of a profile's next derive, counted without any (see `estimate`)."""

    assign: int  # sessions with turns the free rules leave to the model: one call each
    review: int  # skill edits judged on their run: one call each
    learn: int  # skill edits whose run state.db does not hold (no-run): one call each, on the diff
    unread: int  # ledger entries derive reads first (not backfilled yet): up to one call each
    due: int  # intents and runs already due for judging, before the new turns land in intents
    calls: int  # assign + review + learn + due: the calls known now
    new: int  # up to: judging the intents the new turns land in, and their runs
    most: int  # calls + unread + new + consolidate and proposals: what the pass can reach


class _TurnLink(TypedDict):
    session_id: str
    user_message_id: int
    intent_id: str


class _TurnKeyRow(TypedDict):
    session_id: str
    user_message_id: int


class _LearnedRow(TypedDict):
    ledger_entry_id: str
    skill_name: str
    learned: str | None


class _AppliedRuleRow(TypedDict):
    ledger_entry: str
    skill_name: str
    rule: str


class _EditIdRow(TypedDict):
    ledger_entry_id: str


class _ProposalDiffRow(TypedDict):
    id: str
    diff: str | None


TURN_LINK: Final = TypeAdapter(_TurnLink)
TURN_KEY_ROW: Final = TypeAdapter(_TurnKeyRow)
LEARNED_ROW: Final = TypeAdapter(_LearnedRow)
APPLIED_RULE_ROW: Final = TypeAdapter(_AppliedRuleRow)
EDIT_ID_ROW: Final = TypeAdapter(_EditIdRow)
PROPOSAL_DIFF_ROW: Final = TypeAdapter(_ProposalDiffRow)


# ---------------------------------------------------------------- model answers
# The types of prompts.*_SCHEMA. Every field is optional here (a missing one gets the stage's
# fallback); enum fields are text, each stage keeps only the values its schema lists.
class _AssignTurn(TypedDict, total=False):
    turn: str
    intent: str
    title: str
    goal: str
    kind: str
    who: str


class _AssignAnswer(TypedDict, total=False):
    turns: list[_AssignTurn]


_ConsOp = TypedDict(
    "_ConsOp",
    {
        "op": str,
        "keep": str,
        "drop": str,
        "from": str,
        "turns": list[str],
        "id": str,
        "title": str,
        "goal": str,
    },
    total=False,
)


class _ConsAnswer(TypedDict, total=False):
    ops: list[_ConsOp]


class _Judgement(TypedDict, total=False):
    status: str
    who: str
    stable: str
    risk: str
    headline: str
    outcome: str
    consistency: str
    friction: str
    next: str
    attention: int


class _RunAnswer(TypedDict, total=False):
    result: str
    ok: bool


class _Review(TypedDict, total=False):
    learned: str
    trigger: str
    evidence_in_run: str


class _Proposal(TypedDict, total=False):
    skill: str
    rule: str
    why: str
    intents: list[str]
    patterns: list[int]
    overlaps: list[str]
    confidence: str


class _ProposalsAnswer(TypedDict, total=False):
    proposals: list[_Proposal]


class _PatchAnswer(TypedDict, total=False):
    after_line: int
    lines: list[str]


ASSIGN_ANSWER: Final = TypeAdapter(_AssignAnswer)
CONS_ANSWER: Final = TypeAdapter(_ConsAnswer)
JUDGEMENT: Final = TypeAdapter(_Judgement)
RUN_ANSWER: Final = TypeAdapter(_RunAnswer)
REVIEW: Final = TypeAdapter(_Review)
PROPOSALS_ANSWER: Final = TypeAdapter(_ProposalsAnswer)
PATCH_ANSWER: Final = TypeAdapter(_PatchAnswer)


def _calls_text(raw: str) -> str:
    """`name(arguments)` of every tool call of an assistant message's `tool_calls` JSON; what is
    not a well-formed call is left out."""
    try:
        calls = loads(raw)
    except ValueError:
        return ""
    parts: list[str] = []
    for x in calls if isinstance(calls, list) else []:
        f = x.get("function") if isinstance(x, dict) else None
        if isinstance(f, dict):
            args = f.get("arguments")
            parts.append(f"{f.get('name')}({args[:120] if isinstance(args, str) else ''})")
    return " ".join(parts)


# ---------------------------------------------------------------- helpers
def norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", t.lower())).strip()[:400]


def words(*texts: str | None) -> set[str]:
    return {
        w for t in texts for w in (m[0] for m in WORD.finditer((t or "").lower())) if w not in STOP
    }


def _by_score(x: tuple[int, str]) -> int:
    """Sort key of (overlap score, intent id): highest score first, ties keep their order."""
    return -x[0]


def hm(ts: float) -> str:
    return datetime.fromtimestamp(ts or 0, UTC).strftime("%m-%d %H:%M")


def tkey(t: Turn) -> TurnKey:
    return t["sid"], t["msg_id"]


def window(msgs_by_sid: dict[str, list[MessageRow]], sid: str, ts: float, maxc: int = 8000) -> str:
    """The run that preceded a skill edit: from the previous user turn to just after the edit."""
    ms = msgs_by_sid.get(sid, [])
    users = [
        i
        for i, m in enumerate(ms)
        if m["role"] == "user" and m["content"] and m["timestamp"] <= ts + 1
    ]
    start = users[-2] if len(users) > 1 else (users[-1] if users else 0)
    out: list[str] = []
    for m in ms[start:]:
        if m["timestamp"] > ts + 5:
            break
        c = m["content"] or ""
        if m["role"] == "user":
            out.append("USER: " + c[:900])
        elif m["role"] == "tool":
            out.append(f"TOOL {m.get('tool_name')}: " + c[:220].replace("\n", " "))
        elif m["role"] == "assistant":
            calls = ""
            tool_calls = m.get("tool_calls")
            if tool_calls:
                calls = _calls_text(tool_calls)
            out.append("ASSISTANT: " + c[:700] + (" CALLS: " + calls if calls else ""))
    s = "\n".join(out)
    return s if len(s) <= maxc else s[:2000] + "\n…\n" + s[-(maxc - 2000) :]


def error_patterns(members: dict[str, list[Turn]]) -> list[ErrorPattern]:
    """Same normalised error text from tool results or gateway/loop replies, grouped across
    sessions."""
    pats: dict[tuple[str, str], ErrorPattern] = {}
    for iid, tt in members.items():
        for t in tt:
            sigs = [(c[0], sig) for c in t.get("calls", []) if (sig := c[1])] + [
                ("runtime", r) for r in t.get("runtime", [])
            ]
            for tool, sig in sigs:
                p = pats.setdefault(
                    (tool, sig),
                    ErrorPattern(
                        tool=tool,
                        sig=sig,
                        cat=signals.err_category(sig),
                        hits=0,
                        sessions=set(),
                        intents={},
                    ),
                )
                p["hits"] += 1
                p["sessions"].add(t["sid"])
                p["intents"][iid] = p["intents"].get(iid, 0) + 1

    def weight(p: ErrorPattern) -> int:
        return -p["hits"] * len(p["sessions"])

    return sorted(pats.values(), key=weight)


ABORT_AFTER = 5
"""Consecutive model calls that raise (provider down, quota, 429) before a run stops: the rest
of the run would only fail the same way, one timeout at a time."""


class ModelUnavailableError(Exception):
    """The model raised ABORT_AFTER times in a row; the run stops instead of trying every unit."""


class Calls:
    """Wraps the injected llm: counts calls and failures; an exception or an answer that is not a
    JSON object is a failure. ABORT_AFTER exceptions in a row raise ModelUnavailableError, then
    and on every later call, without calling the model again."""

    def __init__(self, llm: LlmFn, log: Log) -> None:
        self.llm, self.log, self.calls, self.failures = llm, log, 0, 0
        self.raised_in_row = 0

    def __call__(
        self, instructions: str, text: str, schema: JsonObject, purpose: str
    ) -> JsonObject | None:
        if self.raised_in_row >= ABORT_AFTER:
            raise ModelUnavailableError(f"the model raised {self.raised_in_row} times in a row")
        self.calls += 1
        r: object
        try:
            r = self.llm(instructions, text, schema, purpose)
            self.raised_in_row = 0
        except Exception as e:
            self.log(f"argus: {purpose}: llm raised {e!r}")
            self.raised_in_row += 1
            r = None
        answer = as_object(r)
        if answer is None:
            self.failures += 1
            self.log(f"argus: {purpose}: no answer, skipped")
            if self.raised_in_row >= ABORT_AFTER:
                raise ModelUnavailableError(f"the model raised {self.raised_in_row} times in a row")
            return None
        return answer

    def ask(
        self, answer: TypeAdapter[T], instructions: str, text: str, schema: JsonObject, purpose: str
    ) -> T | None:
        """The model's answer validated into `answer` (the types of `schema`); None, counted as a
        failure, when there is none or it does not fit."""
        r = self(instructions, text, schema, purpose)
        if r is None:
            return None
        try:
            return answer.validate_python(r)
        except ValidationError as e:
            self.failures += 1
            self.log(f"argus: {purpose}: answer does not fit the schema ({e.error_count()} errors)")
            return None


class Profile:
    """One profile's inputs for a run: plugin DB, turns and messages from state.db, ledger, all
    from the profile's history window (`window_start`, None = the whole history) on."""

    def __init__(
        self,
        name: str,
        home: Path,
        conn: sqlite3.Connection,
        dump: Dump,
        turns: list[Turn],
        sessions: dict[str, Session],
        window_start: float | None = None,
    ) -> None:
        self.name, self.home, self.conn, self.dump = name, home, conn, dump
        self.turns, self.sessions = turns, sessions
        self.window_start = window_start
        self.by_key = {tkey(t): t for t in turns}
        self.changed: dict[str, int] = {}

    def intents(self) -> dict[str, IntentRow]:
        return {r["id"]: r for r in query_as(self.conn, INTENT_ROW, "select * from intent")}

    def members(self, rows: dict[str, IntentRow]) -> dict[str, list[Turn]]:
        """Live intent id -> its turns (known to state.db), in time order."""
        out: dict[str, list[Turn]] = {}
        for r in query_as(
            self.conn, TURN_LINK, "select session_id, user_message_id, intent_id from turn_intent"
        ):
            t = self.by_key.get((r["session_id"], r["user_message_id"]))
            iid = resolve(rows, r["intent_id"])
            if t is not None and iid in rows and rows[iid]["merged_into"] is None:
                out.setdefault(iid, []).append(t)
        for tt in out.values():
            tt.sort(key=lambda t: t["ts"])
        return out


def resolve(rows: dict[str, IntentRow], iid: str) -> str:
    seen: set[str] = set()
    while iid in rows and (into := rows[iid].get("merged_into")) and iid not in seen:
        seen.add(iid)
        iid = into
    return iid


def new_intent(
    conn: sqlite3.Connection,
    rows: dict[str, IntentRow],
    iid: str,
    title: str,
    goal: str | None,
    kind: str,
    who: str | None,
    now: float,
    by: str,
) -> None:
    conn.execute(
        "insert into intent (id, title, goal, kind, who, created_at, updated_at, derived_by) "
        "values (?,?,?,?,?,?,?,?)",
        (iid, title, goal, kind, who, now, now, by),
    )
    rows[iid] = IntentRow(
        id=iid,
        title=title,
        goal=goal,
        kind=kind,
        who=who,
        status=None,
        stable=None,
        risk=None,
        headline=None,
        outcome=None,
        consistency=None,
        friction=None,
        next=None,
        attention=None,
        merged_into=None,
        created_at=now,
        updated_at=now,
        judged_at=None,
        derived_by=by,
    )


# ---------------------------------------------------------------- 1 assign
def gates(px: Profile, done: Collection[TurnKey]) -> Iterator[tuple[Turn, Gate | None]]:
    """assign's free rules: every turn of the window in order with the gate that assigns it, None
    for a turn already assigned (`done`). A rule sees every earlier turn, assigned or not. Each
    session with an `llm` turn costs one model call."""
    sids: set[str] = set()
    texts: set[str] = set()
    for t in px.turns:
        gate: Gate | None = None
        if tkey(t) not in done:
            sess = px.sessions.get(t["sid"])
            # every session of a cron job belongs to its routine
            if t.get("cron") or (sess is not None and sess["source"] == "cron"):
                gate = "cron"
            elif t["sid"] in sids and (len(t["text"]) <= 25 or SHORT_FOLLOWUP.match(t["text"])):
                gate = "follow-up"
            elif len(norm(t["text"])) > 20 and norm(t["text"]) in texts:
                gate = "repeat"
            else:
                gate = "llm"
        yield t, gate
        sids.add(t["sid"])
        texts.add(norm(t["text"]))


def assign(px: Profile, llm: Calls) -> Stats:
    conn, now = px.conn, time.time()
    rows = px.intents()
    done = {
        (r["session_id"], r["user_message_id"]): r["intent_id"]
        for r in query_as(
            conn, TURN_LINK, "select session_id, user_message_id, intent_id from turn_intent"
        )
    }
    # refs are an intent id or ('?', turn key) while the model decides
    last_in_session: dict[str, Ref] = {}
    by_norm: dict[str, Ref] = {}
    last: dict[str, Turn] = {}
    pending: dict[str, tuple[str | None, list[Turn]]] = {}
    deferred: list[tuple[Turn, tuple[str, TurnKey], Gate]] = []
    resolved: dict[TurnKey, str] = {}
    stats = dict(turns=0, cron=0, **{"follow-up": 0}, repeat=0, llm=0, new_intents=0, unassigned=0)

    def note(t: Turn, ref: Ref) -> None:
        last_in_session[t["sid"]] = ref
        by_norm.setdefault(norm(t["text"]), ref)
        if isinstance(ref, str):
            last[ref] = t

    def put(t: Turn, iid: str, gate: str) -> None:
        conn.execute(
            "insert or ignore into turn_intent (session_id, user_message_id, intent_id, gate, "
            "derived_by) values (?,?,?,?,?)",
            (t["sid"], t["msg_id"], iid, gate, V_ASSIGN),
        )
        conn.execute("update intent set updated_at=? where id=?", (now, iid))
        rows[iid]["updated_at"] = now
        stats["turns"] += 1
        stats[gate] += 1

    for t, gate in gates(px, done):
        key = tkey(t)
        if gate is None:
            note(t, resolve(rows, done[key]))
            continue
        ref: Ref
        if gate == "cron":
            sess = px.sessions.get(t["sid"])
            ck = t.get("cron") or signals.cron_key(sess["title"] if sess is not None else None)
            ref = "r" + hashlib.sha1(ck.encode()).hexdigest()[:12]
            if ref not in rows:
                new_intent(
                    conn,
                    rows,
                    ref,
                    ck,
                    f'Hermes cron job "{ck}".',
                    "routine",
                    "scheduled",
                    now,
                    V_ASSIGN,
                )
                stats["new_intents"] += 1
        elif gate == "follow-up":
            ref = last_in_session[t["sid"]]
        elif gate == "repeat":
            ref = by_norm[norm(t["text"])]
        else:
            ref = ("?", key)
            cur_ref = last_in_session.get(t["sid"])
            current = cur_ref if isinstance(cur_ref, str) else None
            pending.setdefault(t["sid"], (current, []))[1].append(t)
        if gate != "llm":
            if isinstance(ref, str):
                put(t, ref, gate)
            else:
                deferred.append((t, ref, gate))
        note(t, ref)
    conn.commit()

    def first_ts(kv: tuple[str, tuple[str | None, list[Turn]]]) -> float:
        return kv[1][1][0]["ts"]

    def recency(k: str) -> float:
        return -last[k]["ts"]

    for sid, (cur, tt) in sorted(pending.items(), key=first_ts):
        # with a window, only intents that have turns in it are candidates
        live = {
            k: v
            for k, v in rows.items()
            if v["merged_into"] is None
            and v["kind"] != "routine"
            and (px.window_start is None or k in last)
        }
        recent = sorted((k for k in live if k in last), key=recency)[:RECENT]
        tw = words(*(t["text"] for t in tt))
        older = sorted(
            (
                (len(tw & words(v["title"], v["goal"], last[k]["text"] if k in last else "")), k)
                for k, v in live.items()
                if k not in recent
            ),
            key=_by_score,
        )
        cands = recent + [k for score, k in older[:OLDER] if score > 0]
        lines: list[str] = []
        for k in cands:
            v, lt = live[k], last.get(k)
            when = (
                f"last {hm(lt['ts'])}{', SAME SESSION' if lt['sid'] == sid else ''}"
                if lt
                else "no recent turns"
            )
            lines.append(
                f"{k}: {v['title']} [{v['kind']}] ({when}) goal: {(v['goal'] or '')[:160]}"
                + (f' last turn: "{lt["text"][:160]}"' if lt else "")
            )
        body = "\n".join(
            f"T{j + 1} ({t['source']}{', UI: ' + t['view'] if t.get('view') else ''}, "
            f"{hm(t['ts'])}):\n{t['text'][:900]}"
            for j, t in enumerate(tt)
        )
        prompt = (
            "CANDIDATE INTENTS (most recent first):\n"
            + ("\n".join(lines) or "(none)")
            + (f"\n\nThe current intent of this session is {cur}." if cur else "")
            + f"\n\nNEW TURNS of session {sid}:\n{body}"
        )
        r = llm.ask(
            ASSIGN_ANSWER, prompts.ASSIGN_SYS, prompt, prompts.ASSIGN_SCHEMA, "argus.assign"
        )
        if r is None:
            continue
        ans = {a.get("turn", ""): a for a in r.get("turns", [])}
        got: dict[str, str] = {}
        prev = cur
        for j, t in enumerate(tt):
            lab, a = f"T{j + 1}", ans.get(f"T{j + 1}")
            iid: str | None = None
            if a is not None:
                pick = a.get("intent", "").strip()
                if pick in got:
                    iid = got[pick]
                elif pick in rows:
                    iid = resolve(rows, pick)
                    iid = (
                        iid
                        if rows[iid]["merged_into"] is None and rows[iid]["kind"] != "routine"
                        else None
                    )
                if iid is None:
                    iid = "i" + hashlib.sha1(f"{t['sid']}:{t['msg_id']}".encode()).hexdigest()[:12]
                    if iid not in rows:
                        kind, who = a.get("kind", ""), a.get("who", "")
                        new_intent(
                            conn,
                            rows,
                            iid,
                            a.get("title", "").strip() or t["text"][:60],
                            a.get("goal", "").strip() or None,
                            kind if kind in KINDS else "task",
                            who if who in WHOS else None,
                            now,
                            V_ASSIGN,
                        )
                        stats["new_intents"] += 1
            elif prev:  # the model skipped this turn: it stays with the session's current intent
                iid = prev
            if iid is None:
                continue
            got[lab], prev, resolved[tkey(t)] = iid, iid, iid
            last[iid] = t
            put(t, iid, "llm")
        still: list[tuple[Turn, tuple[str, TurnKey], Gate]] = []
        for t, pref, gate in deferred:
            if pref[1] in resolved:
                put(t, resolved[pref[1]], gate)
            else:
                still.append((t, pref, gate))
        deferred = still
        conn.commit()
    stats["unassigned"] = len(deferred) + sum(
        1 for _, tt in pending.values() for t in tt if tkey(t) not in resolved
    )
    px.changed["assign"] = stats["turns"]
    return stats


# ---------------------------------------------------------------- 2 consolidate
def consolidate(px: Profile, llm: Calls) -> Stats:
    conn = px.conn
    since = store.meta_get(conn, "consolidated_at")
    rows = px.intents()
    members = px.members(rows)
    live = {
        k: v
        for k, v in rows.items()
        if v["merged_into"] is None and v["kind"] != "routine" and k in members
    }
    touched = [k for k, v in live.items() if since is None or v["updated_at"] > float(since)]
    stats = {"touched": len(touched), "merge": 0, "split": 0, "rename": 0, "ignored": 0}
    if not touched:
        return stats
    kw = {
        k: words(v["title"], v["goal"], *(t["text"] for t in members[k][:6]))
        for k, v in live.items()
    }
    order: list[str] = []
    seen: set[str] = set()

    def first_ts(k: str) -> float:
        return members[k][0]["ts"]

    for k in sorted(touched, key=first_ts):
        near = sorted(((len(kw[k] & kw[o]), o) for o in live if o != k), key=_by_score)[:3]
        for o in [k] + [o for score, o in near if score >= 2]:
            if o not in seen:
                seen.add(o)
                order.append(o)
    touched_set, ok = set(touched), True
    for c in range(0, len(order), CONS_CHUNK):
        ids = order[c : c + CONS_CHUNK]
        labels: dict[str, tuple[str, TurnKey]] = {}
        lines: list[str] = []
        for k in ids:
            v, tt = live[k], members[k]
            shown = tt[:8] if k in touched_set else tt[:2]
            for n, t in enumerate(shown):
                labels[f"{k}#{n + 1}"] = (k, tkey(t))
            ex = "\n".join(
                f"  {k}#{n + 1} [{t['sid'][-6:]} {hm(t['ts'])}] "
                f"{t['text'].replace(chr(10), ' ')[:160]}"
                for n, t in enumerate(shown)
            )
            lines.append(
                f"{k} [{v['kind']}, {v['who'] or '?'}] {v['title']} — goal: {v['goal'] or ''}; "
                f"{len(tt)} turns, {len({t['sid'] for t in tt})} sessions\n{ex}"
            )
        r = llm.ask(
            CONS_ANSWER,
            prompts.CONS_SYS,
            "INTENTS:\n" + "\n".join(lines),
            prompts.CONS_SCHEMA,
            "argus.consolidate",
        )
        if r is None:
            ok = False
            continue
        for op in r.get("ops", []):
            if not apply_op(px, rows, set(ids), labels, op, stats):
                stats["ignored"] += 1
        conn.commit()
    if ok:
        store.meta_set(conn, "consolidated_at", str(time.time()))
        conn.commit()
    px.changed["consolidate"] = stats["merge"] + stats["split"] + stats["rename"]
    return stats


def apply_op(
    px: Profile,
    rows: dict[str, IntentRow],
    shown: set[str],
    labels: dict[str, tuple[str, TurnKey]],
    op: _ConsOp,
    stats: Stats,
) -> bool:
    """Apply one validated consolidation op; False when it is ignored (unknown ids, self-merge,
    cycle, empty)."""
    conn, now, kind = px.conn, time.time(), op.get("op")

    def live(i: str | None) -> TypeGuard[str]:
        return (
            i is not None
            and i in shown
            and i in rows
            and rows[i]["merged_into"] is None
            and rows[i]["kind"] != "routine"
        )

    if kind == "merge":
        keep, drop = op.get("keep"), op.get("drop")
        if not (live(keep) and live(drop)):
            return False
        if keep == drop or resolve(rows, keep) == drop:
            return False
        conn.execute("update intent set merged_into=?, updated_at=? where id=?", (keep, now, drop))
        conn.execute("update intent set merged_into=? where merged_into=?", (keep, drop))
        conn.execute("update turn_intent set intent_id=? where intent_id=?", (keep, drop))
        conn.execute("delete from run where intent_id=?", (drop,))
        conn.execute("update intent set updated_at=?, judged_at=null where id=?", (now, keep))
        for v in rows.values():
            if v["merged_into"] == drop:
                v["merged_into"] = keep
        rows[drop]["merged_into"] = keep
        stats["merge"] += 1
        return True
    if kind == "split":
        frm, title = op.get("from"), op.get("title", "").strip()
        if not live(frm) or not title:
            return False
        moved: list[TurnKey] = [
            labels[x][1] for x in op.get("turns") or [] if x in labels and labels[x][0] == frm
        ]
        keys = list(dict.fromkeys(moved, True))
        have = {
            (r["session_id"], r["user_message_id"])
            for r in query_as(
                conn,
                TURN_KEY_ROW,
                "select session_id, user_message_id from turn_intent where intent_id=?",
                (frm,),
            )
        }
        keys = [k for k in keys if k in have]
        if not keys or len(keys) >= len(have):
            return False
        base = "i" + hashlib.sha1(f"split:{keys[0][0]}:{keys[0][1]}".encode()).hexdigest()[:12]
        iid, n = base, 1
        while iid in rows:
            n += 1
            iid = f"{base}-{n}"
        new_intent(
            conn,
            rows,
            iid,
            title,
            op.get("goal", "").strip() or None,
            rows[frm]["kind"],
            rows[frm]["who"],
            now,
            V_CONS,
        )
        conn.executemany(
            "update turn_intent set intent_id=? where session_id=? and user_message_id=?",
            [(iid, s, m) for s, m in keys],
        )
        conn.execute("delete from run where intent_id=?", (frm,))
        conn.execute("update intent set updated_at=?, judged_at=null where id=?", (now, frm))
        shown.add(iid)
        stats["split"] += 1
        return True
    if kind == "rename":
        target, title = op.get("id"), op.get("title", "").strip()
        if not live(target) or not title:
            return False
        goal = op.get("goal", "").strip() or rows[target]["goal"]
        conn.execute(
            "update intent set title=?, goal=?, updated_at=? where id=?",
            (title, goal, now, target),
        )
        rows[target]["title"] = title
        rows[target]["goal"] = goal
        rows[target]["updated_at"] = now
        stats["rename"] += 1
        return True
    return False


# ---------------------------------------------------------------- 3 judge
def run_starts(tt: list[Turn]) -> dict[str, float]:
    """An intent's runs (sessions) with the time of their first turn, from its turns in order."""
    starts: dict[str, float] = {}
    for t in tt:
        starts.setdefault(t["sid"], t["ts"])
    return starts


def judge_due(it: IntentRow, starts: dict[str, float]) -> bool:
    """judge: whether an intent is judged (again): never judged, or a run of it (`run_starts`)
    started after the last judgement."""
    judged_at = it["judged_at"]
    return judged_at is None or max(starts.values()) > judged_at


def judge(px: Profile, llm: Calls) -> Stats:
    conn = px.conn
    rows = px.intents()
    members = px.members(rows)
    stats = {"candidates": 0, "judged": 0}
    for iid, tt in members.items():
        it = rows[iid]
        starts = run_starts(tt)
        if not judge_due(it, starts):
            continue
        stats["candidates"] += 1
        if it["kind"] == "routine":
            body = "\n".join(
                f"- run {hm(t['ts'])} ({t['tools']} tool calls): "
                f"{(t['reply'] or '(no reply)')[:300]}"
                for t in tt[-30:]
            )
        else:
            shown = tt if len(tt) <= 16 else tt[:8] + tt[-8:]
            body = "\n".join(
                f"- [{t['sid'][-6:]}] ({t['tools']} tool calls) USER: {t['text'][:300]}\n"
                f"  AGENT: {(t['reply'] or '')[:600]}"
                for t in shown
            )
        p = (
            f"INTENT: {it['title']}\nGOAL: {it['goal'] or ''}\nKIND: {it['kind']}\n"
            f"SESSIONS: {len(starts)}\n\nTURNS:\n{body}"
        )
        r = llm.ask(JUDGEMENT, prompts.OUT_SYS, p, prompts.OUT_SCHEMA, "argus.judge")
        if r is None:
            continue
        attention = max(0, min(3, r.get("attention", 0)))
        who, status, stable = r.get("who"), r.get("status"), r.get("stable")
        # every enum field is checked against prompts.OUT_SCHEMA: a value outside it is stored as
        # if missing (NULL; export shows status "unclear", stable "", keeps the previous who), so
        # nothing the model invents reaches the dashboard as a class or label. The texts are
        # stored as sent.
        conn.execute(
            "update intent set status=?, who=coalesce(?, who), stable=?, risk=?, headline=?, "
            "outcome=?, consistency=?, friction=?, next=?, attention=?, judged_at=? where id=?",
            (
                status if status in STATUSES else None,
                who if who in WHOS else None,
                stable if stable in STABLES else None,
                r.get("risk", ""),
                r.get("headline", ""),
                r.get("outcome", ""),
                r.get("consistency", ""),
                r.get("friction", ""),
                r.get("next", ""),
                attention,
                time.time(),
                iid,
            ),
        )
        conn.commit()
        stats["judged"] += 1
    px.changed["judge"] = stats["judged"]
    return stats


# ---------------------------------------------------------------- 4 runs
def signal_status(conn: sqlite3.Connection, tt: list[Turn]) -> str:
    """Run status from evidence only: cron markers, interruptions, tool/runtime errors, missing
    reply."""
    sid, t0, t1 = tt[0]["sid"], tt[0]["ts"] - 1, max(t.get("end") or t["ts"] for t in tt) + 1
    replies = [t["reply"] or "" for t in tt]
    runtime = [r for t in tt for r in t.get("runtime", [])]
    if any("[CRON_FAILURE]" in r for r in replies):
        return "cron_failure"
    if "[SILENT]" in replies[-1]:
        return "silent"
    if any(r.startswith("loop: turn interrupted") for r in runtime) or query_one(
        conn,
        "select 1 from turn_outcome where session_id=? and interrupted=1 and started_at "
        "between ? and ? limit 1",
        (sid, t0, t1),
    ):
        return "interrupted"
    if (
        runtime
        or any(c[1] for t in tt for c in t.get("calls", []))
        or query_one(
            conn,
            "select 1 from tool_outcome where session_id=? and status='error' and ts between ? "
            "and ? limit 1",
            (sid, t0, t1),
        )
    ):
        return "error"
    return "ok"


def runs(px: Profile, llm: Calls) -> Stats:
    conn = px.conn
    rows = px.intents()
    members = px.members(rows)
    have = {
        (r["intent_id"], r["session_id"]): r for r in query_as(conn, RUN_ROW, "select * from run")
    }
    stats = {"runs": 0, "removed": 0, "judged": 0}
    keep: set[tuple[str, str]] = set()
    for iid, tt in members.items():
        per: dict[str, list[Turn]] = {}
        for t in tt:
            per.setdefault(t["sid"], []).append(t)
        for sid, st in per.items():
            keep.add((iid, sid))
            conn.execute(
                "insert into run (intent_id, session_id, started_at, signal_status, derived_by) "
                "values (?,?,?,?,?) on conflict(intent_id, session_id) do update set "
                "started_at=excluded.started_at, signal_status=excluded.signal_status, "
                "derived_by=excluded.derived_by",
                (iid, sid, st[0]["ts"], signal_status(conn, st), V_RUNS),
            )
            stats["runs"] += 1
    for k in have:
        # a session outside the window is not in px.sessions: its runs stay as they are
        if k not in keep and (px.window_start is None or k[1] in px.sessions):
            conn.execute("delete from run where intent_id=? and session_id=?", k)
            stats["removed"] += 1
    conn.commit()
    for iid, sid, st in runs_due(rows, members, have):
        p = (
            f"TASK: {st[0]['text'][:600]}\n\n"
            f"FINAL AGENT ANSWER:\n{(st[-1]['reply'] or '(none)')[:1800]}"
        )
        r = llm.ask(RUN_ANSWER, prompts.RUN_SYS, p, prompts.RUN_SCHEMA, "argus.runs")
        if r is None or "ok" not in r:
            continue
        conn.execute(
            "update run set model_ok=?, model_result=? where intent_id=? and session_id=?",
            (int(r["ok"]), r.get("result", "").strip(), iid, sid),
        )
        conn.commit()
        stats["judged"] += 1
    return stats


def runs_due(
    rows: dict[str, IntentRow],
    members: dict[str, list[Turn]],
    have: dict[tuple[str, str], RunRow],
) -> Iterator[tuple[str, str, list[Turn]]]:
    """runs: the runs the model judges, as (intent id, session id, its turns): every session of a
    non-routine intent with 3+ sessions whose run in `have` (the run rows before the pass) the
    model has not judged yet."""
    for iid, tt in members.items():
        sids = list({t["sid"]: None for t in tt})
        if rows[iid]["kind"] == "routine" or len(sids) < 3:
            continue
        for sid in sids:
            old = have.get((iid, sid))
            if old is not None and old["model_ok"] is not None:
                continue
            yield iid, sid, [t for t in tt if t["sid"] == sid]


# ---------------------------------------------------------------- 5 edit_review
class EditPlan:
    """What edit_review does with each pending skill edit, decided without a model call (the rules
    are on edit_review)."""

    def __init__(self, px: Profile) -> None:
        # edits before the window stay pending: their ledger entry and run are not loaded
        self.todo = query_as(
            px.conn,
            SKILL_EDIT_ROW,
            "select * from skill_edit where (derived_by is null or derived_by != ?) and ts >= ?"
            " order by ts",
            (V_REVIEW, px.window_start or 0.0),
        )
        self.ledger: dict[str, LedgerEntry] = {}
        self.blobs: dict[str, str] = {}
        self.by: dict[str, list[MessageRow]] = {}
        self.earlier: set[str] = set()
        if not self.todo:
            return
        self.ledger = {e["id"]: e for e in px.dump["ledger"] or []}
        self.blobs = px.dump["blobs"] or {}
        for m in px.dump["messages"] or []:
            self.by.setdefault(m["session_id"], []).append(m)
        for ms in self.by.values():
            ms.sort(key=lambda m: (m["timestamp"], m["id"]))
        # without a window the dump holds every message: a session missing from it has none. With
        # a window, one whose messages all precede it waits until a wider window loads them.
        unloaded = {sid for se in self.todo if (sid := self.session_of(se)) and sid not in self.by}
        if px.window_start is not None:
            self.earlier = hermes_data.sessions_with_messages(px.home, unloaded)

    def session_of(self, se: SkillEditRow) -> str | None:
        e = self.ledger.get(se["ledger_entry_id"])
        evidence = e.get("evidence") if e is not None else None
        return se["session_id"] or (evidence.get("session_id") if evidence else None)

    def steps(self) -> Iterator[EditStep]:
        """Every pending edit in time order with its step and model input."""
        for se in self.todo:
            e = self.ledger.get(se["ledger_entry_id"])
            changed = hermes_data.changed_md(e, self.blobs) if e is not None else {}
            diffs = [
                f"file={fpath.split('/skills/', 1)[-1]}\n{signals.unified(b, a)}"
                for fpath, (b, a) in changed.items()
            ]
            action = e.get("action") if e is not None else None
            actor = e.get("actor") if e is not None else None
            older = se["derived_by"] is not None  # labelled by an older version of this stage
            sid = self.session_of(se)
            step: ReviewStep
            if not diffs or action in NO_DIFF_ACTIONS:
                step = "no_diff"
            elif actor == "user" or action == "rollback":
                step = "by_person"  # a person's edit or an undo: there is no learning to check
            elif sid in self.earlier:
                step = "skipped"
            elif sid is None or sid not in self.by:
                # no run to judge it on (a curator pass, a deleted session): judged without one
                # it would read as speculative. The rule itself is in the diff.
                step = "relabel_no_run" if older else "no_run"
            else:
                step = "relabel" if older else "review"
            model = step in ("no_run", "review")
            yield EditStep(
                edit=se,
                step=step,
                sid=sid or "",
                head=f"SKILL: {se['skill_name']} actor={actor} action={action}\n" if model else "",
                diff=f"DIFF:\n{chr(10).join(diffs)[:4000]}" if model else "",
            )


def edit_review(px: Profile, llm: Calls) -> Stats:
    """Label each skill edit with the evidence its run gave for it. Only an edit whose run state.db
    holds is judged on that run; the others are labelled without judging: an edit without a diff
    stays unlabelled, a person's (ledger actor `user`, a rollback) is not reviewed, and one whose
    session has no messages in state.db gets `no-run` and its rule read from the diff alone.
    Rows labelled by an older version of this stage are relabelled by the same rules, keeping
    their model output: no model call."""
    conn = px.conn
    plan = EditPlan(px)
    stats = {
        "pending": len(plan.todo),
        "reviewed": 0,
        "no_diff": 0,
        "by_person": 0,
        "no_run": 0,
        "relabelled": 0,
    }
    if px.window_start is not None:
        stats["skipped"] = 0  # edits whose session has messages only before the window
    if not plan.todo:
        return stats

    def label(
        se: SkillEditRow, evidence: str | None, learned: str | None, trigger: str | None
    ) -> None:
        conn.execute(
            "update skill_edit set evidence=?, learned=?, trigger=?, derived_by=? "
            "where ledger_entry_id=?",
            (evidence, learned, trigger, V_REVIEW, se["ledger_entry_id"]),
        )
        conn.commit()

    for s in plan.steps():
        se, step = s["edit"], s["step"]
        if step == "no_diff" or step == "by_person":
            label(se, None, None, None)
            stats[step] += 1
        elif step == "skipped":
            stats["skipped"] += 1
        elif step == "relabel_no_run":
            label(se, NO_RUN, se["learned"], None)
            stats["relabelled"] += 1
        elif step == "no_run":
            r = llm.ask(
                REVIEW,
                prompts.LEARN_SYS,
                s["head"] + s["diff"],
                prompts.LEARN_SCHEMA,
                "argus.edit_learned",
            )
            if r is None:
                continue
            label(se, NO_RUN, r.get("learned", "").strip(), None)
            stats["no_run"] += 1
        elif step == "relabel":
            label(se, se["evidence"], se["learned"], se["trigger"])
            stats["relabelled"] += 1
        else:
            run = window(plan.by, s["sid"], se["ts"]) or "(no messages)"
            p = f"{s['head']}{s['diff']}\n\nRUN WINDOW:\n{run}"
            r = llm.ask(REVIEW, prompts.REV_SYS, p, prompts.REV_SCHEMA, "argus.edit_review")
            if r is None:
                continue
            ev = r.get("evidence_in_run")
            label(
                se,
                ev if ev in EVIDENCE else None,
                r.get("learned", "").strip(),
                r.get("trigger", "").strip(),
            )
            stats["reviewed"] += 1
    px.changed["edit_review"] = stats["reviewed"] + stats["no_run"]
    return stats


# ---------------------------------------------------------------- 6 proposals
def proposals(px: Profile, llm: Calls, force: bool = False) -> Stats:
    conn = px.conn
    stats = {"proposed": 0, "patched": 0, "skipped": 0}
    changed = (
        px.changed.get("judge", 0)
        + px.changed.get("edit_review", 0)
        + px.changed.get("consolidate", 0)
    )
    if not force and not changed and store.meta_get(conn, "proposals_at") is not None:
        return stats
    rows = px.intents()
    members = px.members(rows)
    pats = error_patterns(members)[:30]
    il = "\n".join(
        f"{k} [{v['status']}] {v['title']}: friction={v['friction'] or ''} next={v['next'] or ''}"
        for k, v in rows.items()
        if k in members
        and v["status"]
        and v["kind"] not in ("probe", "chat")
        and (v["friction"] or v["status"] != "achieved")
    )
    pl = "\n".join(
        f"{k}: [{p['cat']}] {p['tool']}: {p['sig']} — {p['hits']} hits, "
        f"{len(p['sessions'])} sessions, intents {list(p['intents'])[:6]}"
        for k, p in enumerate(pats)
    )
    edits = query_as(
        conn,
        LEARNED_ROW,
        "select ledger_entry_id, skill_name, learned from skill_edit where learned is not null "
        "and learned != '' and ts >= ? order by ts desc limit 60",
        (px.window_start or 0.0,),
    )[::-1]
    applied = applied_rules(conn)
    el = "\n".join(
        [f"{e['ledger_entry_id']}: {e['skill_name']}: {e['learned']}" for e in edits]
        + [f"{a['ledger_entry']}: {a['skill_name']}: {a['rule']}" for a in applied]
    )
    if not il and not pl:
        store.meta_set(conn, "proposals_at", str(time.time()))
        store.meta_set(conn, auto.LAST_PASS, "[]")
        conn.commit()
        return stats
    files = store.skill_files(px.home)
    prompt = (
        f"SKILLS (names): {', '.join(sorted(files))[:3000]}\n\n"
        f"INTENTS WITH PROBLEMS:\n{il or '(none)'}\n\n"
        f"ERROR PATTERNS:\n{pl or '(none)'}\n\nHERMES EDITS:\n{el or '(none)'}"
    )
    r = llm.ask(PROPOSALS_ANSWER, prompts.PROP_SYS, prompt, prompts.PROP_SCHEMA, "argus.proposals")
    if r is None:
        return stats
    edit_ids = {
        e["ledger_entry_id"]
        for e in query_as(conn, EDIT_ID_ROW, "select ledger_entry_id from skill_edit")
    } | {a["ledger_entry"] for a in applied}
    old = {x["id"]: x for x in query_as(conn, PROPOSAL_DIFF_ROW, "select id, diff from proposal")}
    emitted: list[str] = []
    for p in r.get("proposals", []):
        rule, sk = p.get("rule", "").strip(), p.get("skill", "").strip()
        name = sk[4:].strip() if sk.startswith("new:") else sk
        if not rule or not name:
            stats["skipped"] += 1
            continue
        path = files.get(name)
        pid = hashlib.sha256(f"{name}\n{rule}".encode()).hexdigest()[:16]
        prior = old.get(pid)
        diff = prior["diff"] if prior is not None else None
        if path is not None and diff is None:
            diff = patch(path.read_text(encoding="utf-8", errors="replace"), rule, llm)
            stats["patched"] += diff is not None
        pidx = [i for i in p.get("patterns", []) if 0 <= i < len(pats)]
        evidence = {
            "intents": [i for i in p.get("intents", []) if i in members],
            "error_sigs": [pats[i]["sig"] for i in pidx],
            "overlaps": [i for i in p.get("overlaps", []) if i in edit_ids],
        }
        confidence = p.get("confidence")
        conn.execute(
            "insert or replace into proposal (id, skill_name, is_new_skill, file, rule, why, "
            "diff, confidence, evidence, derived_by) values (?,?,?,?,?,?,?,?,?,?)",
            (
                pid,
                name,
                int(path is None and sk.startswith("new:")),
                store.skill_rel(px.home, path) if path is not None else None,
                rule,
                p.get("why", "").strip(),
                diff,
                confidence if confidence in CONFIDENCE else None,
                json.dumps(evidence),
                V_PROP,
            ),
        )
        # never hold the write lock across the next patch() model call (hook Writer would time out)
        conn.commit()
        emitted.append(pid)
        stats["proposed"] += 1
    store.meta_set(conn, "proposals_at", str(time.time()))
    store.meta_set(conn, auto.LAST_PASS, json.dumps(emitted))
    conn.commit()
    return stats


def applied_rules(conn: sqlite3.Connection) -> list[_AppliedRuleRow]:
    """Argus' own rules written or staged for a skill (by a person's apply or auto-apply), newest
    first, under their ledger entry (or `argus-<proposal id>` while apply_log has none, e.g. a
    staged write). The proposals pass lists them with the harness' edits, so it does not propose
    them again in other words; one rolled back or rejected since stays listed: it was answered."""
    return query_as(
        conn,
        APPLIED_RULE_ROW,
        "select coalesce(a.ledger_entry, 'argus-' || p.id) ledger_entry, p.skill_name, p.rule"
        " from apply_log a join proposal p on a.target_kind = 'proposal' and p.id = a.target_id"
        " where a.outcome in ('applied', 'staged', 'unknown') group by p.id"
        " order by max(a.applied_at) desc limit 30",
    )


def patch(text: str, rule: str, llm: Calls) -> str | None:
    """Minimal insertion of a rule into the current SKILL.md, as a unified diff."""
    lines = text.splitlines()
    num = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines[:450]))
    r = llm.ask(
        PATCH_ANSWER,
        prompts.PATCH_SYS,
        f"RULE: {rule}\n\nSKILL.md ({len(lines)} lines):\n{num}",
        prompts.PATCH_SCHEMA,
        "argus.patch",
    )
    if r is None or not r.get("lines"):
        return None
    at = max(0, min(len(lines), r.get("after_line", 0)))
    new = lines[:at] + r.get("lines", []) + lines[at:]
    return signals.unified("\n".join(lines), "\n".join(new))


# ---------------------------------------------------------------- estimate
def skipped(home: Path, start: float | None) -> bool:
    """Whether derive skips a profile (UNBACKFILLED); `start` is its window (store.read_window)."""
    return start is None and not store.backfilled(home)


def history(home: Path, start: float | None) -> tuple[Dump, list[Turn], dict[str, Session]]:
    """What derive reads of a profile's history: the window backfill set (store.WINDOW, `start`)
    on; nothing before it is read, assigned or judged. Raises HermesFormatError."""
    dump = hermes_data.load_dump(home, since=start)
    turns, sessions, _children = hermes_data.build_turns([dump])
    return dump, turns, sessions


def estimate(home: Path) -> Estimate | None:
    """The model calls the next derive of a profile makes, counted by the stages' own rules from
    the plugin DB and the window's history, without a model call and read-only; None when derive
    skips the profile. Exact for assign (one call per session with turns the free rules leave to
    the model) and edit_review (one per edit judged on its run or on its diff alone) as the data
    stands; `due` counts the intents and runs judge and runs would send now. What the new turns
    add once they land in intents is known only after assign: `new` bounds it (each turn the
    model or a repeat assigns can touch one intent, judged once, and add one run of it; each cron
    job's routine is judged once), and `most` adds the ledger entries derive reads first and the
    consolidation calls. Raises HermesFormatError."""
    home = Path(home)
    start = store.read_window(home)
    if skipped(home, start):
        return None
    dump, turns, sessions = history(home, start)
    conn = sqlite3.connect(f"file:{store.db_path(home)}?mode=ro", uri=True, timeout=30)
    try:
        px = Profile(store.profile_name(home), home, conn, dump, turns, sessions, start or None)
        done = {
            (r["session_id"], r["user_message_id"])
            for r in query_as(
                conn, TURN_KEY_ROW, "select session_id, user_message_id from turn_intent"
            )
        }
        pending = [(t, gate) for t, gate in gates(px, done) if gate is not None]
        assign_calls = len({t["sid"] for t, gate in pending if gate == "llm"})
        opening = sum(1 for _t, gate in pending if gate in ("llm", "repeat"))
        routines = {
            t.get("cron") or t["sid"] for t, gate in pending if gate == "cron"
        }  # an upper bound: sessions of one job share its routine
        steps = [s["step"] for s in EditPlan(px).steps()]
        rows = px.intents()
        members = px.members(rows)
        have = {
            (r["intent_id"], r["session_id"]): r
            for r in query_as(conn, RUN_ROW, "select * from run")
        }
        judging = sum(judge_due(rows[iid], run_starts(tt)) for iid, tt in members.items())
        due = judging + sum(1 for _ in runs_due(rows, members, have))
        known = {
            r["ledger_entry_id"]
            for r in query_as(conn, EDIT_ID_ROW, "select ledger_entry_id from skill_edit")
        }
        try:
            raw = store.meta_get(conn, store.BACKFILL_CURSOR) or "{}"
            cur = as_number(loads_object(raw).get("ledger_ts"))
        except ValueError:
            cur = None
    finally:
        conn.close()
    unread = sum(
        1
        for e in hermes_data.read_ledger(home, max(cur or 0.0, start or 0.0))
        if e.get("skill") and e["id"] not in known
    )
    review, learn = steps.count("review"), steps.count("no_run")
    calls = assign_calls + review + learn + due
    new = 2 * opening + len(routines)
    cons = -(-4 * opening // CONS_CHUNK)  # touched intents and up to 3 neighbours each, per call
    return Estimate(
        assign=assign_calls,
        review=review,
        learn=learn,
        unread=unread,
        due=due,
        calls=calls,
        new=new,
        most=calls + unread + new + cons + 1,  # + the proposals pass
    )


def estimate_lines(est: Estimate, window: Literal["set", "widened"] | None = None) -> list[str]:
    """The estimate for a person: one line, and a warning when the pass can exceed LARGE calls.
    `window`: this backfill just set or widened the window the estimate covers."""
    due = f", {est['due']} intents and runs due for judging" if est["due"] else ""
    unread = f", {est['unread']} new ledger entries" if est["unread"] else ""
    lines = [
        (
            f"next derive: about {est['calls']} model calls known now, up to {est['most']}"
            f" ({est['assign']} sessions to assign, {est['review'] + est['learn']} skill edits to"
            f" review{unread}{due}; up to {est['new']} to judge the intents and runs the new"
            " turns land in; + one per proposal)"
        )
    ]
    if est["most"] > LARGE:
        why = f", over the window this backfill {window}" if window else ""
        lines += [
            (
                f"warning: this can be a large pass on the auxiliary.argus model{why}; a smaller"
                " --since keeps it cheaper (2-7 days on a busy agent), but the window never"
                " narrows."
            ),
            (
                "derive also starts by itself once the agent is idle: to spend less on this"
                " window, choose a cheaper model for auxiliary.argus (`hermes model`) first."
            ),
        ]
    return lines


# ---------------------------------------------------------------- entry point
def run(
    root: Path,
    llm: LlmFn,
    *,
    profiles: list[str] | None = None,
    stages: list[str] | None = None,
    log: Log = print,
) -> RunReport:
    """Run the derivation stages on every profile of the Hermes install at `root`; returns the
    report."""
    root, t_all = Path(root), time.monotonic()
    unknown = [s for s in stages or [] if s not in STAGES]
    if unknown:
        raise ValueError(f"unknown stage(s): {', '.join(unknown)}; expected {', '.join(STAGES)}")
    want = [s for s in STAGES if stages is None or s in stages]
    started = datetime.now(UTC)
    report = RunReport(started_at=started.isoformat(timespec="seconds"), stages=want, profiles={})
    calls = Calls(llm, log)
    force_proposals = bool(stages) and "proposals" in (stages or [])
    fns: dict[str, Callable[[Profile, Calls], Stats]] = {
        "assign": assign,
        "consolidate": consolidate,
        "judge": judge,
        "runs": runs,
        "edit_review": edit_review,
        "proposals": lambda px, c: proposals(px, c, force=force_proposals),
    }
    for name, home in store.profile_homes(root).items():
        if report.get("aborted"):
            break
        if profiles is not None and name not in profiles:
            continue
        t_prof, rep = time.monotonic(), ProfileReport(home=str(home), stages={})
        report["profiles"][name] = rep
        start = store.read_window(home)
        if skipped(home, start):
            log(f"argus: {name}: skipped: {UNBACKFILLED}")
            rep["skipped"] = UNBACKFILLED
            continue
        try:
            dump, turns, sessions = history(home, start)
        except hermes_data.HermesFormatError as e:
            log(f"argus: {name}: {e}")
            rep["error"] = str(e)
            continue
        conn = store.open_db(home)
        try:
            # 0: the whole history, asked for explicitly; derives as if no window was set
            px = Profile(name, Path(home), conn, dump, turns, sessions, start or None)
            if start is not None:
                rep["window_start"] = start
            for st in want:
                t0, c0, f0 = time.monotonic(), calls.calls, calls.failures
                out: StageReport
                try:
                    out = dict(fns[st](px, calls))
                except ModelUnavailableError as e:  # the provider is down for every later stage too
                    conn.rollback()
                    log(f"argus: {name}: stage {st} stopped: {e}")
                    out = {"error": str(e)}
                    report["aborted"] = rep["error"] = f"stopped at {st}: {e}"
                except Exception as e:  # a broken stage must not block the next ones
                    conn.rollback()
                    log(f"argus: {name}: stage {st} failed: {e!r}\n{traceback.format_exc()}")
                    out = {"error": repr(e)}
                out.update(
                    llm_calls=calls.calls - c0,
                    llm_failures=calls.failures - f0,
                    seconds=round(time.monotonic() - t0, 2),
                )
                rep["stages"][st] = out
                log(f"argus: {name}: {st} {json.dumps(out)}")
                if report.get("aborted"):
                    break
        finally:
            conn.close()
        rep["seconds"] = round(time.monotonic() - t_prof, 2)
        out_dir = store.db_path(home).parent / "runs"
        out_dir.mkdir(parents=True, exist_ok=True)
        run_file: RunReport = {**report, "profiles": {name: rep}}
        (out_dir / f"{started:%Y%m%dT%H%M%SZ}.json").write_text(json.dumps(run_file, indent=1))
    report["llm_calls"] = calls.calls
    report["llm_failures"] = calls.failures
    report["seconds"] = round(time.monotonic() - t_all, 2)
    return report
