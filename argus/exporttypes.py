"""The JSON the dashboard reads: `export.build` (`/api/plugins/argus/data`, `window.DATA` in app.js)
and the other dashboard routes (`/status`, `/refresh`, `/decision`, `/apply`).

Key order follows what the builders emit. Values copied verbatim from JSON Argus does not shape
(a proposal's evidence, a run report, the apply child's answer) stay `JsonValue`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, NotRequired, TypeAlias

from typing_extensions import TypedDict

from .jsontypes import JsonValue
from .signals import BlameLine

__all__ = [
    "ApplyAttempt",
    "ApplyBody",
    "ApplyOutcome",
    "BlameLine",
    "Child",
    "DecisionAction",
    "DecisionDone",
    "DecisionKind",
    "ErrorBody",
    "Export",
    "ExportMeta",
    "Harness",
    "Intent",
    "Judgement",
    "LastDerive",
    "Learning",
    "ProfileCounts",
    "Proposal",
    "RefreshBody",
    "Review",
    "Run",
    "RunError",
    "RunOutcome",
    "RunStatus",
    "Session",
    "Status",
    "Trend",
    "Turn",
]

DecisionKind: TypeAlias = Literal["skill_edit", "proposal"]
DecisionAction: TypeAlias = Literal["keep", "reject", "accept", "dismiss"]
ApplyOutcome: TypeAlias = Literal["applied", "staged", "refused", "failed", "unknown"]
RunStatus: TypeAlias = Literal["failed", "silent", "reported", "empty"]


# ---------------------------------------------------------------- /data (export.build)
class Turn(TypedDict):
    ts: float
    sid: str
    profile: str
    source: str
    text: str
    view: str
    reply: str
    tools: int
    tc: dict[str, list[int]]  # tool -> [calls, failed calls]
    errs: list[list[str]]  # [tool, error signature, category]
    rt: list[str]  # runtime signals
    dur: int  # seconds
    rtcat: list[str]  # category of each runtime signal
    sk: dict[str, int]  # skill -> explicit reads during the turn


class Child(TypedDict):
    """A subagent session started during one of the intent's turns."""

    sid: str
    title: str
    start: float
    tools: int


class RunOutcome(TypedDict):
    ok: bool
    result: str


class Judgement(TypedDict):
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


class Run(TypedDict):
    """One session of a routine (cron) intent."""

    ts: float
    status: RunStatus
    tools: int
    reply: str


class Intent(TypedDict):
    id: str
    title: str
    kind: str
    goal: str
    sessions: list[str]
    profiles: list[str]
    children: list[Child]
    turns: list[Turn]
    runs_out: dict[str, RunOutcome]  # session id -> the model's verdict on the run
    judgement: Judgement
    runs: NotRequired[list[Run]]  # routine intents only


class ApplyAttempt(TypedDict):
    """One apply_log row (an `argus apply` attempt)."""

    outcome: ApplyOutcome
    message: str | None
    ledger_entry: str | None
    pending_id: str | None
    at: float
    by: str | None  # cli | dashboard | auto (the auto_apply policy), `--force` appended when forced
    # proposals only, on an applied one whose ledger entry was rolled back since: the rollback's id
    rolled_back: NotRequired[str]


class Review(TypedDict):
    learned: str
    trigger: str
    evidence_in_run: str


class Learning(TypedDict):
    """One skill file changed by one curator ledger entry."""

    id: str  # "<ledger entry id>:<file>"
    entry: str  # ledger entry id
    profile: str
    ts: float
    actor: str | None  # ledger actor: agent | curator | user
    writer: str | None  # actor with `curator` split: review (background review) | curator
    action: str | None
    skill: str | None
    file: str
    sid: str | None
    plus: int
    minus: int
    words_before: int
    words_after: int
    diff: str
    intent: str | None
    intent_who: str | None
    desc_change: str | None
    applied: NotRequired[ApplyAttempt]
    last_attempt: NotRequired[ApplyAttempt]
    review: NotRequired[Review]


class Session(TypedDict):
    id: str
    profile: str
    source: str
    title: str
    start: float
    end: float
    tools: int
    parent: str | None


class Trend(TypedDict):
    verdict: str
    summary: str


class Proposal(TypedDict):
    id: str
    skill: str
    rule: str
    why: str
    intents: list[str]
    patterns: list[str]
    overlaps: JsonValue
    confidence: str
    profile: str
    file: str
    diff: str
    plus: int
    minus: int
    ts: float
    applied: ApplyAttempt | None
    last_attempt: ApplyAttempt | None
    skill_gone: bool  # an existing-skill proposal whose skill is no longer in the profile
    read_only: bool  # its skill's SKILL.md cannot be written here (e.g. a read-only external dir)


class SkillAbsence(TypedDict):
    """A skill with history in the export that is no longer in its profile."""

    state: Literal["removed", "missing"]  # removed: by a ledger cleanup; missing: outside Hermes
    ts: float | None  # its last ledger entry in the window, if any
    entry: str | None
    action: str | None


class Harness(TypedDict):
    """One Hermes install (every profile of it)."""

    label: str
    desc: str
    profiles: dict[str, str]
    intents: list[Intent]
    learnings: list[Learning]
    blame: dict[str, list[BlameLine]]  # "<profile>:<file>" -> final text by line
    sessions: dict[str, Session]
    trends: dict[str, Trend]  # "<profile>:<skill>" -> flag
    examples: dict[str, str]  # "<tool>|<signature>" -> excerpt
    proposals: list[Proposal]
    decisions: dict[str, DecisionAction]  # "edit:<profile>:<learning id>" | "prop:<profile>:<id>"
    installed: dict[str, int]  # profile -> installed skills
    absent: dict[str, SkillAbsence]  # "<profile>:<skill>" -> why it is no longer there


class ExportMeta(TypedDict):
    generated: str
    errors: NotRequired[dict[str, str]]  # profile -> why it is missing or incomplete


class Export(TypedDict):
    meta: ExportMeta
    harnesses: dict[str, Harness]


# ---------------------------------------------------------------- /status
class ProfileCounts(TypedDict):
    intents: int
    edits_without_evidence: int
    pending_proposals: int


class LastDerive(TypedDict):
    profile: str
    file: str
    ts: float
    # the run report as read (any JSON, not validated), None when large; absent when unreadable
    report: NotRequired[object]


class RunError(TypedDict):
    """The newest finished derive, when it exited non-zero."""

    exit_code: int
    at: float
    log: str
    profile: str


class Status(TypedDict):
    profiles: dict[str, ProfileCounts]
    intents: int
    edits_without_evidence: int
    pending_proposals: int
    last_derive: LastDerive | None
    last_run_error: RunError | None
    running: bool
    errors: dict[str, str]


# ---------------------------------------------------------------- /refresh, /decision, /apply
class RefreshBody(TypedDict):
    started: bool
    running: bool
    error: NotRequired[str]


class ErrorBody(TypedDict):
    error: str


class DecisionDone(TypedDict):
    ok: Literal[True]
    profile: str
    target_kind: DecisionKind
    target_id: str
    action: DecisionAction | Literal["clear"]
    rows: int


ApplyBody: TypeAlias = Mapping[str, object]
"""/apply: the apply result with `profile` added: `apply.ApplyResult` (the apply child prints the
same, read back as any JSON object with `outcome`)."""
