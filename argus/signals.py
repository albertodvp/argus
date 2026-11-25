"""Deterministic text signals, ported verbatim from the prototype (pipeline/intents.py):
turn cleaning, cron keys, tool/runtime error signatures, diffs and per-line blame."""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Final, Literal, TypeAlias

from typing_extensions import TypedDict

KindHint: TypeAlias = Literal["", "cron"]
ErrCategory: TypeAlias = Literal["runtime", "policy", "timeout", "env", "input", "exit"]

# ---------------------------------------------------------------- cleaning
DROP: Final = (
    "[ASYNC DELEGATION",
    "[IMPORTANT: Background process",
    "Gateway message origin",
    "[System:",
    "[SYSTEM:",
    "[Note: model was just switched",
    "[CONTEXT COMPACTION",
    "[STILL IN PROGRESS",
    "[Continuing toward your standing goal]",
)
CRON: Final = "[IMPORTANT: You are running as a scheduled cron job"
TS_PREFIX: Final = re.compile(
    r"^\[(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC\]\s*"
)
SPEAKER: Final = re.compile(r"^\[[A-Za-z0-9_.\-]{2,32}\]\s*")


def clean_text(c: str) -> tuple[str | None, str, KindHint]:
    """Return (text or None to drop, ui_view, kind_hint)."""
    if not c:
        return None, "", ""
    c = c.strip()
    if c.startswith(CRON):
        return c, "", "cron"
    if c.startswith("[Recent channel messages]"):
        if "[New message]" not in c:
            return None, "", ""
        c = c.rsplit("[New message]", 1)[1].strip()
    c = TS_PREFIX.sub("", c)
    c = SPEAKER.sub("", c)
    if c.startswith(DROP):
        return None, "", ""
    c = re.sub(r"\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*", "", c).replace(
        "[/OUT-OF-BAND USER MESSAGE]", ""
    )
    if re.match(r"\[[^\]\n]+ context\]", c) and "\n\n" in c:
        c = c.split("\n\n", 1)[1]
    view = ""
    m = re.match(r"\[User view\] (.*?)\n\n(.*)", c, re.S)
    if m:
        view, c = m.group(1), m.group(2)
    if c.startswith("Contesto fornito dall"):
        m = re.search(r"La mia domanda:\s*(.*)$", c, re.S)
        ctx = re.search(r'"nome":\s*"([^"]{0,80})', c)
        view = ("catalogo: " + ctx.group(1)) if ctx else "contesto catalogo"
        c = m.group(1) if m else c[-400:]
    c = re.sub(r"\[Image attached at: [^\]]+\]", "[immagine]", c)
    c = c.strip()
    return (c or None), view, ""


# Hermes' skill scaffolding (agent/skill_commands.py): a /skill or /bundle turn, or a channel's
# auto-loaded skill, arrives as a user message embedding whole skill bodies around what the person
# typed. Markers byte-identical to Hermes' builders.
SKILL_INVOKED: Final = "[IMPORTANT: The user has invoked the "
SKILL_LOADED: Final = "The full skill content is loaded below.]"
SKILL_INSTRUCTION: Final = (
    "The user has provided the following instruction alongside the skill invocation: "
)
RUNTIME_NOTE: Final = "\n\n[Runtime note:"
BUNDLE: Final = " skill bundle,"
BUNDLE_INSTRUCTION: Final = "\nUser instruction: "
BUNDLE_FIRST_SKILL: Final = "\n\n[Loaded as part of the "
AUTO_LOADED: Final = re.compile(r'^\[IMPORTANT: The "[^"]*" skill is auto-loaded\.')
SKILL_DIR_NOTE_END: Final = "then run them with the terminal tool using the absolute path."


def _between(c: str, start: int, marker: str, end: str = "") -> str | None:
    """The text after `marker` (found at `start`) up to `end`, or to the end without one."""
    if start < 0:
        return None
    rest = c[start + len(marker) :]
    cut = rest.find(end) if end else -1
    return (rest if cut < 0 else rest[:cut]).strip() or None


def human_text(c: str | None) -> str | None:
    """What a person typed in a user message, without what Hermes wraps around it: None for a
    message clean_text drops, a cron prompt, or a skill invocation without an instruction; the
    instruction of a /skill or /bundle invocation, or the text after the last loaded skill of an
    auto-loaded one, themselves unwrapped the same way; otherwise clean_text's text."""
    text, _view, hint = clean_text(c or "")
    if text is None or hint == "cron":
        return None
    if text.startswith(SKILL_INVOKED):
        # the kind is in Hermes' header line only: a skill body may mention either marker
        header = text.split("\n", 1)[0]
        if BUNDLE in header:  # the instruction precedes the loaded skills, in the header block
            end = text.find(BUNDLE_FIRST_SKILL)
            head = text if end < 0 else text[:end]
            return human_text(_between(head, head.find(BUNDLE_INSTRUCTION), BUNDLE_INSTRUCTION))
        if SKILL_LOADED in header:  # it follows the body, which may quote the marker: the last one
            inner = _between(text, text.rfind(SKILL_INSTRUCTION), SKILL_INSTRUCTION, RUNTIME_NOTE)
            return human_text(inner)  # a cron job's prompt can be the instruction
        return None
    if AUTO_LOADED.match(text):
        end = text.rfind(SKILL_DIR_NOTE_END)
        return human_text(text[end + len(SKILL_DIR_NOTE_END) :]) if end >= 0 else None
    return text


def cron_key(title: str | None) -> str:
    t = (title or "").split("|")[0].split("·")[0].strip()
    return t or "cron"


def to_ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


# ---------------------------------------------------------------- deterministic runtime signals
_ERRWORD: Final = re.compile(
    r"(traceback|error|exception|denied|not found|no such|failed|fatal|refused|timed out|timeout|"
    r"forbidden|unauthori[sz]ed|invalid|cannot|can\'t)",
    re.I,
)


def norm_err(s: str) -> str:
    s = s.replace("\\n", "\n").replace('\\"', '"').strip()
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    line = next((ln for ln in lines if _ERRWORD.search(ln)), lines[0] if lines else "")
    line = re.sub(r"https?://\S+", "<url>", line)
    line = re.sub(r"(?<![\w])(~|\.{0,2})?/[\w.\-@~+/]+", "<path>", line)
    line = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b[0-9a-f]{10,}\b", "<id>", line, flags=re.I)
    line = re.sub(r"(?<![\w.])\d+(\.\d+)?\b", "N", line)
    line = re.sub(r"\s+", " ", line)
    return line[:110].lower()


def tool_error(c: str | None) -> str | None:
    """Deterministic failure signature of one tool result, or None."""
    if not c:
        return None
    if c.startswith("[Orphan recovery"):
        return "orphan recovery: side-effecting tool interrupted, effect unknown"
    m = re.search(r'"error":\s*"((?:[^"\\]|\\.){3,300})"', c)
    if m:
        return norm_err(m.group(1))
    m = re.search(r'"exit_code":\s*(-?[1-9]\d*)', c)
    if m:
        code = m.group(1)
        out = re.search(r'"output":\s*"((?:[^"\\]|\\.){0,2000})', c)
        msg = norm_err(out.group(1)) if out and _ERRWORD.search(out.group(1)) else ""
        return f"exit {code}" + (f": {msg}" if msg else "")
    if re.search(r'"(success|ok)":\s*false|"status":\s*"(error|failed)"|"isError":\s*true', c):
        m = re.search(r'"(message|detail|reason)":\s*"((?:[^"\\]|\\.){3,200})"', c)
        return norm_err(m.group(2)) if m else "success=false"
    head = c[:300]
    if (
        re.match(r"\s*(Error|Tool error|Exception|PermissionError)", head)
        or "Traceback (most recent call last)" in head
    ):
        return norm_err(head)
    return None


def runtime_signal(reply: str) -> str | None:
    """Gateway/agent-loop conditions visible in the assistant's own text."""
    r = reply[:200]
    if r.startswith("Your request was not processed"):
        return "gateway: request not processed"
    if r.startswith("Operation interrupted"):
        return "loop: turn interrupted" + (" waiting for model" if "waiting for model" in r else "")
    return None


def err_category(sig: str) -> ErrCategory:
    """Coarse operator category for a failure signature."""
    s = sig.lower()
    if s.startswith(("gateway:", "loop:")) or "orphan recovery" in s:
        return "runtime"
    if re.search(
        r"blocked|dangerous|approval|forbidden|denied|not permitted|permission|unauthori|policy", s
    ):
        return "policy"
    if re.search(r"timed out|timeout|exit 124|exit 130|interrupted", s):
        return "timeout"
    if re.search(
        r"command not found|no module named|failed to load|docker api|could not connect|"
        r"connection refused|not a git repository|exit 127|ssh: connect",
        s,
    ):
        return "env"
    if re.search(
        r"not found|no such file|validation|unrecognized argument|found n matches|invalid|must be|"
        r"required",
        s,
    ):
        return "input"
    return "exit"


# ---------------------------------------------------------------- skill files
class BlameEdit(TypedDict):
    """One applied edit of a skill file: its text before and after (None when absent)."""

    id: str
    ts: float
    profile: str
    file: str
    _before: str | None
    _after: str | None


class BlameLine(TypedDict):
    """One line of a file's final text (`t`) and the id of the edit that introduced it (`o`)."""

    t: str
    o: str


def desc_of(text: str | None) -> str | None:
    m = re.search(r"^description:\s*(.+)$", text or "", re.M)
    if not m:
        return None
    desc: str = m.group(1)
    return desc.strip().strip("\"'")


def unified(a: str | None, b: str | None) -> str:
    return "\n".join(
        list(
            difflib.unified_diff((a or "").splitlines(), (b or "").splitlines(), lineterm="", n=1)
        )[2:]
    )


def blame(edits: Sequence[BlameEdit]) -> dict[str, list[BlameLine]]:
    """Per skill file: final text annotated with the edit that introduced each line ('base' =
    before the window)."""
    files: dict[tuple[str, str], list[BlameEdit]] = {}
    ordered = list(edits)
    ordered.sort(key=lambda x: x["ts"])
    for e in ordered:
        files.setdefault((e["profile"], e["file"]), []).append(e)
    out: dict[str, list[BlameLine]] = {}
    for (prof, f), es in files.items():
        lines = [(ln, "base") for ln in (es[0]["_before"] or "").splitlines()]
        for e in es:
            prev = [ln for ln, _ in lines]
            before = e["_before"]
            if before is not None and before.splitlines() != prev:
                # an untracked change happened in between: re-anchor on this edit's before, keeping
                # matching origins
                base = before.splitlines()
                sm = difflib.SequenceMatcher(None, prev, base, autojunk=False)
                nl: list[tuple[str, str]] = []
                for op, i1, i2, j1, j2 in sm.get_opcodes():
                    nl += (
                        lines[i1:i2] if op == "equal" else [(ln, "untracked") for ln in base[j1:j2]]
                    )
                lines, prev = nl, base
            new = (e["_after"] or "").splitlines()
            sm = difflib.SequenceMatcher(None, prev, new, autojunk=False)
            nl = []
            for op, i1, i2, j1, j2 in sm.get_opcodes():
                nl += lines[i1:i2] if op == "equal" else [(ln, e["id"]) for ln in new[j1:j2]]
            lines = nl
        out[f"{prof}:{f}"] = [{"t": ln, "o": o} for ln, o in lines]
    return out
