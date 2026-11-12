"""Build the dashboard JSON (the `window.DATA` structure app.js reads) from state.db, the curator
ledger and blobs, and the plugin DB of each profile. Read-only everywhere.

The JSON contract is exporttypes.Export."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypeAlias

from pydantic import TypeAdapter
from typing_extensions import TypedDict

from . import apply, derive, hermes_data, signals, store
from .dbtypes import (
    APPLY_LOG_ROW,
    INTENT_ROW,
    PROPOSAL_ROW,
    RUN_ROW,
    SKILL_EDIT_ROW,
    IntentRow,
    ProposalRow,
    RunRow,
    SkillEditRow,
    query_as,
)
from .exporttypes import (
    ApplyAttempt,
    Child,
    DecisionAction,
    DecisionKind,
    Export,
    ExportMeta,
    Harness,
    Intent,
    Learning,
    Proposal,
    Run,
    RunOutcome,
    RunStatus,
    Session,
    SkillAbsence,
    Trend,
    Turn,
)
from .hermes_data import Turn as HermesTurn
from .jsontypes import JsonValue


def writer_of(actor: str | None, sid: str | None, with_messages: Collection[str]) -> str | None:
    """Who wrote a ledger entry: the ledger actor, with `curator` split. Hermes writes `curator`
    both for the periodic curator and for the background review after a turn. The review's fork
    shares the turn's session id and is not persisted; the curator's fork has a session of its own,
    also not persisted. So a `curator` entry whose session has messages in state.db is the review
    (`review`); one without stays `curator` (a curator pass, or a review whose session is gone)."""
    if actor == "curator" and sid is not None and sid in with_messages:
        return "review"
    return actor


# ---------------------------------------------------------------- the plugin DB of one profile
class _OutcomeRow(TypedDict):
    tool_call_id: str
    error_sig: str
    error_category: str | None


class _TurnIntentRow(TypedDict):
    session_id: str
    user_message_id: int
    intent_id: str


class _DecisionRow(TypedDict):
    target_kind: DecisionKind
    target_id: str
    action: DecisionAction


_OUTCOME_ROW: Final = TypeAdapter(_OutcomeRow)
_TURN_INTENT_ROW: Final = TypeAdapter(_TurnIntentRow)
_DECISION_ROW: Final = TypeAdapter(_DecisionRow)

_Target = tuple[DecisionKind, str]  # (target_kind, target_id)


class _PluginRows(TypedDict):
    outcomes: dict[str, tuple[str, str | None]]  # tool_call_id -> (error_sig, error_category)
    edits: dict[str, SkillEditRow]  # ledger entry id ->
    intents: dict[str, IntentRow]
    turn_intent: dict[tuple[str, int], str]  # (session id, user message id) -> intent id
    runs: dict[tuple[str, str], RunRow]  # (intent id, session id) ->
    proposals: list[ProposalRow]
    decisions: dict[_Target, DecisionAction]
    applied: dict[_Target, ApplyAttempt]
    # a staged success later than `applied` whose pending write is gone: approved or rejected
    released: dict[_Target, ApplyAttempt]
    attempts: dict[_Target, ApplyAttempt]


def _empty_rows() -> _PluginRows:
    return {
        "outcomes": {},
        "edits": {},
        "intents": {},
        "turn_intent": {},
        "runs": {},
        "proposals": [],
        "decisions": {},
        "applied": {},
        "released": {},
        "attempts": {},
    }


def _plugin_rows(home: Path) -> _PluginRows:
    """Layer 1/2 rows of one profile's plugin DB; empty when the plugin has not written one yet."""
    out = _empty_rows()
    path = store.db_path(home)
    if not path.exists():
        return out
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        for o in query_as(
            conn,
            _OUTCOME_ROW,
            "select tool_call_id, error_sig, error_category from tool_outcome "
            "where error_sig is not null",
        ):
            out["outcomes"][o["tool_call_id"]] = (o["error_sig"], o["error_category"])
        for e in query_as(conn, SKILL_EDIT_ROW, "select * from skill_edit"):
            out["edits"][e["ledger_entry_id"]] = e
        for i in query_as(conn, INTENT_ROW, "select * from intent"):
            out["intents"][i["id"]] = i
        for ti in query_as(
            conn,
            _TURN_INTENT_ROW,
            "select session_id, user_message_id, intent_id from turn_intent",
        ):
            out["turn_intent"][(ti["session_id"], ti["user_message_id"])] = ti["intent_id"]
        for r in query_as(conn, RUN_ROW, "select * from run"):
            out["runs"][(r["intent_id"], r["session_id"])] = r
        out["proposals"] = query_as(conn, PROPOSAL_ROW, "select * from proposal")
        for d in query_as(
            conn,
            _DECISION_ROW,
            "select target_kind, target_id, action from decision order by decided_at, id",
        ):
            out["decisions"][(d["target_kind"], d["target_id"])] = d["action"]  # latest one wins
        # apply_log exists once the DB was opened by this version (store.SCHEMA_VERSION 2)
        try:
            for a in query_as(
                conn, APPLY_LOG_ROW, "select * from apply_log order by applied_at, id"
            ):
                key = (a["target_kind"], a["target_id"])
                row: ApplyAttempt = {
                    "outcome": a["outcome"],
                    "message": a["message"],
                    "ledger_entry": a["ledger_entry"],
                    "pending_id": a["pending_id"],
                    "at": a["applied_at"],
                    "by": a["applied_by"],
                }
                out["attempts"][key] = row  # the latest attempt of any outcome
                # the latest success: applied, or staged while Hermes still holds the pending write.
                # A staged one whose pending write is gone was approved or rejected; build tells
                # them apart in the ledger (_approved)
                if a["outcome"] == "applied" or (
                    a["outcome"] == "staged"
                    and a["pending_id"]
                    and (home / "pending" / "skills" / f"{a['pending_id']}.json").exists()
                ):
                    out["applied"][key] = row
                    out["released"].pop(key, None)
                elif a["outcome"] == "staged" and a["pending_id"]:
                    out["released"][key] = row
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    return out


def _window(home: Path) -> float | None:
    """The profile's history window; None (the whole history) also when its plugin DB cannot be
    opened, which _plugin_rows then reports."""
    try:
        return store.read_window(home)
    except (sqlite3.Error, OSError):
        return None


def _applied(
    a: ApplyAttempt | None, profile: str, rolled_back: Mapping[tuple[str, str | None], str]
) -> ApplyAttempt | None:
    """Latest successful apply of a proposal; an applied one whose ledger entry was rolled back
    since (by Argus or `hermes curator rollback`) carries `rolled_back` = the rollback entry."""
    rb = rolled_back.get((profile, a["ledger_entry"])) if a and a["outcome"] == "applied" else None
    return {**a, "rolled_back": rb} if a and rb else a


# a ledger write as _approved reads it: entry id, action, ts, the SKILL.md it changed (path, text
# before, text after)
_Write = tuple[str, str | None, float, str, str | None, str | None]
_Writes: TypeAlias = dict[tuple[str, str | None], list[_Write]]  # (profile, skill) -> oldest first


def _approved(
    pr: ProposalRow, staged: ApplyAttempt, writes: Sequence[_Write], target: str | None
) -> ApplyAttempt | None:
    """A proposal's staged apply whose pending write is gone, as `applied` with its ledger entry
    when the ledger has that write since: approved, under the approver's actor. None otherwise:
    rejected (or approved before the history window). `target` is the resolved SKILL.md an apply of
    this proposal stages (apply writes only in the profile's skills/); a write to another file of
    the same skill name (another category, root or nesting) is not this approval."""
    for entry, action, ts, path, before, after in writes:
        if target is not None and os.path.realpath(path) != target:
            continue
        if ts >= staged["at"] and apply.written_by(pr, action, before, after):
            return {
                **staged,
                "outcome": "applied",
                "ledger_entry": entry,
                "message": f"staged as pending write {staged['pending_id']}, approved since",
            }
    return None


def _final(intents: dict[str, IntentRow], iid: str | None) -> str | None:
    """The live intent `iid` was merged into (itself when live); None when unknown."""
    final = derive.resolve(intents, iid) if iid is not None else None
    return final if final in intents else None


def _count_diff(d: str | None) -> tuple[int, int]:
    lines = (d or "").splitlines()
    return (
        sum(1 for x in lines if x.startswith("+") and not x.startswith("+++")),
        sum(1 for x in lines if x.startswith("-") and not x.startswith("---")),
    )


def _run_status(reply: str, signal: str | None) -> RunStatus:
    """Cron run result in Hermes terms: delivered report, [SILENT], [CRON_FAILURE], no reply."""
    r = (reply or "").strip()
    if signal == "cron_failure" or "[CRON_FAILURE]" in r:
        return "failed"
    if signal == "silent" or r.startswith("[SILENT]"):
        return "silent"
    return "reported" if r else "empty"


def _installed(home: Path) -> int:
    return sum(
        1 for root in store.skill_roots(home) if root.is_dir() for _ in root.rglob("SKILL.md")
    )


# ---------------------------------------------------------------- a proposal's evidence
class _Evidence(TypedDict, total=False):
    """proposal.evidence as derive.proposals writes it."""

    intents: list[str]
    error_sigs: list[str]
    overlaps: list[str]


_EVIDENCE: Final = TypeAdapter(_Evidence)


def _evidence(text: str | None) -> _Evidence:
    """A proposal's evidence; empty when it is not what derive writes."""
    try:
        return _EVIDENCE.validate_json(text or "{}")
    except ValueError:
        return {}


_Plug: TypeAlias = dict[str, _PluginRows]
_IntentKey: TypeAlias = tuple[str, str]  # (profile, plugin intent id)
_IntentAt: TypeAlias = Callable[[str | None, float], str | None]


def _load(homes: dict[str, Path]) -> tuple[list[hermes_data.Dump], _Plug, dict[str, str]]:
    """Each profile's history (from its window on) and plugin rows; a broken profile is reported
    in the errors, never hides the others."""
    dumps: list[hermes_data.Dump] = []
    plug: _Plug = {}
    errors: dict[str, str] = {}
    for name, home in homes.items():
        try:
            # from the profile's history window on (store.WINDOW; unset: the whole history)
            d = hermes_data.load_dump(home, since=_window(home))
        except hermes_data.HermesFormatError as e:
            errors[name] = str(e)
            continue
        d["profile"] = name
        dumps.append(d)
        try:
            plug[name] = _plugin_rows(home)
        except (sqlite3.Error, OSError) as e:
            errors[name] = f"plugin data: {e}"
            plug[name] = _empty_rows()
    return dumps, plug, errors


def _turns(
    turns: list[HermesTurn], plug: _Plug, reads: list[hermes_data.SkillRead]
) -> tuple[dict[int, Turn], dict[str, str]]:
    """Each turn (by id() of the Hermes turn) with tool counts, error signatures (hook outcome
    first, else the result text) and skill reads; and one excerpt per error signature."""
    examples: dict[str, str] = {}
    by_sid: dict[str, list[HermesTurn]] = {}
    skill_reads: dict[int, dict[str, int]] = {}
    for t in turns:
        by_sid.setdefault(t["sid"], []).append(t)
    for tt in by_sid.values():
        tt.sort(key=lambda t: t["ts"])
    for sid, ts, name in reads:
        tt = [t for t in by_sid.get(sid, []) if t["ts"] <= ts + 1]
        if tt:
            sk = skill_reads.setdefault(id(tt[-1]), {})
            sk[name] = sk.get(name, 0) + 1
    out: dict[int, Turn] = {}
    for t in turns:
        p = plug.get(t["profile"])
        outcomes = p["outcomes"] if p is not None else {}
        tc: dict[str, list[int]] = {}
        errs: list[list[str]] = []
        for tool, called_sig, excerpt, tcid in t["calls"]:
            sig, cat = outcomes[tcid] if tcid and tcid in outcomes else (called_sig, None)
            x = tc.setdefault(tool, [0, 0])
            x[0] += 1
            if sig:
                x[1] += 1
                cat = cat or signals.err_category(sig)
                errs.append([tool, sig, cat])
                if excerpt and f"{tool}|{sig}" not in examples:
                    examples[f"{tool}|{sig}"] = excerpt
        rt = list(t["runtime"])
        out[id(t)] = {
            "ts": t["ts"],
            "sid": t["sid"],
            "profile": t["profile"],
            "source": t["source"],
            "text": t["text"],
            "view": t["view"],
            "reply": t["reply"],
            "tools": t["tools"],
            "tc": tc,
            "errs": errs,
            "rt": rt,
            "dur": max(0, round((t["end"] or t["ts"]) - t["ts"])),
            "rtcat": [signals.err_category(r) for r in rt],
            "sk": skill_reads.get(id(t), {}),
        }
    return out, examples


def _intent(eid: str, profile: str, row: IntentRow) -> Intent:
    return {
        "id": eid,
        "title": row["title"],
        "kind": row["kind"],
        "goal": row["goal"] or "",
        "sessions": [],
        "profiles": [profile],
        "children": [],
        "turns": [],
        "runs_out": {},
        "judgement": {
            "status": row["status"] or "unclear",
            "who": row["who"] or "team",
            "stable": row["stable"] or "",
            "risk": row["risk"] or "",
            "headline": row["headline"] or "",
            "outcome": row["outcome"] or "",
            "consistency": row["consistency"] or "",
            "friction": row["friction"] or "",
            "next": row["next"] or "",
            "attention": row["attention"] or 0,
        },
    }


def _intents(
    turns: list[HermesTurn], plug: _Plug, out_turn: dict[int, Turn]
) -> tuple[dict[str, Intent], dict[_IntentKey, str], dict[str, list[tuple[float, str]]]]:
    """The intents of the turns (merged ones skipped: their turns follow merged_into), by export
    id; the export id of each (profile, plugin intent id); each session's (turn ts, intent)."""
    ids: dict[_IntentKey, str] = {}
    intents: dict[str, Intent] = {}
    t2i: dict[str, list[tuple[float, str]]] = {}
    for t in turns:
        p = plug.get(t["profile"])
        iid = _final(p["intents"], p["turn_intent"].get((t["sid"], t["msg_id"]))) if p else None
        if not iid:
            continue
        key = (t["profile"], iid)
        if key not in ids:
            eid = iid if iid not in set(ids.values()) else f"{t['profile']}.{iid}"
            ids[key] = eid
            intents[eid] = _intent(eid, t["profile"], plug[t["profile"]]["intents"][iid])
        it = intents[ids[key]]
        it["turns"].append(out_turn[id(t)])
        if t["sid"] not in it["sessions"]:
            it["sessions"].append(t["sid"])
        t2i.setdefault(t["sid"], []).append((t["ts"], it["id"]))
    return intents, ids, t2i


def _intent_at(
    t2i: dict[str, list[tuple[float, str]]], sessions: dict[str, hermes_data.Session]
) -> _IntentAt:
    """Intent of a session at a time: its latest turn's by then, else its parent session's."""

    def at(sid: str | None, ts: float, depth: int = 0) -> str | None:
        cands = [iid for t_ts, iid in t2i.get(sid, []) if t_ts <= ts + 1] if sid is not None else []
        if cands:
            return cands[-1]
        s = sessions.get(sid) if sid is not None else None
        par = s["parent"] if s is not None else None
        return at(par, ts, depth + 1) if par and par != sid and depth < 10 else None

    return at


def _add_runs(intents: dict[str, Intent], ids: dict[_IntentKey, str], plug: _Plug) -> None:
    """Model results per run and, for a routine, its runs."""
    for (profile, iid), eid in ids.items():
        it, runs = intents[eid], plug[profile]["runs"]
        for sid in it["sessions"]:
            r = runs.get((iid, sid))
            if r and r["model_ok"] is not None:
                it["runs_out"][sid] = RunOutcome(
                    ok=bool(r["model_ok"]), result=r["model_result"] or ""
                )
        if it["kind"] == "routine":
            it_runs: list[Run] = []
            it["runs"] = it_runs
            for sid in it["sessions"]:
                tt_out = [t for t in it["turns"] if t["sid"] == sid]
                rr = runs.get((iid, sid))
                reply = tt_out[-1]["reply"]
                it_runs.append(
                    {
                        "ts": tt_out[0]["ts"],
                        "status": _run_status(reply, rr["signal_status"] if rr else None),
                        "tools": sum(t["tools"] for t in tt_out),
                        "reply": reply,
                    }
                )
            it_runs.sort(key=lambda r: r["ts"])


def _add_children(
    intents: dict[str, Intent], children: list[hermes_data.Session], at: _IntentAt
) -> None:
    """Subagent sessions under the intent of the turn that spawned them."""
    for ch in children:
        iid = at(ch["parent"], ch["start"]) if ch["parent"] else None
        if iid:
            child: Child = {
                "sid": ch["id"],
                "title": (ch["title"] or "").replace("Subagent: ", ""),
                "start": ch["start"],
                "tools": ch["tools"] or 0,
            }
            intents[iid]["children"].append(child)


def _learnings(
    dumps: list[hermes_data.Dump], plug: _Plug, intents: dict[str, Intent], at: _IntentAt
) -> tuple[list[Learning], list[signals.BlameEdit], dict[tuple[str, str | None], str], _Writes]:
    """Applied skill edits (ledger + blobs) annotated by the plugin's review, in time order; the
    texts blame needs; (profile, ledger entry id) -> the entry that rolled it back; and each
    skill's patch/create entries, for _approved."""
    edited: list[tuple[Learning, signals.BlameEdit]] = []
    rolled_back: dict[tuple[str, str | None], str] = {}
    writes: _Writes = {}
    for d in dumps:
        name, blobs, edits = d["profile"], d["blobs"], plug[d["profile"]]["edits"]
        with_messages = {m["session_id"] for m in d["messages"]}
        for en in d["ledger"]:
            evidence: hermes_data.LedgerEvidence = en.get("evidence") or {}
            target = evidence.get("rollback_target")
            if en.get("action") == "rollback" and target:
                rolled_back[(name, target)] = en["id"]
            edit_row = edits.get(en["id"])
            ledger_sid = evidence.get("session_id") or (
                edit_row["session_id"] if edit_row else None
            )
            ts = en["ts_epoch"] or (edit_row["ts"] if edit_row else 0) or 0
            changed = hermes_data.changed_md(en, blobs)
            skill_md = next(
                ((fp, *ba) for fp, ba in changed.items() if fp.endswith("/SKILL.md")), None
            )
            if skill_md is not None and en.get("action") in ("patch", "create"):
                writes.setdefault((name, en.get("skill")), []).append(
                    (en["id"], en.get("action"), ts, *skill_md)
                )
            for fpath, (b, a) in changed.items():
                rel = fpath.split("/skills/", 1)[-1]
                diff = signals.unified(b, a)
                plus, minus = _count_diff(diff)
                iid = at(ledger_sid, ts) if ledger_sid else None
                db, da = signals.desc_of(b), signals.desc_of(a)
                learning: Learning = {
                    "id": f"{en['id']}:{rel}",
                    "entry": en["id"],
                    "profile": name,
                    "ts": ts,
                    "actor": en.get("actor"),
                    "writer": writer_of(en.get("actor"), ledger_sid, with_messages),
                    "action": en.get("action"),
                    "skill": en.get("skill"),
                    "file": rel,
                    "sid": ledger_sid,
                    "plus": plus,
                    "minus": minus,
                    "words_before": len((b or "").split()),
                    "words_after": len((a or "").split()),
                    "diff": diff[:8000],
                    "intent": iid,
                    "intent_who": intents[iid]["judgement"]["who"] if iid else None,
                    "desc_change": f'"{db}" → "{da}"' if db and da and db != da else None,
                }
                # `argus apply --rollback` of the entry: latest success, latest attempt
                key_t: _Target = ("skill_edit", en["id"])
                applied = plug[name]["applied"].get(key_t)
                if applied:
                    learning["applied"] = applied
                attempt = plug[name]["attempts"].get(key_t)
                if attempt:
                    learning["last_attempt"] = attempt
                if edit_row and (edit_row["evidence"] or edit_row["learned"]):
                    learning["review"] = {
                        "learned": edit_row["learned"] or "",
                        "trigger": edit_row["trigger"] or "",
                        "evidence_in_run": edit_row["evidence"] or "unreviewed",
                    }
                texts: signals.BlameEdit = {
                    "id": learning["id"],
                    "ts": ts,
                    "profile": name,
                    "file": rel,
                    "_before": b,
                    "_after": a,
                }
                edited.append((learning, texts))

    def edit_ts(pair: tuple[Learning, signals.BlameEdit]) -> float:
        return pair[0]["ts"]

    edited.sort(key=edit_ts)
    return [L for L, _ in edited], [texts for _, texts in edited], rolled_back, writes


def _trends(learnings: list[Learning]) -> dict[str, Trend]:
    """Deterministic skill flags: a skill whose edits are often without evidence. Only Hermes' own
    edits count: an edit a person wrote (ledger actor `user`, e.g. an accepted proposal applied from
    the dashboard) neither adds evidence nor dilutes the share of edits without it."""
    trends: dict[str, Trend] = {}
    groups: dict[str, list[Learning]] = {}
    for le in learnings:
        if le["action"] not in derive.NO_DIFF_ACTIONS and le["writer"] != "user":
            groups.setdefault(f"{le['profile']}:{le['skill']}", []).append(le)
    for k, es in groups.items():
        spec = sum(
            1
            for le in es
            if (rv := le.get("review")) is not None and rv["evidence_in_run"] == "speculative"
        )
        if spec >= 3 and spec >= 0.3 * len(es):
            trends[k] = {
                "verdict": "needs review",
                "summary": f"{spec} of {len(es)} edits without evidence",
            }
    return trends


def _proposals(
    homes: dict[str, Path],
    plug: _Plug,
    ids: dict[_IntentKey, str],
    intents: dict[str, Intent],
    sig_keys: list[str],
    rolled_back: dict[tuple[str, str | None], str],
    writes: _Writes,
) -> list[Proposal]:
    """The plugin's proposals, linked to the export's intents and error signatures."""
    proposals: list[Proposal] = []
    for name, p in plug.items():
        present = store.skill_files(homes[name])
        for pr in p["proposals"]:
            ev = _evidence(pr["evidence"])
            its = [
                ids[(name, f)]
                for f in (_final(p["intents"], i) for i in ev.get("intents", []))
                if f is not None and (name, f) in ids
            ]
            pats: list[str] = []
            for sig in ev.get("error_sigs", []):
                pats += (
                    [sig] if "|" in sig else [k for k in sig_keys if k.split("|", 1)[1] == sig][:1]
                )
            plus, minus = _count_diff(pr["diff"])
            tss = [t_out["ts"] for i in its for t_out in intents[i]["turns"]]
            key_p: _Target = ("proposal", pr["id"])
            staged, attempt = p["released"].get(key_p), p["attempts"].get(key_p)
            approved = (
                _approved(
                    pr,
                    staged,
                    writes.get((name, pr["skill_name"].removeprefix("new:")), []),
                    os.path.realpath(homes[name] / "skills" / pr["file"]) if pr["file"] else None,
                )
                if staged
                else None
            )
            proposals.append(
                {
                    "id": pr["id"],
                    "skill": ("new:" if pr["is_new_skill"] else "") + pr["skill_name"],
                    "rule": pr["rule"],
                    "why": pr["why"] or "",
                    "intents": list(dict.fromkeys(its, True)),
                    "patterns": pats,
                    "overlaps": list[JsonValue](ev.get("overlaps", [])),
                    "confidence": pr["confidence"] or "medium",
                    "profile": name,
                    "file": pr["file"] or "",
                    "diff": pr["diff"] or "",
                    "plus": plus,
                    "minus": minus,
                    "ts": max(tss) if tss else time.time(),
                    "applied": _applied(approved or p["applied"].get(key_p), name, rolled_back),
                    "last_attempt": approved if approved and attempt is staged else attempt,
                    "skill_gone": not pr["is_new_skill"] and pr["skill_name"] not in present,
                    "read_only": (f := present.get(pr["skill_name"])) is not None
                    and not pr["is_new_skill"]
                    and not store.skill_writable(f),
                }
            )
    return proposals


def _decisions(
    plug: _Plug, learnings: list[Learning], proposals: list[Proposal]
) -> dict[str, DecisionAction]:
    """The person's latest decision per target (layer 3), keyed like app.js
    (`edit:<profile>:<learning id>`, `prop:<profile>:<proposal id>`): ids repeat across profiles
    (a proposal id is hash(skill, rule)). A skill edit decision targets its ledger entry (rollback
    granularity), so it applies to every file of the entry."""
    decisions: dict[str, DecisionAction] = {}
    for le in learnings:
        act = plug[le["profile"]]["decisions"].get(("skill_edit", le["entry"]))
        if act:
            decisions[f"edit:{le['profile']}:{le['id']}"] = act
    for prop in proposals:
        act = plug[prop["profile"]]["decisions"].get(("proposal", prop["id"]))
        if act:
            decisions[f"prop:{prop['profile']}:{prop['id']}"] = act
    return decisions


def _sessions(
    sessions: dict[str, hermes_data.Session], intents: dict[str, Intent]
) -> dict[str, Session]:
    """The sessions referenced by intents and their subagents."""
    keep = {s for it in intents.values() for s in it["sessions"]} | {
        c["sid"] for it in intents.values() for c in it["children"]
    }
    return {
        sid: {
            "id": sid,
            "profile": s["profile"],
            "source": s["source"],
            "title": s["title"] or "",
            "start": s["start"],
            "end": s["end"],
            "tools": s["tools"] or 0,
            "parent": s["parent"],
        }
        for sid, s in sessions.items()
        if sid in keep
    }


def _absent(
    homes: dict[str, Path], learnings: list[Learning], proposals: list[Proposal]
) -> dict[str, SkillAbsence]:
    """Skills with history here (ledger edits, proposals) that are no longer in their profile:
    "removed" when the last ledger entry is a cleanup (archive, delete), "missing" when the ledger
    still has it live, i.e. it was removed outside Hermes. Their proposals are obsolete."""
    have = {n: store.skill_files(h) for n, h in homes.items()}
    latest: dict[str, Learning] = {}
    for le in learnings:
        lk = f"{le['profile']}:{le['skill']}"
        if le["skill"] and (lk not in latest or le["ts"] >= latest[lk]["ts"]):
            latest[lk] = le
    refs = set(latest) | {
        f"{pr['profile']}:{pr['skill']}" for pr in proposals if not pr["skill"].startswith("new:")
    }
    absent: dict[str, SkillAbsence] = {}
    for ref in sorted(refs):
        prof, _, skill = ref.partition(":")
        if prof in have and skill not in have[prof]:
            hit = latest.get(ref)
            cleaned = hit is not None and hit["action"] in derive.NO_DIFF_ACTIONS
            absent[ref] = {
                "state": "removed" if cleaned else "missing",
                "ts": hit["ts"] if hit else None,
                "entry": hit["entry"] if hit else None,
                "action": hit["action"] if hit else None,
            }
    return absent


def build(root: Path, profiles: list[str] | None = None, label: str | None = None) -> Export:
    root = Path(root)
    homes = store.profile_homes(root)
    if profiles:
        homes = {k: v for k, v in homes.items() if k in profiles}
    dumps, plug, errors = _load(homes)
    turns, sessions, children = hermes_data.build_turns(dumps)
    out_turn, examples = _turns(turns, plug, hermes_data.skill_reads(dumps))
    intents, ids, t2i = _intents(turns, plug, out_turn)
    at = _intent_at(t2i, sessions)
    _add_runs(intents, ids, plug)
    _add_children(intents, children, at)
    learnings, texts, rolled_back, writes = _learnings(dumps, plug, intents, at)
    sig_keys = sorted(
        set(examples)
        | {f"{x[0]}|{x[1]}" for t_out in out_turn.values() for x in t_out["errs"]}
        | {f"runtime|{r}" for t_out in out_turn.values() for r in t_out["rt"]}
    )
    proposals = _proposals(homes, plug, ids, intents, sig_keys, rolled_back, writes)
    for it in intents.values():
        it["turns"].sort(key=lambda t: t["ts"])
    names = [d["profile"] for d in dumps]
    label = label or ("hermes" if root.name in (".hermes", "") else root.name.lstrip("."))
    entry: Harness = {
        "label": label,
        "desc": f"{len(names)} profile{'s' if len(names) != 1 else ''} · {root}",
        "profiles": {n: n for n in names},
        "intents": list(intents.values()),
        "learnings": learnings,
        "blame": signals.blame(texts),
        "sessions": _sessions(sessions, intents),
        "trends": _trends(learnings),
        "examples": examples,
        "proposals": proposals,
        "decisions": _decisions(plug, learnings, proposals),
        "installed": {n: _installed(homes[n]) for n in names},
        "absent": _absent(homes, learnings, proposals),
    }
    meta: ExportMeta = {"generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")}
    if errors:
        meta["errors"] = errors
    return {"meta": meta, "harnesses": {label: entry}}
