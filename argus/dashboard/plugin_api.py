"""Dashboard backend of the Argus plugin, mounted by Hermes at /api/plugins/argus/."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Final,
    Literal,
    TypeAlias,
    TypedDict,
    TypeGuard,
)

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

_PKG_DIR = Path(__file__).resolve().parent.parent
_PKG = "hermes_dashboard_plugin_argus_pkg"


def _code_stamp() -> tuple[tuple[str, int, int], ...]:
    """Name, size and mtime of every file of the plugin package its code and schema load from."""
    files = sorted(
        p
        for p in _PKG_DIR.rglob("*")
        if p.suffix in (".py", ".sql", ".yaml") and "__pycache__" not in p.parts
    )
    return tuple(
        (str(p.relative_to(_PKG_DIR)), st.st_size, st.st_mtime_ns)
        for p in files
        for st in (p.stat(),)
    )


_LOADED: Final = _code_stamp()
"""The plugin as this dashboard process loaded it: Hermes keeps this router until the dashboard
restarts, so after an update on disk the routes would mix two versions."""


def _stale() -> JSONResponse | None:
    """503 with what to do when Argus changed on disk since this process loaded it."""
    try:
        if _code_stamp() == _LOADED:
            return None
    except OSError:
        pass
    body: ErrorBody = {
        "error": "Argus was updated after this dashboard loaded it: restart `hermes dashboard`"
        " to use the new version"
    }
    return JSONResponse(body, status_code=503)


def _mod(name: str) -> types.ModuleType:
    """Import a module of the plugin package under a private package name: Hermes loads this file
    on its own, so the plugin directory is not an importable package here."""
    if _PKG not in sys.modules:
        pkg = types.ModuleType(_PKG)
        pkg.__path__ = [str(_PKG_DIR)]
        sys.modules[_PKG] = pkg
    return importlib.import_module(f"{_PKG}.{name}")


if TYPE_CHECKING:
    # mypy reads the plugin modules themselves; at run time they come from _mod
    from argus import apply as apply_mod
    from argus import cli, dbtypes, export, jsontypes, store
    from argus.apply import Outcome
    from argus.exporttypes import (
        ApplyBody,
        DecisionAction,
        DecisionDone,
        DecisionKind,
        ErrorBody,
        Export,
        LastDerive,
        ProfileCounts,
        RefreshBody,
        RunError,
        Status,
    )
    from argus.jsontypes import JsonObject
else:
    apply_mod, cli, dbtypes, export, jsontypes, store = (
        _mod(n) for n in ("apply", "cli", "dbtypes", "export", "jsontypes", "store")
    )

router = APIRouter()


def _root() -> Path:
    """Install root (the default profile's home) of the HERMES_HOME the dashboard was launched
    with."""
    from hermes_constants import get_hermes_home

    return store.root_of(Path(get_hermes_home()))


_refresh: subprocess.Popen[bytes] | None = None
"""The derive this dashboard started last: running from the spawn on, before it takes its locks."""


def _derive_running() -> bool:
    """A derive of this install is running: the one refresh started, or any holding a profile's
    derive lock (idle trigger, CLI). Works wherever flock does, and sees no other install."""
    if _refresh is not None and _refresh.poll() is None:
        return True
    return any(cli.lock_held(home) for home in store.profile_homes(_root()).values())


def _status() -> Status:
    out: Status = {
        "profiles": {},
        "intents": 0,
        "edits_without_evidence": 0,
        "pending_proposals": 0,
        "last_derive": None,
        "last_run_error": None,
        "running": _derive_running(),
        "errors": {},
    }
    # newest finished derive (dashboard refresh or idle trigger) across the profiles' derive.log
    last_exit: RunError | None = None
    for name, home in store.profile_homes(_root()).items():
        ex = cli.last_exit(cli.derive_log(home))
        if ex and (last_exit is None or ex["at"] > last_exit["at"]):
            last_exit = {
                "exit_code": ex["exit_code"],
                "at": ex["at"],
                "log": ex["log"],
                "profile": name,
            }
        c: ProfileCounts = {"intents": 0, "edits_without_evidence": 0, "pending_proposals": 0}
        try:  # one profile's broken plugin data must not hide the others
            path = store.db_path(home)
            if path.exists():
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
                try:
                    # inside the profile's history window, as the dashboard lists them
                    c["intents"] = store.live_intents(conn)
                    c["edits_without_evidence"] = store.speculative_edits(conn)
                    # proposals for a skill no longer in the profile are obsolete, not pending
                    c["pending_proposals"] = store.pending_proposals(
                        conn, home, ("accept", "dismiss")
                    )
                finally:
                    conn.close()
        except (sqlite3.Error, OSError) as e:
            out["errors"][name] = str(e)
        runs = sorted((store.data_dir(home) / "runs").glob("*.json"))
        if runs:
            last: LastDerive = {
                "profile": name,
                "file": runs[-1].name,
                "ts": runs[-1].stat().st_mtime,
            }
            try:
                rep: object = json.loads(runs[-1].read_text())
                last["report"] = rep if len(json.dumps(rep)) < 4000 else None
            except (OSError, ValueError):
                pass
            if not out["last_derive"] or last["ts"] > out["last_derive"]["ts"]:
                out["last_derive"] = last
        out["profiles"][name] = c
        out["intents"] += c["intents"]
        out["edits_without_evidence"] += c["edits_without_evidence"]
        out["pending_proposals"] += c["pending_proposals"]
    if last_exit and last_exit["exit_code"] != 0:
        out["last_run_error"] = last_exit
    return out


_ACTIONS: Final[dict[DecisionKind, tuple[DecisionAction, DecisionAction]]] = {
    "skill_edit": ("keep", "reject"),
    "proposal": ("accept", "dismiss"),
}


def _is_kind(v: object) -> TypeGuard[DecisionKind]:
    return v in _ACTIONS


def _is_action(v: object, kind: DecisionKind) -> TypeGuard[DecisionAction | Literal["clear"]]:
    return v in (*_ACTIONS[kind], "clear")


def _valid_id(v: object) -> TypeGuard[str]:
    return isinstance(v, str) and bool(v.strip()) and len(v) <= 300


def _decide(raw: object) -> tuple[int, ErrorBody | DecisionDone]:
    """Record (or with action `clear`, undo) a person's decision in the profile's plugin DB
    (layer 3)."""
    body = jsontypes.as_object(raw)
    if body is None:
        return 400, {"error": "expected a JSON object"}
    kind, tid, action, prof, snap = (
        body.get(k) for k in ("target_kind", "target_id", "action", "profile", "snapshot")
    )
    if not _is_kind(kind):
        return 400, {"error": f"target_kind must be one of {', '.join(_ACTIONS)}"}
    if not _is_action(action, kind):
        allowed = ", ".join((*_ACTIONS[kind], "clear"))
        return 400, {"error": f"action for {kind} must be one of {allowed}"}
    if not _valid_id(tid):
        return 400, {"error": "target_id must be a non-empty string"}
    homes = store.profile_homes(_root())
    if not isinstance(prof, str) or prof not in homes:
        return 400, {"error": f"unknown profile {prof!r}"}
    snapshot = None
    if action != "clear":
        if not isinstance(snap, dict):
            return 400, {"error": "snapshot must be a JSON object"}
        snapshot = json.dumps(snap, ensure_ascii=False)
        if len(snapshot) > 65536:
            return 413, {"error": "snapshot too large"}
    conn = store.open_db(homes[prof])  # short-lived connection, never the hooks' Writer
    try:
        if action == "clear":  # the user undoes their own click: drop that target's decisions
            n = conn.execute(
                "delete from decision where target_kind = ? and target_id = ?", (kind, tid)
            ).rowcount
        else:
            conn.execute(
                "insert into decision (target_kind, target_id, action, target_snapshot, "
                "decided_at, decided_by) values (?, ?, ?, ?, ?, 'dashboard')",
                (kind, tid, action, snapshot, time.time()),
            )
            n = 1
        conn.commit()
    finally:
        conn.close()
    return 200, {
        "ok": True,
        "profile": prof,
        "target_kind": kind,
        "target_id": tid,
        "action": action,
        "rows": n,
    }


APPLY_TIMEOUT: Final = 45  # seconds; the host's plugin HTTP limit is 60 (504 after it)
_NEED: Final[dict[DecisionKind, DecisionAction]] = {"skill_edit": "reject", "proposal": "accept"}


def _child_result(stdout: str) -> JsonObject | None:
    """The apply child's JSON result: its last stdout line that is a JSON object with `outcome`."""
    for line in reversed(stdout.splitlines()):
        try:
            out = jsontypes.loads(line) if line.startswith("{") else None
        except ValueError:
            out = None
        if isinstance(out, dict) and "outcome" in out:
            return out
    return None


def _apply(raw: object) -> tuple[int, ErrorBody | ApplyBody]:
    """Carry out a recorded decision (reject -> rollback, accept -> patch) in the decision's
    profile.
    Hermes' skill functions act on the HERMES_HOME of the process that calls them, and
    `hermes -p <profile> argus` exists only where the plugin is enabled, so `apply_child.py` runs
    with this interpreter and HERMES_HOME=<profile home>, for every profile the dashboard shows.
    Returns its JSON result."""
    body = jsontypes.as_object(raw)
    if body is None:
        return 400, {"error": "expected a JSON object"}
    kind, tid, prof = (body.get(k) for k in ("target_kind", "target_id", "profile"))
    if not _is_kind(kind):
        return 400, {"error": f"target_kind must be one of {', '.join(_ACTIONS)}"}
    need = _NEED[kind]
    if not _valid_id(tid):
        return 400, {"error": "target_id must be a non-empty string"}
    homes = store.profile_homes(_root())
    if not isinstance(prof, str) or prof not in homes:
        return 400, {"error": f"unknown profile {prof!r}"}
    conn = store.open_db(homes[prof])
    try:  # fast pre-check; apply.run re-reads the decision under its lock
        current = apply_mod.current_decision(conn, kind, tid)
    finally:
        conn.close()
    if current != need:
        return 400, {"error": f'{kind} {tid} has no current "{need}" decision in profile {prof}'}
    spec = importlib.util.find_spec("hermes_cli")
    repo = str(Path(spec.origin).parent.parent) if spec and spec.origin else ""
    # the child changes nothing after deadline - MARGIN
    deadline = time.time() + APPLY_TIMEOUT - apply_mod.MARGIN
    cmd = [sys.executable, str(_PKG_DIR / "apply_child.py"), repo, kind, tid, f"{deadline:.3f}"]
    # out: the child's JSON result; it logged the attempt in apply_log itself
    out: JsonObject | None = None
    outcome: Outcome = "failed"
    err = ""
    try:
        cp = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=APPLY_TIMEOUT,
            env={**os.environ, "HERMES_HOME": str(homes[prof])},
            cwd=str(homes[prof]),
        )
        out = _child_result(cp.stdout)
        if out is None:
            tail = " ".join((cp.stderr or cp.stdout or "").strip().splitlines()[-5:])
            err = f"apply exited {cp.returncode} without a result: {tail}"
    except subprocess.TimeoutExpired:
        outcome = "unknown"
        err = (
            f"apply did not answer within {APPLY_TIMEOUT}s and was stopped; it may or may not "
            f"have changed the skill. Check `hermes -p {prof} curator ledger` and the skill "
            "before retrying"
        )
    except OSError as e:
        err = f"cannot start the apply process: {e}"
    if out is not None:
        return 200, {**out, "profile": prof}
    # the child recorded nothing: record it so a reload still shows it
    res = apply_mod.result(outcome, err, kind, tid)
    conn = store.open_db(homes[prof])
    try:
        apply_mod.log_attempt(conn, res, "dashboard")
    finally:
        conn.close()
    return 200, {**res, "profile": prof}


# export.build reads every profile's state.db and ledger (seconds on a busy install): reuse the last
# result while none of the files it reads changed
_Fingerprint: TypeAlias = tuple[tuple[str, int, int], ...]


class _Cache(TypedDict):
    key: _Fingerprint | None
    data: Export | None


_cache: _Cache = {"key": None, "data": None}
_cache_lock = threading.Lock()


def _fingerprint(root: Path) -> _Fingerprint:
    parts: list[tuple[str, int, int]] = []
    for _name, home in sorted(store.profile_homes(root).items()):
        db = store.db_path(home)
        for p in (
            home / "state.db",
            home / "state.db-wal",
            db,
            Path(str(db) + "-wal"),
            home / "skills" / ".curator_ledger.jsonl",
            home / "pending" / "skills",  # pending: staged applies
        ):
            try:
                s = p.stat()
                parts.append((str(p), s.st_mtime_ns, s.st_size))
            except OSError:
                parts.append((str(p), 0, 0))
    return tuple(parts)


def _data() -> Export:
    root = _root()
    key = _fingerprint(root)
    with _cache_lock:
        if _cache["key"] == key and _cache["data"] is not None:
            return _cache["data"]
    d = export.build(root)
    with _cache_lock:
        _cache["key"] = key
        _cache["data"] = d
    return d


async def data(profile: str | None = None) -> Export | JSONResponse:
    # ?profile is added by the host's fetchJSON; the export always covers every profile of the
    # install
    if (stale := _stale()) is not None:
        return stale
    return await run_in_threadpool(_data)


async def status(profile: str | None = None) -> Status | JSONResponse:
    if (stale := _stale()) is not None:
        return stale
    return await run_in_threadpool(_status)


async def refresh(profile: str | None = None) -> JSONResponse:
    body: RefreshBody
    if (stale := _stale()) is not None:
        return stale
    if await run_in_threadpool(_derive_running):
        body = {"started": False, "running": True}
        return JSONResponse(body, status_code=202)
    # the dashboard's PATH has no `hermes` and its cwd cannot import hermes_cli: use the
    # install's launcher
    cmd = cli._hermes_cmd() + ["argus", "derive", "--all", "--quiet"]
    global _refresh
    try:
        _refresh = cli.spawn_logged(cmd, cli.derive_log(_root()), env=os.environ.copy())
    except OSError as e:
        body = {"started": False, "running": False, "error": str(e)}
        return JSONResponse(body, status_code=500)
    body = {"started": True, "running": True}
    return JSONResponse(body, status_code=202)


_INVALID_JSON: Final[ErrorBody] = {"error": "invalid JSON body"}


async def decision(request: Request, profile: str | None = None) -> JSONResponse:
    # ?profile is added by the host's fetchJSON; the target profile is the body's `profile`
    if (stale := _stale()) is not None:
        return stale
    try:
        body: object = json.loads(await request.body())  # what request.json() does
    except ValueError:
        return JSONResponse(_INVALID_JSON, status_code=400)
    result: tuple[int, ErrorBody | DecisionDone] = await run_in_threadpool(_decide, body)
    code, out = result
    return JSONResponse(out, status_code=code)


async def apply(request: Request, profile: str | None = None) -> JSONResponse:
    # ?profile is added by the host's fetchJSON; the target profile is the body's `profile`
    if (stale := _stale()) is not None:
        return stale
    try:
        body: object = json.loads(await request.body())  # what request.json() does
    except ValueError:
        return JSONResponse(_INVALID_JSON, status_code=400)
    result: tuple[int, ErrorBody | ApplyBody] = await run_in_threadpool(_apply, body)
    code, out = result
    return JSONResponse(out, status_code=code)


# Registered like the `@router.get`/`@router.post` decorators do (their type is not checkable).
# The routes return plain dicts serialized as before: `response_model=None` keeps FastAPI from
# turning the return annotation into a response model.
router.add_api_route("/data", data, methods=["GET"], response_model=None)
router.add_api_route("/status", status, methods=["GET"], response_model=None)
router.add_api_route("/refresh", refresh, methods=["POST"])
router.add_api_route("/decision", decision, methods=["POST"])
router.add_api_route("/apply", apply, methods=["POST"])
