"""`hermes argus apply` and the dashboard's apply button: carry out a decision through Hermes'
own ledgered functions, in the profile whose HERMES_HOME this process runs with. A rejected skill
edit is rolled back with `tools.skill_ledger.rollback_entry` (a plain restore: no curator lock, no
write approval); an accepted proposal is inserted with
`tools.skill_manager_tool.skill_manage(action="patch")`, or becomes a new skill with
`skill_manage(action="create")` when it proposes one; both refuse a locked skill and stage the
write when `skills.write_approval` is on. Both run with the ledger actor `user`; a staged write
gets the actor of whoever approves it. The skill a proposal names is resolved with Hermes'
published `tools.skills_tool.locate_skill`; a name Hermes cannot resolve to one skill is refused.

Hermes is imported lazily inside the functions: the module stays importable by the dashboard
backend (which only uses `result`/`log_attempt`), and a Hermes without these internals gives a
`failed` outcome. Every real attempt (not --dry-run) is recorded in the plugin table apply_log.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Final, Literal, NotRequired, TextIO, TypeAlias, Unpack

from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypedDict

from . import store
from .dbtypes import PROPOSAL_ROW, ProposalRow, query_as, scalar
from .hermes_data import LedgerLine
from .jsontypes import JsonObject, as_str, loads_object

# target kind -> the decision an apply carries out
KINDS: Final[dict[str, str]] = {"skill_edit": "reject", "proposal": "accept"}
DONE: Final = ("applied", "staged")  # exit 0; refused | failed | unknown exit 1
MARGIN: Final = 5.0  # seconds before the caller's deadline after which nothing is changed any more
LOCK_WAIT: Final = (
    10.0  # seconds to wait for another apply of the same profile (without a deadline)
)

Outcome: TypeAlias = Literal["applied", "staged", "refused", "failed", "unknown", "dry-run"]
"""apply_log.outcome, plus `dry-run` (never logged)."""


class ResultExtra(TypedDict, total=False):
    """Optional fields of an apply result, in the order the caller passes them."""

    newer: list[str]  # ids of the newer ledger entries that block a rollback
    pending_id: str | None  # the staged write (skills.write_approval)
    old_string: str  # dry run of a proposal: the skill_manage patch
    new_string: str


class ApplyResult(TypedDict):
    """One apply attempt, as printed by `hermes argus apply --json` and sent to the dashboard."""

    ok: bool
    outcome: Outcome
    message: str
    ledger_entry: str | None
    target_kind: str
    target_id: str
    newer: NotRequired[list[str]]
    pending_id: NotRequired[str | None]
    old_string: NotRequired[str]
    new_string: NotRequired[str]


def result(
    outcome: Outcome,
    message: str,
    kind: str,
    tid: str,
    ledger_entry: str | None = None,
    **extra: Unpack[ResultExtra],
) -> ApplyResult:
    return {
        "ok": outcome in DONE or outcome == "dry-run",
        "outcome": outcome,
        "message": message,
        "ledger_entry": ledger_entry,
        "target_kind": kind,
        "target_id": tid,
        **extra,
    }


def log_attempt(conn: sqlite3.Connection, r: ApplyResult, applied_by: str) -> None:
    """Record one attempt in apply_log; an applied/staged one also marks the target's latest
    matching decision."""
    now = time.time()
    conn.execute(
        "insert into apply_log (target_kind, target_id, outcome, message, ledger_entry, "
        "pending_id, applied_at, applied_by) values (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            r["target_kind"],
            r["target_id"],
            r["outcome"],
            r["message"],
            r.get("ledger_entry"),
            r.get("pending_id"),
            now,
            applied_by,
        ),
    )
    if r["outcome"] in DONE:
        conn.execute(
            "update decision set executed_at = ?, execution_result = ?, ledger_entry_id = ? "
            "where id = "
            "(select id from decision where target_kind = ? and target_id = ? and action = ? "
            "order by decided_at desc, id desc limit 1)",
            (
                now,
                f"{r['outcome']}: {r['message']}",
                r.get("ledger_entry"),
                r["target_kind"],
                r["target_id"],
                KINDS[r["target_kind"]],
            ),
        )
    conn.commit()


def current_decision(conn: sqlite3.Connection, kind: str, tid: str) -> str | None:
    action = scalar(
        conn,
        "select action from decision where target_kind = ? and target_id = ? "
        "order by decided_at desc, id desc limit 1",
        (kind, tid),
    )
    if action is None or isinstance(action, str):
        return action
    raise TypeError(f"decision.action is not text: {action!r}")


def changed_since_decision(conn: sqlite3.Connection, pid: str) -> str | None:
    """What of proposal `pid` (rule, diff) differs from what its latest decision recorded as shown
    to the person; None when nothing does, or the decision recorded neither. A later derive can
    fill a diff that was empty when the person accepted: that diff was never reviewed."""
    snap = scalar(
        conn,
        "select target_snapshot from decision where target_kind = 'proposal' and target_id = ? "
        "order by decided_at desc, id desc limit 1",
        (pid,),
    )
    try:
        shown = loads_object(snap) if isinstance(snap, str) else None
    except ValueError:
        shown = None
    rows = query_as(conn, PROPOSAL_ROW, "select * from proposal where id = ?", (pid,))
    if shown is None or not rows:
        return None
    now: dict[str, str] = {"rule": rows[0]["rule"] or "", "diff": rows[0]["diff"] or ""}
    changed = [k for k, v in now.items() if k in shown and (as_str(shown[k]) or "") != v]
    return " and ".join(changed) or None


def pending_path(home: Path, pending_id: str) -> Path:
    """Where Hermes keeps a staged skill write (tools.write_approval._pending_path)."""
    return Path(home) / "pending" / "skills" / f"{pending_id}.json"


def _sha(path: str) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _within(root: Path, path: str) -> bool:
    try:
        Path(os.path.normpath(path)).relative_to(os.path.normpath(str(root)))
        return True
    except ValueError:
        return False


def _rel(path: str) -> str:
    return path.split("/skills/", 1)[-1]


def _late(deadline: float | None, kind: str, tid: str) -> ApplyResult | None:
    """Refusal when the caller's deadline is too close to start a write (the caller would kill us
    mid-way)."""
    if deadline is not None and time.time() > deadline - MARGIN:
        return result(
            "refused",
            "too little time left before the caller's deadline; nothing was changed, retry",
            kind,
            tid,
        )
    return None


# ---------------------------------------------------------------- JSON from Hermes
_LEDGER_LINE: Final = TypeAdapter(LedgerLine)


def _ledger(raw: list[dict[str, object]]) -> list[LedgerLine]:
    """Hermes' ledger entries (`tools.skill_ledger.list_entries`) Argus can read, in order."""
    out: list[LedgerLine] = []
    for e in raw:
        try:
            out.append(_LEDGER_LINE.validate_python(e))
        except ValidationError:
            continue
    return out


def _paths(entry: LedgerLine) -> set[str]:
    return {f.get("path", "") for f in (entry.get("before") or []) + (entry.get("after") or [])}


def _rolls_back(entry: LedgerLine, target: str) -> bool:
    return (
        entry.get("action") == "rollback"
        and (entry.get("evidence") or {}).get("rollback_target") == target
    )


class _Pending(TypedDict, total=False):
    """A staged skill write (tools.write_approval), as far as Argus reads it."""

    payload: JsonObject


class _SkillManaged(TypedDict, total=False):
    """skill_manage's JSON answer."""

    success: bool
    staged: bool
    pending_id: str
    message: str
    error: str


_PENDING: Final = TypeAdapter(_Pending)
_SKILL_MANAGED: Final = TypeAdapter(_SkillManaged)


# ---------------------------------------------------------------- rollback (reject an applied edit)
def rollback(
    home: Path, entry_id: str, dry_run: bool, deadline: float | None = None
) -> ApplyResult:
    kind = "skill_edit"
    try:
        from tools import skill_ledger as sl
    except Exception as e:
        return result(
            "failed",
            f"Hermes skill ledger unavailable ({e}); use `hermes curator rollback {entry_id}`",
            kind,
            entry_id,
        )
    entries = _ledger(sl.list_entries())  # newest first
    idx = next((i for i, x in enumerate(entries) if x["id"] == entry_id), None)
    if idx is None:
        return result("refused", f"no ledger entry {entry_id} in this profile", kind, entry_id)
    entry = entries[idx]
    before, after = entry.get("before") or [], entry.get("after") or []
    outside = [i.get("path", "") for i in before + after if not _within(home, i.get("path", ""))]
    if outside:
        return result(
            "refused",
            f"entry {entry_id} references {outside[0]}, outside this profile's home {home} "
            f"(ledger written on another host or home); Hermes refuses to roll it back",
            kind,
            entry_id,
        )
    # precondition: no later change, i.e. every file is still exactly what the entry left behind
    after_paths = {i["path"] for i in after if i.get("path")}
    changed = [i["path"] for i in after if i.get("path") and _sha(i["path"]) != i.get("sha256")]
    changed += [
        i["path"]
        for i in before
        if i.get("path") and i["path"] not in after_paths and Path(i["path"]).exists()
    ]
    if changed:
        files = set(changed)
        newer = [
            x for x in entries[:idx] if x.get("action") != "pre-rollback" and files & _paths(x)
        ]
        done = next((x for x in newer if _rolls_back(x, entry_id)), None)
        names = ", ".join(sorted(_rel(f) for f in files))
        if done:
            msg = (
                f"entry {entry_id} was already rolled back by ledger entry {done['id']} "
                f"({done['ts'][:19]})"
            )
        elif newer:
            msg = (
                f"{names} changed after entry {entry_id} by newer ledger "
                f"entr{'ies' if len(newer) > 1 else 'y'} "
                + ", ".join(
                    f"{x['id']} ({x.get('action')}, {x.get('actor')}, {x['ts'][:19]})"
                    for x in newer
                )
                + f"; rolling back now would erase {'them' if len(newer) > 1 else 'it'}. "
                f"Reject and apply {'those' if len(newer) > 1 else 'it'} first (newest first), "
                f"or roll back "
                f"by hand with the copied `hermes curator rollback {entry_id}`"
            )
        else:
            msg = (
                f"{names} changed outside the ledger after entry {entry_id}; rolling back now "
                f"would erase that change. Roll back by hand with the copied "
                f"`hermes curator rollback {entry_id}` if intended"
            )
        return result("refused", msg, kind, entry_id, newer=[x["id"] for x in newer])
    files_ = sorted(_rel(i["path"]) for i in before + after if i.get("path"))
    if dry_run:
        return result(
            "dry-run",
            f"would roll back entry {entry_id} ({entry.get('action')} on '{entry.get('skill')}'): "
            f"restore {', '.join({f: None for f in files_})} to the state before it",
            kind,
            entry_id,
        )
    if late := _late(deadline, kind, entry_id):
        return late
    tok = sl.set_ledger_actor("user")
    try:
        ok, msg = sl.rollback_entry(entry_id)
    except Exception as e:
        return result("failed", f"rollback_entry raised {type(e).__name__}: {e}", kind, entry_id)
    finally:
        sl.reset_ledger_actor(tok)
    if not ok:
        return result("refused", msg, kind, entry_id)
    recent = _ledger(sl.list_entries(skill=entry.get("skill"), limit=50))
    rb = next((x for x in recent if _rolls_back(x, entry_id)), None)
    return result("applied", msg, kind, entry_id, ledger_entry=rb["id"] if rb else None)


# ---------------------------------------------------------------- proposal (accept a proposed edit)
_MARKER: Final = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _norm(s: str | None) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", (s or "").lower()).split())


def hunks(diff: str | None) -> list[list[tuple[str, str]]]:
    """Hunks of a unified diff as (tag, text) lines, tag ' ' | '+' | '-'; file headers skipped."""
    out: list[list[tuple[str, str]]] = []
    for line in (diff or "").splitlines():
        if line.startswith("@@"):
            out.append([])
        elif out and not line.startswith("\\"):
            tag = line[:1] if line[:1] in " +-" else " "
            out[-1].append((tag, line[1:] if line[:1] in " +-" else line))
    return out


def strings(hunk: list[tuple[str, str]]) -> tuple[str, str]:
    """skill_manage old_string/new_string of one hunk: context + removed vs context + added,
    without blank-only context at either end (it anchors nothing and breaks the match on whitespace
    drift)."""
    h = list(hunk)
    while h and h[0][0] == " " and not h[0][1].strip():
        h.pop(0)
    while h and h[-1][0] == " " and not h[-1][1].strip():
        h.pop()
    return "\n".join(t for g, t in h if g in " -"), "\n".join(t for g, t in h if g in " +")


def _pending_duplicate(home: Path, action: str, name: str, **fields: str) -> str | None:
    """Id of a staged skill write that is this very action, if one waits for approval."""
    for f in sorted((Path(home) / "pending" / "skills").glob("*.json")):
        try:
            payload = _PENDING.validate_json(f.read_bytes()).get("payload") or {}
        except (OSError, ValueError):
            continue
        if (
            payload.get("action") == action
            and payload.get("name") == name
            and all(payload.get(k) == v for k, v in fields.items())
        ):
            return f.stem
    return None


DESC_LIMIT: Final = 60
"""Hermes' SKILL_PROMPT_DESC_LIMIT: skill_manage refuses a new skill whose description is longer."""


def new_skill(name: str, rule: str) -> str:
    """SKILL.md of a skill created from a proposal: the rule is the body; the description is its
    first sentence, cut at a word to fit the skill index (no model call: the person who accepted
    can edit it later)."""
    flat = " ".join(rule.split())
    ends = [i + 1 for i, ch in enumerate(flat) if ch in ".!?" and flat[i + 1 : i + 2] == " "]
    desc = flat[: ends[0]] if ends else flat
    if len(desc) > DESC_LIMIT:
        desc = desc[: DESC_LIMIT - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "\u2026"
    return (
        f"---\nname: {name}\ndescription: {json.dumps(desc, ensure_ascii=False)}\n---\n\n"
        f"# {name}\n\n{rule.strip()}\n"
    )


def written_by(row: ProposalRow, action: str | None, before: str | None, after: str | None) -> bool:
    """Whether a ledger entry (its action, the skill's SKILL.md before and after) is the write an
    apply of this proposal stages. The ledger records an approved staged write under the approver's
    actor and without the pending id: export recognises it by its content."""
    if after is None:
        return False
    if row["is_new_skill"] or row["skill_name"].startswith("new:"):
        name = row["skill_name"].removeprefix("new:").strip()
        return action == "create" and _norm(after) == _norm(new_skill(name, row["rule"] or ""))
    hs = [h for h in hunks(row["diff"]) if any(g in "+-" for g, _ in h)]
    if action != "patch" or len(hs) != 1:
        return False
    new = _norm(strings(hs[0])[1])
    return bool(new) and new in _norm(after) and new not in _norm(before)


def _skill_call(
    home: Path,
    action: str,
    name: str,
    kind: str,
    pid: str,
    expect: Path | None = None,
    **args: str,
) -> ApplyResult:
    """Run skill_manage as the ledger actor `user` and turn its JSON into an apply result. With
    `expect` (the SKILL.md apply checked), the ledger entry of the write must name that file."""
    try:
        from tools import skill_ledger as sl
        from tools import skill_manager_tool as smt
    except Exception as e:
        return result(
            "failed", f"Hermes skill_manage unavailable ({e}); use the copied command", kind, pid
        )
    seen = {x.get("id") for x in sl.list_entries(skill=name, limit=50)}
    tok = sl.set_ledger_actor("user")
    try:
        if action == "create":
            raw = smt.skill_manage("create", name, content=args["content"])
        else:
            raw = smt.skill_manage(
                "patch", name, old_string=args["old"], new_string=args["new"], file_path=None
            )
    except Exception as e:
        return result("failed", f"skill_manage raised {type(e).__name__}: {e}", kind, pid)
    finally:
        sl.reset_ledger_actor(tok)
    try:
        r = _SKILL_MANAGED.validate_json(raw)
    except (TypeError, ValueError):
        return result("failed", f"skill_manage answered {str(raw)[:300]}", kind, pid)
    if r.get("staged"):
        p = r.get("pending_id") or "<id>"
        return result(
            "staged",
            f"staged as pending write {p} (skills.write_approval is on), not written yet: approve "
            f"with `/skills approve {p}` in a Hermes chat, or review with `/skills pending` in the "
            f"hermes CLI",
            kind,
            pid,
            pending_id=r.get("pending_id"),
        )
    if r.get("success"):
        new = next(
            (
                x
                for x in _ledger(sl.list_entries(skill=name, limit=50))
                if x.get("id") not in seen
                and x.get("actor") == "user"
                and x.get("action") == action
            ),
            None,
        )
        new_id = new["id"] if new else None
        written = {os.path.realpath(p) for p in _paths(new) if p} if new else set()
        if expect is not None and os.path.realpath(expect) not in written:
            # the write happened, but not provably to the file apply checked
            what = (
                f"skill_manage wrote {', '.join(sorted(written))}, not the checked {expect}"
                if written
                else f"skill_manage reported success but left no ledger entry naming {expect} "
                "(skills.ledger off?), so the file it wrote is not known"
            )
            undo = (
                f"; `hermes -p {store.profile_name(home)} curator rollback {new_id}` undoes it "
                "inside the profile home, files outside it must be restored by hand"
                if new_id
                else ""
            )
            return result(
                "unknown",
                f"{what}. Check the skill before applying again{undo}",
                kind,
                pid,
                ledger_entry=as_str(new_id),
            )
        done = "created" if action == "create" else "patched"
        return result("applied", r.get("message") or done, kind, pid, ledger_entry=as_str(new_id))
    return result(
        "refused",
        r.get("error") or r.get("message") or f"skill_manage refused the {action}",
        kind,
        pid,
    )


# directories Hermes' skill scanners skip in every supported version (agent.skill_utils
# EXCLUDED_SKILL_DIRS of 0.22.5): pruned when counting the directories skill_manage may pick
_SKIPPED_DIRS: Final = frozenset(
    {
        ".git",
        ".github",
        ".hub",
        ".archive",
        ".curator_backups",
        ".locks",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        "__pycache__",
        ".tox",
        ".nox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)
# skipped by some supported versions only (DIST_SWAP_DIR, later than 0.22.5): never pruned when
# counting, but a target under one is refused
_SOMETIMES_SKIPPED: Final = frozenset({".hermes-dist"})
# a skill's own support dirs, never scanned as skills (agent.skill_utils SKILL_SUPPORT_DIRS)
_SUPPORT_DIRS: Final = frozenset({"references", "templates", "assets", "scripts"})


def _crosses_symlink(root: Path, path: Path) -> bool:
    """Whether `path` (under `root`) is reached through a symlink below `root`."""
    cur = root
    for part in path.relative_to(root).parts:
        cur = cur / part
        if cur.is_symlink():
            return True
    return False


def _scanner_skips(md: Path) -> bool:
    """Whether Hermes' skill scanners skip this SKILL.md (its `is_excluded_skill_path`): a skipped
    directory anywhere in the path, or a support dir directly inside another skill."""
    parts = md.parts
    return any(part in _SKIPPED_DIRS | _SOMETIMES_SKIPPED for part in parts) or any(
        part in _SUPPORT_DIRS and (Path(*parts[:i]) / "SKILL.md").exists()
        for i, part in enumerate(parts[:-1])
        if i > 0
    )


def _dirs_named(name: str, roots: list[tuple[int, Path]]) -> list[Path]:
    """Every SKILL.md in a directory called `name` under the roots, each place it is reached
    (symlinks followed, a directory already on the current path skipped as a cycle): a superset
    of what `skill_manage` can pick for `name` by directory name. Only Hermes' skipped dirs and
    the support dirs of a skill are pruned, as its scanners do."""
    out: list[Path] = []

    def walk(d: Path, ancestors: frozenset[str]) -> None:
        try:
            with os.scandir(d) as it:
                entries: list[os.DirEntry[str]] = list(it)
        except OSError:
            return
        names = {e.name for e in entries}
        is_skill = "SKILL.md" in names
        if is_skill and d.name == name:
            out.append(d / "SKILL.md")
        for e in entries:
            if e.name in _SKIPPED_DIRS or (is_skill and e.name in _SUPPORT_DIRS):
                continue
            try:
                if not e.is_dir():  # follows symlinks
                    continue
            except OSError:
                continue
            real = os.path.realpath(e.path)
            if real not in ancestors:
                walk(Path(e.path), ancestors | {real})

    for _tier, root in roots:
        walk(root, frozenset({os.path.realpath(root)}))
    return out


def _locate(home: Path, name: str) -> tuple[Path | None, str | None]:
    """The skill `name` of the profile as Hermes resolves it, with the published
    `tools.skills_tool.locate_skill` over the profile's roots (`store.hermes_skill_roots`):
    `(file, None)` for one skill, `(None, None)` when none has that name (and no directory has it),
    `(None, reason)` when Hermes refuses to pick one or cannot answer. Raises ImportError without
    Hermes."""
    from tools.skills_tool import locate_skill

    if "/" in name or "\\" in name:
        return None, f"apply takes a bare skill name, not the path {name}."
    roots = store.hermes_skill_roots(Path(home))
    if roots is None:
        return None, "Hermes could not list the profile's skill directories."
    if not roots:
        return None, None
    err, _dir, md = locate_skill(name, None, roots)
    if md is not None:
        return md, None
    try:
        answer = loads_object(err or "")
    except ValueError:
        return None, err or f"Hermes could not resolve skill {name}."
    if "available_skills" in answer:  # Hermes' "not found" answer
        if named := _dirs_named(name, store.hermes_all_skill_roots(Path(home)) or roots):
            return None, f"Hermes reports it missing, but {named[0]} exists."
        return None, None
    return None, as_str(answer.get("error")) or f"Hermes could not resolve skill {name}."


def _writer_name(home: Path, name: str, md: Path) -> tuple[str | None, str | None]:
    """The name to give `skill_manage` so that it writes `md` and nothing else, or why there is
    none. `skill_manage` takes only a name and resolves it itself (Hermes' private `_find_skill`):
    the first directory it enumerates whose name, or whose path relative to the profile's
    `skills/`, equals the name; a unique frontmatter name only when no directory matches. So:

    - `md` must be a SKILL.md in the profile's `skills/` that its scanner enumerates: reached
      without a symlink (the file included), outside the directories it skips;
    - no other directory called `name` may exist in any of Hermes' skill roots (project dirs
      included, every directory any supported Hermes may enumerate): Argus keys a skill by its
      directory name, so two would also be one skill to it (its ledger, its pending writes);
    - in a category (`skills/<cat>/<name>/`) the name given is that relative path, which only
      `md`'s directory has; directly in `skills/` it is the bare name."""
    local = Path(home) / "skills"
    where = f"{md} is"
    try:
        rel = md.parent.relative_to(local)
    except ValueError:
        return None, f"{where} not in the profile's skills/ directory, where apply writes."
    if (
        not rel.parts
        or md.is_symlink()
        or _crosses_symlink(local, md.parent)
        or os.path.realpath(md.parent) != os.path.join(os.path.realpath(local), *rel.parts)
    ):
        return None, f"{where} reached through a symlink, which apply does not follow."
    if _scanner_skips(md):
        return None, f"{where} in a directory Hermes may not scan for skills."
    every = store.hermes_all_skill_roots(Path(home))
    if every is None:
        return None, "Hermes could not list the profile's skill directories."
    others = sorted({str(p) for p in _dirs_named(name, every)} - {str(md)})
    if others:
        return None, f"{where} not the only directory called {name}: {', '.join(others)}."
    return rel.as_posix(), None


def _create(
    home: Path, pid: str, name: str, rule: str, dry_run: bool, deadline: float | None
) -> ApplyResult:
    kind = "proposal"
    try:
        found, problem = _locate(home, name)
    except Exception as e:
        return result(
            "failed", f"Hermes skill lookup unavailable ({e}); use the copied command", kind, pid
        )
    if problem:
        return result(
            "refused",
            f"proposal {pid} creates skill {name}, and Hermes cannot confirm that profile "
            f"{store.profile_name(home)} has none: {problem} Use the copied command",
            kind,
            pid,
        )
    if found:
        return result(
            "refused",
            f"proposal {pid} creates skill {name}, which exists now in profile "
            f"{store.profile_name(home)}. Re-run `hermes argus derive` for a patch, or use the "
            "copied command",
            kind,
            pid,
        )
    content = new_skill(name, rule)
    if dup := _pending_duplicate(home, "create", name, content=content):
        return result(
            "refused",
            f"this skill is already staged as pending write {dup}: approve it with "
            f"`/skills approve {dup}`",
            kind,
            pid,
            pending_id=dup,
        )
    if dry_run:
        return result(
            "dry-run",
            f"would create skill {name} with skill_manage",
            kind,
            pid,
            old_string="",
            new_string=content,
        )
    if late := _late(deadline, kind, pid):
        return late
    return _skill_call(home, "create", name, kind, pid, content=content)


def proposal(home: Path, pid: str, dry_run: bool, deadline: float | None = None) -> ApplyResult:
    kind = "proposal"
    conn = store.open_db(home)
    try:
        rows = query_as(conn, PROPOSAL_ROW, "select * from proposal where id = ?", (pid,))
    finally:
        conn.close()
    if not rows:
        return result(
            "refused", f"no proposal {pid} in profile {store.profile_name(home)}", kind, pid
        )
    row = rows[0]
    name, file = row["skill_name"], row["file"] or ""
    if row["is_new_skill"] or name.startswith("new:"):
        name = name.removeprefix("new:").strip()
        return _create(home, pid, name, row["rule"] or "", dry_run, deadline)
    if file and Path(file).name != "SKILL.md":
        return result(
            "refused",
            f"proposal {pid} targets {file}: apply only patches SKILL.md; use the copied command",
            kind,
            pid,
        )
    hs = [h for h in hunks(row["diff"]) if any(g in "+-" for g, _ in h)]
    if len(hs) != 1:
        return result(
            "refused",
            f"proposal {pid} has {len(hs) or 'no'} change hunk{'s' if len(hs) != 1 else ''}; "
            "apply needs exactly one. Use the copied command",
            kind,
            pid,
        )
    old, new = strings(hs[0])
    if not old.strip():
        return result(
            "refused",
            f"proposal {pid}: the diff has no context line to anchor the change; "
            "use the copied command",
            kind,
            pid,
        )
    try:
        target, problem = _locate(home, name)
    except Exception as e:
        return result(
            "failed", f"Hermes skill lookup unavailable ({e}); use the copied command", kind, pid
        )
    if problem:
        # skill_manage would pick one of the candidates by its own rules: never patch a guess
        return result(
            "refused",
            f"Hermes cannot resolve skill {name} in profile {store.profile_name(home)} to one "
            f"file: {problem} Use the copied command",
            kind,
            pid,
        )
    if target is None:
        return result(
            "refused",
            f"skill {name} not found in profile {store.profile_name(home)}: it was removed or "
            "renamed after the proposal. Dismiss the proposal, or recreate the skill first",
            kind,
            pid,
        )
    if target.name != "SKILL.md":
        return result(
            "refused",
            f"skill {name} is a single-file skill ({target.name}): apply only patches SKILL.md; "
            "use the copied command",
            kind,
            pid,
        )
    if file and store.skill_rel(home, target) != file:
        return result(
            "refused",
            f"proposal {pid} was derived from {file}, but Hermes now resolves skill {name} to "
            f"{target}. Re-run `hermes argus derive` for a fresh diff, or use the copied command",
            kind,
            pid,
        )
    writer, why = _writer_name(home, name, target)
    if writer is None:
        return result(
            "refused",
            f"apply cannot make skill_manage write the file it checks for skill {name}: {why} "
            "Use the copied command",
            kind,
            pid,
        )
    rel = _rel(str(target))
    try:
        content = target.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    except OSError as e:
        return result("failed", f"cannot read {target}: {e}", kind, pid)
    text = _norm(content)
    added = [n for n in (_norm(_MARKER.sub("", t)) for g, t in hs[0] if g == "+") if n]
    if (_norm(row["rule"]) and _norm(row["rule"]) in text) or (
        added and all(a in text for a in added)
    ):
        return result("refused", f"the rule is already in {rel}; nothing to apply", kind, pid)
    if not store.skill_writable(target):
        return result(
            "refused",
            f"skill {name} is read-only here ({target.parent}): Hermes cannot write it, typically a"
            " shared skills.external_dirs. Add the rule where that skill is maintained",
            kind,
            pid,
        )
    # the diff is a snapshot of SKILL.md at derive time; skill_manage matches fuzzily and would put
    # the stale context back over a newer edit, so the context must still be in the file, verbatim
    # and once
    n = content.count(old)
    if n != 1:
        return result(
            "refused",
            f"{rel} changed since the proposal: its context lines are "
            f"{'no longer in the file' if n == 0 else f'in the file {n} times'}. Re-run "
            "`hermes argus derive` for a fresh diff, or use the copied command",
            kind,
            pid,
        )
    if dup := _pending_duplicate(
        home, "patch", name, old_string=old, new_string=new
    ) or _pending_duplicate(home, "patch", writer, old_string=old, new_string=new):
        return result(
            "refused",
            f"this patch is already staged as pending write {dup}: approve it with "
            f"`/skills approve {dup}`",
            kind,
            pid,
            pending_id=dup,
        )
    if dry_run:
        return result(
            "dry-run",
            f"would patch {rel} of skill {name} with skill_manage",
            kind,
            pid,
            old_string=old,
            new_string=new,
        )
    if late := _late(deadline, kind, pid):
        return late
    return _skill_call(home, "patch", writer, kind, pid, expect=target, old=old, new=new)


# ---------------------------------------------------------------- entry point
def _lock(f: TextIO, until: float) -> bool:
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.time() >= until:
                return False
            time.sleep(0.2)


def run(
    home: Path,
    kind: str,
    tid: str,
    dry_run: bool = False,
    applied_by: str = "cli",
    deadline: float | None = None,
    force: bool = False,
) -> ApplyResult:
    """Apply one target in the profile at `home`, one apply at a time per profile. Unless `force`
    (or a dry run), the target's current decision must be the one the apply carries out (reject /
    accept); it is re-read under the lock. `deadline` (epoch): refuse before changing anything when
    it is near. Real attempts are logged in apply_log."""
    home = Path(home)
    lock = store.data_dir(home) / "apply.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    by = f"{applied_by} --force" if force else applied_by
    r: ApplyResult
    with open(lock, "a") as f:
        if not _lock(f, deadline - MARGIN if deadline is not None else time.time() + LOCK_WAIT):
            r = result(
                "refused", "another apply is running in this profile; retry in a moment", kind, tid
            )
        else:
            try:
                need = KINDS[kind]
                conn = store.open_db(home)
                try:
                    current = current_decision(conn, kind, tid)
                    what = changed_since_decision(conn, tid) if kind == "proposal" else None
                finally:
                    conn.close()
                if not (dry_run or force) and current != need:
                    r = result(
                        "refused",
                        f'{kind} {tid} has no current "{need}" decision in profile '
                        f"{store.profile_name(home)} (latest: {current or 'none'}); decide in the "
                        f"dashboard first, or pass --force",
                        kind,
                        tid,
                    )
                elif kind == "skill_edit":
                    r = rollback(home, tid, dry_run, deadline)
                elif not (dry_run or force) and what:
                    r = result(
                        "refused",
                        f"proposal {tid}: its {what} changed since it was accepted; review it "
                        "again and accept it again, or pass --force",
                        kind,
                        tid,
                    )
                else:
                    r = proposal(home, tid, dry_run, deadline)
            except Exception as e:
                r = result("failed", f"{type(e).__name__}: {e}", kind, tid)
        if not dry_run:
            conn = store.open_db(home)
            try:
                log_attempt(conn, r, by)
            finally:
                conn.close()
    return r
