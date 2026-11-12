"""`hermes argus status|backfill|derive|export|apply`, the `/argus` slash command, the llm adapter
for derive.run and the idle-triggered detached derive."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal, SupportsFloat, SupportsIndex, TextIO, TypeAlias, TypedDict

from pydantic import TypeAdapter

from . import apply, auto, backfill, derive, export, hermes_data, hermes_internals, store
from .dbtypes import count, query, sql_value
from .hermes_api import CliHandler, Llm, PluginContext
from .jsontypes import JsonObject, JsonValue, as_object, closed_schema, loads_object

log = logging.getLogger("hermes.plugins.argus")

ConfigKey: TypeAlias = Literal[
    "interval_minutes", "idle_minutes", "since_days", "auto_apply", "auto_apply_per_week"
]

DEFAULTS: Final[dict[ConfigKey, int | bool]] = {
    "interval_minutes": 60,
    "idle_minutes": 10,
    "since_days": 30,
    "auto_apply": False,
    "auto_apply_per_week": 3,
}
LAST_RUN: Final = "derive_last_run"
LAST_SPAWN: Final = "derive_last_spawn"
TABLES: Final = (
    "tool_outcome",
    "turn_outcome",
    "skill_edit",
    "intent",
    "turn_intent",
    "run",
    "proposal",
    "decision",
    "apply_log",
)


def config(ctx: PluginContext, key: ConfigKey) -> object:
    try:
        v = ctx.get_config(key, DEFAULTS[key])
    except Exception:
        v = DEFAULTS[key]
    return DEFAULTS[key] if v is None else v


def auto_policy(ctx: PluginContext) -> tuple[bool, int]:
    """This profile's auto-apply settings: (on, successful applies allowed per 7 days)."""
    return (
        auto.setting_on(config(ctx, "auto_apply")),
        auto.setting_per_week(config(ctx, "auto_apply_per_week")),
    )


def _float(value: object) -> float:
    """`float(value)` of an untyped value (config, ledger JSON): what float() accepts, else the
    TypeError float() raises."""
    if isinstance(value, (str, bytes, bytearray, memoryview, SupportsFloat, SupportsIndex)):
        return float(value)
    raise TypeError(
        f"float() argument must be a string or a real number, not '{type(value).__name__}'"
    )


# ---------------------------------------------------------------- llm adapter
def make_llm(ctx: PluginContext) -> Llm:
    """derive.run's `llm(instructions, text, schema, purpose) -> dict | None` over ctx.llm (aux task
    `argus`)."""

    def llm(instructions: str, text: str, schema: JsonObject, purpose: str) -> JsonObject | None:
        try:
            r = ctx.llm.complete_structured(
                instructions=instructions,
                input=[{"type": "text", "text": text}],
                json_schema=closed_schema(schema),  # every object closed, as providers require
                schema_name="argus_" + re.sub(r"\W+", "_", purpose or "call").strip("_"),
                purpose=f"argus:{purpose}",
                task="argus",
                temperature=0,
                timeout=300,
            )
        except Exception as e:
            log.warning("argus: llm call %s failed: %s", purpose, e)
            return None
        parsed: object = getattr(r, "parsed", None)
        return as_object(parsed)

    return llm


# ---------------------------------------------------------------- derive lock
def lock_path(home: Path) -> Path:
    return store.data_dir(home) / "derive.lock"


def try_lock(home: Path) -> TextIO | None:
    """Exclusive non-blocking lock on the profile's derive lock file; the open file, or None if
    held."""
    path = lock_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+")  # the open file is the lock, handed to the caller
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def unlock(f: TextIO) -> None:
    try:
        fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def lock_held(home: Path) -> bool:
    """Whether a derive holds the profile's lock now; creates nothing and writes nothing."""
    try:
        f = lock_path(home).open()  # closed in the finally below
    except OSError:
        return False  # never derived here
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(f, fcntl.LOCK_UN)
        return False
    finally:
        f.close()


# ---------------------------------------------------------------- idle-triggered derive
def _group(m: re.Match[str], n: int) -> str:
    """Group `n` of a match, for a group that is not optional (so it always took part)."""
    g: object = m.group(n)
    if isinstance(g, str):
        return g
    raise ValueError(f"group {n} did not match: {m.string!r}")


def _launcher_ok(path: Path) -> bool:
    """A git install's launcher `exec`s a fixed interpreter path; a checkout copied from another
    machine keeps a path that does not exist here. An unknown launcher format is trusted."""
    try:
        m = re.search(r"^exec (/\S+)", path.read_text(encoding="utf-8", errors="replace"), re.M)
    except OSError:
        return False
    return m is None or Path(_group(m, 1)).is_file()


def _hermes_cmd() -> list[str]:
    """The `hermes` launcher of the running install (git installs: <repo>/.hermes/bin/hermes; the
    gateway runs `python3 -I -c ...`, so sys.executable alone cannot import hermes_cli)."""
    spec = importlib.util.find_spec("hermes_cli")
    repo = Path(spec.origin).parent.parent if spec and spec.origin else None
    launcher = repo / ".hermes" / "bin" / "hermes" if repo is not None else None
    if launcher is not None and launcher.is_file() and _launcher_ok(launcher):
        return [str(launcher)]
    found = shutil.which("hermes")
    if found:
        return [found]
    return [sys.executable, "-m", "hermes_cli.main"]


def derive_log(home: Path) -> Path:
    return store.data_dir(home) / "derive.log"


EXIT_LINE: Final = re.compile(r"^\[argus\] exit (-?\d+) at (\d+(?:\.\d+)?)$")


class LastExit(TypedDict):
    """The last finished run of a derive.log."""

    exit_code: int
    at: float
    log: str  # its last output lines


def spawn_logged(
    cmd: list[str], log_file: Path, env: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    """Start `cmd` detached, stdout/stderr appended to `log_file`; a daemon thread reaps it (no
    zombie) and appends an `[argus] exit <code> at <epoch>` line that `last_exit` reads back."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab") as out:
        out.write(f"[argus] start at {time.time():.0f}: {' '.join(cmd)}\n".encode())
        out.flush()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env,
        )

    def reap() -> None:
        rc = proc.wait()
        try:
            with log_file.open("ab") as out:
                out.write(f"[argus] exit {rc} at {time.time():.0f}\n".encode())
        except OSError:
            log.warning("argus: cannot write %s", log_file, exc_info=True)

    threading.Thread(target=reap, name="argus-derive-reaper", daemon=True).start()
    return proc


def last_exit(log_file: Path, lines: int = 20) -> LastExit | None:
    """Last finished run recorded in a derive.log: `{exit_code, at, log}` (log = its last output
    lines)."""
    try:
        with log_file.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 32768))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for i in range(len(tail) - 1, -1, -1):
        m = EXIT_LINE.match(tail[i])
        if m:
            start = max(
                (j for j in range(i) if tail[j].startswith("[argus] start at ")), default=-1
            )
            out = [x for x in tail[start + 1 : i] if x.strip()]
            return {
                "exit_code": int(_group(m, 1)),
                "at": float(_group(m, 2)),
                "log": "\n".join(out[-lines:]),
            }
    return None


def spawn_derive(home: Path) -> bool:
    """Start a detached `hermes -p <profile> argus derive --quiet` unless one is running."""
    held = try_lock(home)
    if held is None:
        return False
    unlock(held)  # the child takes the lock itself; it exits at once if another one won the race
    spawn_logged(
        [*_hermes_cmd(), "-p", store.profile_name(home), "argus", "derive", "--quiet"],
        derive_log(home),
        env={**os.environ, "HERMES_HOME": str(home)},
    )
    return True


class IdleScheduler:
    """After each turn, wait `idle_minutes` of quiet; then derive if none ran (or was spawned) in
    `interval_minutes`."""

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def turn_ended(self, home: Path) -> None:
        idle = _float(config(self.ctx, "idle_minutes")) * 60
        t = threading.Timer(idle, self._check, args=(Path(home),))
        t.daemon = True
        with self._lock:
            old = self._timers.pop(str(home), None)
            if old is not None:
                old.cancel()
            self._timers[str(home)] = t
        t.start()

    def _check(self, home: Path) -> None:
        try:
            with self._lock:
                self._timers.pop(str(home), None)
            interval = _float(config(self.ctx, "interval_minutes")) * 60
            conn = store.open_db(home)
            try:
                last = max(
                    float(store.meta_get(conn, LAST_RUN, 0) or 0),
                    float(store.meta_get(conn, LAST_SPAWN, 0) or 0),
                )
                if time.time() - last < interval:
                    return
                if spawn_derive(home):
                    store.meta_set(conn, LAST_SPAWN, time.time())
            finally:
                conn.close()
        except Exception:
            log.warning("argus: idle derive check failed", exc_info=True)


# ---------------------------------------------------------------- CLI
def setup(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(
        dest="argus_cmd", metavar="{status,backfill,derive,export,apply,auto-apply}"
    )
    sub.add_parser(
        "status",
        help="plugin data per profile: rows, backfill cursor, last derive, model calls of the"
        " next derive",
    )
    p = sub.add_parser(
        "backfill", help="read past sessions and skill edits (read-only) into the plugin DB"
    )
    p.add_argument(
        "--profile", dest="only_profile", help="profile name (default: the active profile)"
    )
    p.add_argument("--all", action="store_true", help="every profile of this install")
    p.add_argument(
        "--since",
        type=float,
        metavar="DAYS",
        help="set the history window derive and the dashboard read to the last DAYS days, or widen"
        " it (0 = whole history); it never narrows (default: config since_days, only on a"
        " profile's first backfill)",
    )
    p = sub.add_parser(
        "derive", help="assign, consolidate, judge, review edits, propose (model calls)"
    )
    p.add_argument(
        "--profile", dest="only_profile", help="profile name (default: the active profile)"
    )
    p.add_argument("--all", action="store_true", help="every profile of this install")
    p.add_argument(
        "--stage",
        action="append",
        help="run only this stage (repeatable): assign, consolidate, judge, "
        "runs, edit_review, proposals",
    )
    p.add_argument("--quiet", action="store_true", help="no progress output")
    p = sub.add_parser("export", help="write the dashboard data (window.DATA) of this install")
    p.add_argument(
        "--out",
        help="output file; .js writes `window.DATA=...;`, anything else JSON (default: stdout)",
    )
    p.add_argument(
        "--profile",
        dest="only_profile",
        action="append",
        help="only these profiles (repeatable)",
    )
    p = sub.add_parser(
        "apply",
        help="carry out a decision in this profile through Hermes (actor user): roll back a "
        "ledger entry or insert a proposed rule with skill_manage",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--proposal", metavar="ID", help="proposal id: patch its skill with skill_manage"
    )
    g.add_argument(
        "--rollback",
        metavar="ENTRY_ID",
        help="ledger entry id: rollback_entry, only if no later change",
    )
    p.add_argument("--json", action="store_true", help="one JSON object on stdout")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="run every check and show the change; write nothing (no decision needed)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="apply even without a current reject/accept decision (recorded in apply_log)",
    )
    sub.add_parser(
        "auto-apply",
        help="show what the auto_apply policy would apply in this profile now, even with the"
        " setting off; writes nothing (with auto_apply on, derive applies it after each pass)",
    )


_STRS: Final = TypeAdapter[list[str]](list[str])


def _arg_str(args: argparse.Namespace, name: str) -> str | None:
    """A string option of `setup`'s parser (None when not given)."""
    v: object = getattr(args, name)
    if v is None or isinstance(v, str):
        return v
    raise TypeError(f"argus: option {name} is {type(v).__name__}, not a string")


def _arg_strs(args: argparse.Namespace, name: str) -> list[str] | None:
    """A repeatable (`action="append"`) string option (None when not given)."""
    v: object = getattr(args, name)
    return None if v is None else _STRS.validate_python(v, strict=True)


def _arg_float(args: argparse.Namespace, name: str) -> float | None:
    """A `type=float` option (None when not given)."""
    v: object = getattr(args, name)
    if v is None or isinstance(v, float):
        return v
    raise TypeError(f"argus: option {name} is {type(v).__name__}, not a float")


def _arg_flag(args: argparse.Namespace, name: str) -> bool:
    """A `store_true` option."""
    v: object = getattr(args, name)
    if isinstance(v, bool):
        return v
    raise TypeError(f"argus: option {name} is {type(v).__name__}, not a flag")


def _scope(args: argparse.Namespace, home: Path) -> list[str] | None:
    """Profiles a command acts on: --profile, --all (None = every profile), else the active one.
    Hermes' pre-argparse scan usually consumes `--profile P` / `-p P` itself and makes P the active
    profile, so `hermes argus backfill --profile P` ends up in the default branch with P active."""
    only: object = getattr(args, "only_profile", None)
    if only:
        if not isinstance(only, str):
            raise TypeError(f"argus: option only_profile is {type(only).__name__}, not a string")
        return [only]
    every: object = getattr(args, "all", False)
    return None if every else [store.profile_name(home)]


def _since_days(since: object) -> float | None:
    """The window request of config since_days (backfill without --since; it only applies to a
    profile's first backfill): 0 or unset = none."""
    if not since:
        return None
    if isinstance(since, (int, float)):
        return since
    raise TypeError(f"argus: since_days must be a number of days, not {type(since).__name__}")


def _fmt_time(t: object) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(_float(t))) if t else "never"


def status(root: Path, home: Path) -> str:
    lines = [f"argus — install {root} (active profile: {store.profile_name(home)})"]
    homes = store.profile_homes(root)
    if not homes:
        lines.append("  no profile with a state.db")
    for name, h in homes.items():
        db = store.db_path(h)
        if not db.exists():
            lines.append(f"  {name}: no plugin data yet (run `hermes -p {name} argus backfill`)")
            continue
        conn = store.open_db(h)
        cur: JsonValue
        try:
            counts = ", ".join(f"{t} {count(conn, f'select count(*) from {t}')}" for t in TABLES)
            try:
                cur = loads_object(store.meta_get(conn, store.BACKFILL_CURSOR) or "{}").get("at")
            except ValueError:  # not JSON, or not an object
                cur = None
            last = store.meta_get(conn, LAST_RUN)
            window = store.window_start(conn)
        finally:
            conn.close()
        lines.append(f"  {name}: {counts}")
        pad = f"  {' ' * len(name)}  "
        span = f"{pad}backfill {_fmt_time(cur)}, derive {_fmt_time(last)}"
        if window is not None:  # absent = never requested: the whole history, said nowhere
            span += f", history from {_fmt_time(window)}" if window else ", whole history"
        lines.append(span)
        try:
            est = derive.estimate(h)
        except hermes_data.HermesFormatError as e:
            lines.append(f"{pad}next derive: no estimate: {e}")
            continue
        if est is None:
            lines.append(f"{pad}next derive: skipped ({derive.UNBACKFILLED})")
        else:
            lines += [pad + line for line in derive.estimate_lines(est)]
    return "\n".join(lines)


def auto_status(home: Path, on: bool, per_week: int) -> str:
    """The active profile's auto-apply line of `hermes argus status`."""
    prof = store.profile_name(home)
    if not on:
        return f"  auto-apply ({prof}): off"
    n = 0
    if store.db_path(home).exists():
        conn = store.open_db(home)
        try:
            n = auto.used(conn, time.time())
        finally:
            conn.close()
    return f"  auto-apply ({prof}): on, {n} of {per_week} used in the last 7 days"


def _silent(_line: str) -> None:
    return None


def run_derive(
    ctx: PluginContext,
    root: Path,
    profiles: list[str] | None,
    stages: list[str] | None,
    quiet: bool,
    home: Path | None = None,
) -> int:
    names = profiles or list(store.profile_homes(root))
    homes = {n: h for n, h in store.profile_homes(root).items() if n in names}
    if unknown := [s for s in stages or [] if s not in derive.STAGES]:
        print(
            f"argus: unknown stage(s): {', '.join(unknown)}; expected {', '.join(derive.STAGES)}",
            file=sys.stderr,
        )
        return 2
    if not homes:
        print(
            f"argus: no state.db yet for {', '.join(names) or 'any profile'}: nothing to derive"
            " (Hermes creates it on the profile's first chat)",
            file=sys.stderr,
        )
        return 1
    locks: dict[str, TextIO] = {}
    try:
        for n, h in homes.items():
            f = try_lock(h)
            if f is None:
                print(f"argus: derive already running for {n}", file=sys.stderr)
                return 1
            locks[n] = f
        say: Callable[[str], object] = _silent if quiet else print
        # catch up first with what no hook recorded (a profile without the plugin enabled, edits
        # made while it was off): tool outcomes and skill edits since each profile's cursor; the
        # window stays as it is, and a profile never backfilled stays skipped
        if caught_up := [n for n, h in homes.items() if store.backfilled(h)]:
            backfill.run(root, profiles=caught_up, log=say, estimate=False)
        report = derive.run(root, make_llm(ctx), profiles=list(homes), stages=stages, log=say)
        for h in homes.values():
            conn = store.open_db(h)
            try:
                store.meta_set(conn, LAST_RUN, time.time())
            finally:
                conn.close()
        if not quiet:
            print(json.dumps(report, indent=2, default=str))
        if aborted := report.get("aborted"):
            print(f"argus: derive {aborted}; run it again once the model answers", file=sys.stderr)
            return 1
        # auto-apply only where this process runs: its settings are this profile's, and Hermes'
        # skill functions write this profile's skills
        # only after a clean pass of this very profile: a profile or stage error leaves the
        # proposals it would act on unverified
        mine = next(
            (n for n, h in homes.items() if home is not None and h.resolve() == home.resolve()),
            None,
        )
        rep = report["profiles"].get(mine or "")
        clean = (
            rep is not None
            and "error" not in rep
            and "skipped" not in rep
            and not any("error" in st for st in rep["stages"].values())
        )
        if home is not None and clean:
            on, per_week = auto_policy(ctx)
            if on:
                run_auto(home, per_week, dry_run=False, quiet=quiet)
        return 0
    finally:
        for f in locks.values():
            unlock(f)


def run_auto(home: Path, per_week: int, dry_run: bool, quiet: bool = False) -> int:
    """Auto-apply in the profile at `home`; one line per proposal tried, printed even when `quiet`
    (a skill write belongs in derive.log). Exit 0 unless an attempt failed (refused | failed |
    unknown)."""
    try:
        results = auto.run(home, per_week, dry_run=dry_run)
    except Exception as e:  # never let it fail the derive pass that ran before it
        log.warning("argus: auto-apply failed", exc_info=True)
        print(f"argus: auto-apply failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    prof = store.profile_name(home)
    if not results and not quiet:
        print(f"argus auto-apply ({prof}): nothing to apply")
    for p, r in results:
        print(f"argus auto-apply ({prof}): proposal {p['id']} on {p['skill_name']}: {r['outcome']}")
        print(f"  rule: {p['rule']}\n  {r['message']}")
        if r.get("ledger_entry"):
            print(f"  undo: hermes -p {prof} curator rollback {r['ledger_entry']}")
    return 0 if all(r["ok"] for _p, r in results) else 1


def run_export(root: Path, out: str | None, profiles: list[str] | None) -> int:
    data = json.dumps(export.build(root, profiles=profiles), default=str)
    if not out:
        print(data)
        return 0
    Path(out).write_text(
        f"window.DATA={data};\n" if out.endswith(".js") else data + "\n", encoding="utf-8"
    )
    print(f"argus: wrote {out}")
    return 0


def run_apply(home: Path, args: argparse.Namespace) -> int:
    """`hermes argus apply`: exit 0 when applied or staged (or a passing --dry-run), 1 otherwise."""
    proposal = _arg_str(args, "proposal")
    kind, tid = ("proposal", proposal) if proposal else ("skill_edit", _arg_str(args, "rollback"))
    if tid is None:  # `--proposal ""`: the exclusive group then leaves no target
        print(
            "hermes argus apply: error: argument --proposal: expected a non-empty ID",
            file=sys.stderr,
        )
        return 2
    r = apply.run(
        home,
        kind,
        tid,
        dry_run=_arg_flag(args, "dry_run"),
        applied_by="cli",
        force=_arg_flag(args, "force"),
    )
    if _arg_flag(args, "json"):
        print(json.dumps(r, ensure_ascii=False))
    else:
        what = f"proposal {tid}" if kind == "proposal" else f"rollback of ledger entry {tid}"
        print(f"argus apply ({store.profile_name(home)}): {what}: {r['outcome']}\n  {r['message']}")
        if r.get("ledger_entry"):
            print(f"  ledger entry {r['ledger_entry']} (actor user)")
        if "old_string" in r:
            print(f"--- old_string\n{r['old_string']}")
        if "new_string" in r:
            print(f"--- new_string\n{r['new_string']}")
    return 0 if r["ok"] else 1


def make_handler(ctx: PluginContext) -> CliHandler:
    def handler(args: argparse.Namespace) -> int:
        home = hermes_internals.hermes_home()
        root = store.root_of(home)
        cmd_arg: object = getattr(args, "argus_cmd", None)
        cmd = cmd_arg or "status"
        if cmd == "status":
            print(status(root, home))
            print(auto_status(home, *auto_policy(ctx)))
            return 0
        if cmd == "backfill":
            since = _arg_float(args, "since")  # explicit: --since 0 asks for the whole history
            default = _since_days(config(ctx, "since_days"))  # since_days 0 asks nothing
            if any(d is not None and d < 0 for d in (since, default)):
                print(
                    "hermes argus backfill: error: --since / since_days must be 0 (whole history)"
                    " or a positive number of days",
                    file=sys.stderr,
                )
                return 2
            backfill.run(root, profiles=_scope(args, home), since_days=since, default_days=default)
            return 0
        if cmd == "derive":
            return run_derive(
                ctx,
                root,
                _scope(args, home),
                _arg_strs(args, "stage"),
                _arg_flag(args, "quiet"),
                home,
            )
        if cmd == "export":
            return run_export(root, _arg_str(args, "out"), _arg_strs(args, "only_profile"))
        if cmd == "apply":
            return run_apply(home, args)
        if cmd == "auto-apply":
            print(auto_status(home, *auto_policy(ctx)).strip())
            return run_auto(home, auto_policy(ctx)[1], dry_run=True)
        return 2

    return handler


# ---------------------------------------------------------------- /argus
def slash(
    raw_args: str = "", /, session_id: object = None, platform: object = None, **kwargs: object
) -> str:
    """Intent(s) of the calling session and the profile's counts. On a messaging platform, where
    any allowed user of the chat can ask, only the session's own intents: no judge advice, no
    profile-wide counts."""
    full = platform == "cli"
    home = hermes_internals.hermes_home()
    if not store.db_path(home).exists():
        return "argus: no data yet — run `hermes argus backfill`, then `hermes argus derive`."
    conn = store.open_db(home)
    try:
        lines: list[str] = []
        if session_id:
            sid = sql_value(session_id)
            rows = query(
                conn,
                "select i.title, i.status, i.next, count(*) n from turn_intent t join intent i on "
                "i.id = t.intent_id where t.session_id = ? group by i.id "
                "order by max(t.user_message_id) desc",
                (sid,),
            )
            lines.extend(
                f"• {r['title']!s} — {r['status'] or 'not judged'!s} "
                f"({r['n']!s} turn{'s' if r['n'] != 1 else ''})"
                + (f"\n  next: {r['next']!s}" if r["next"] and full else "")
                for r in rows
            )
            errs = count(
                conn,
                "select count(*) from tool_outcome where session_id = ? and error_sig is not null",
                (sid,),
            )
            if not rows:
                lines.append(
                    "This session has no intent yet (derivation runs when the agent is idle)."
                )
            if errs:
                lines.append(f"{errs} failed tool call{'s' if errs != 1 else ''} in this session.")
        if not full:
            return (
                "\n".join(lines) or "argus: open the Argus tab of `hermes dashboard` for details."
            )
        intents = store.live_intents(conn)
        spec = store.speculative_edits(conn)
        props = store.pending_proposals(conn, home, ("accept", "dismiss"))
        lines.append(
            f"Profile: {intents} intents, {spec} skill edits without evidence, "
            f"{props} proposals to review."
        )
        return "\n".join(lines)
    finally:
        conn.close()
