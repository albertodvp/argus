"""Plugin storage: one SQLite file per Hermes profile at <home>/plugin-data/argus/data.db.

Free of Hermes imports at module level, so the dashboard and the CLI can import it standalone
(the profile's skill directories read Hermes' YAML reader lazily, when present).
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, TypeAlias, TypeVar, overload

from pydantic import TypeAdapter

from .dbtypes import PROPOSAL_ROW, SQLITE_ROW, count, query_as, query_one

T = TypeVar("T")

PLUGIN: Final = "argus"
# 2 adds apply_log. schema.sql is idempotent (create ... if not exists): an older DB upgrades in
# place on its next open_db
SCHEMA_VERSION: Final = 2
SCHEMA_FILE: Final = Path(__file__).with_name("schema.sql")

log = logging.getLogger("hermes.plugins.argus")
_MAP: Final = TypeAdapter(dict[str, object])
_LIST: Final = TypeAdapter(list[object])


def profile_homes(root: Path) -> dict[str, Path]:
    """`{"default": root, "<name>": root/profiles/<name>}` for every profile dir holding a
    state.db."""
    root = Path(root)
    out: dict[str, Path] = {}
    if (root / "state.db").exists():
        out["default"] = root
    pdir = root / "profiles"
    if pdir.is_dir():
        for p in sorted(pdir.iterdir()):
            if (p / "state.db").exists():
                out[p.name] = p
    return out


def profile_name(home: Path) -> str:
    """Profile name of a Hermes home: the dir name under `profiles/`, else `default`."""
    home = Path(home)
    return home.name if home.parent.name == "profiles" else "default"


def root_of(home: Path) -> Path:
    """Install root (the default profile's home) of a profile home."""
    home = Path(home)
    return home.parent.parent if home.parent.name == "profiles" else home


def data_dir(home: Path) -> Path:
    return Path(home) / "plugin-data" / PLUGIN


def db_path(home: Path) -> Path:
    return data_dir(home) / "data.db"


def open_db(home: Path) -> sqlite3.Connection:
    """Open (creating if needed) the profile's plugin DB with the schema applied idempotently."""
    path = db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = SQLITE_ROW
    conn.execute("pragma busy_timeout = 30000")
    stored = str(meta_get(conn, "schema_version") or "")
    if not stored.isdigit() or int(stored) < SCHEMA_VERSION:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        meta_set(conn, "schema_version", SCHEMA_VERSION)
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma foreign_keys = on")
    return conn


@overload
def meta_get(conn: sqlite3.Connection, key: str) -> str | None: ...


@overload
def meta_get(conn: sqlite3.Connection, key: str, default: T) -> str | T: ...


def meta_get(conn: sqlite3.Connection, key: str, default: object = None) -> object:
    """The value stored under `key` (meta.value is TEXT), or `default` when there is none."""
    try:
        r = query_one(conn, "select value from meta where key = ?", (key,))
    except sqlite3.OperationalError:  # no meta table yet
        return default
    if r is None:
        return default
    value = r["value"]
    if not isinstance(value, str):
        raise TypeError(f"meta {key!r} is not text: {type(value).__name__}")
    return value


def meta_set(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "insert into meta(key, value) values (?, ?)"
        " on conflict(key) do update set value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


# meta `window_start`: epoch seconds where the profile's history window starts. Backfill sets it
# from --since / since_days and only ever widens it; derive and export read the sessions active in
# it and the ledger from there on. Absent = the whole history (0: the whole history, asked for).
WINDOW: Final = "window_start"
# meta key backfill writes on every run (backfill.CURSOR)
BACKFILL_CURSOR: Final = "backfill_cursor"


def window_start(conn: sqlite3.Connection) -> float | None:
    """Start of the profile's history window (epoch seconds), None for the whole history."""
    value = meta_get(conn, WINDOW)
    return float(value) if value else None


def _read_meta(home: Path, key: str) -> str | None:
    """meta `key` of a profile, read-only: None also when the profile has no plugin DB yet."""
    path = db_path(home)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        return meta_get(conn, key)
    finally:
        conn.close()


def read_window(home: Path) -> float | None:
    """window_start of a profile, read-only (None: no window, or no plugin DB yet)."""
    value = _read_meta(home, WINDOW)
    return float(value) if value else None


def backfilled(home: Path) -> bool:
    """Whether `hermes argus backfill` ever ran on the profile."""
    return _read_meta(home, BACKFILL_CURSOR) is not None


def live_intents(conn: sqlite3.Connection) -> int:
    """Live (not merged) intents; with a window, only those with a run started in it, as the
    dashboard lists them."""
    w = window_start(conn)
    if not w:
        return count(conn, "select count(*) from intent where merged_into is null")
    return count(
        conn,
        "select count(*) from intent i where i.merged_into is null and exists"
        " (select 1 from run r where r.intent_id = i.id and r.started_at >= ?)",
        (w,),
    )


def speculative_edits(conn: sqlite3.Connection) -> int:
    """Skill edits reviewed as speculative (no evidence in their run), inside the window."""
    w = window_start(conn)
    if not w:
        return count(conn, "select count(*) from skill_edit where evidence = 'speculative'")
    return count(
        conn,
        "select count(*) from skill_edit where evidence = 'speculative' and ts >= ?",
        (w,),
    )


def _config_skill_dirs(home: Path) -> list[Path]:
    """`skills.create_dir` and `skills.external_dirs` of the profile's config.yaml, in Hermes'
    lookup order, as Hermes reads them (`~` and `${VAR}` expanded, a relative path under the
    profile home); none when the config or Hermes' config reader is missing or unreadable."""
    try:
        from hermes_cli.config import read_user_config_raw

        cfg = read_user_config_raw(Path(home) / "config.yaml")
        skills = _MAP.validate_python(cfg.get("skills") or dict[str, object]())
    except Exception:
        return []
    ext = skills.get("external_dirs")
    try:
        listed = _LIST.validate_python(ext)
    except ValueError:  # a single path written as a scalar
        listed = [ext]
    out: list[Path] = []
    for e in [skills.get("create_dir"), *listed]:
        if isinstance(e, str) and e.strip():
            p = Path(os.path.expanduser(os.path.expandvars(e.strip())))
            out.append(p if p.is_absolute() else Path(home) / p)
    return out


def skill_roots(home: Path) -> list[Path]:
    """The directories Hermes finds the profile's skills in, in its lookup order: `skills/`, then
    `skills.create_dir` and `skills.external_dirs` (existing ones only)."""
    roots = [Path(home) / "skills"]
    for p in _config_skill_dirs(home):
        if p.is_dir() and all(p.resolve() != r.resolve() for r in roots):
            roots.append(p)
    return roots


def skill_files(home: Path) -> dict[str, Path]:
    """Current SKILL.md of every skill of the profile, by skill name, as Hermes finds them: the
    first root that has it (`skill_roots`), hidden directories such as the curator's archive
    excluded."""
    out = dict[str, Path]()
    for base in skill_roots(home):
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("SKILL.md")):
            if any(part.startswith(".") for part in p.relative_to(base).parts):
                continue
            out.setdefault(p.parent.name, p)
    return out


def skill_rel(home: Path, path: Path) -> str:
    """`path` relative to the skill root holding it (`<category>/<skill>/SKILL.md`); absolute
    when none does."""
    for base in skill_roots(home):
        try:
            return str(path.relative_to(base))
        except ValueError:
            continue
    return str(path)


def skill_writable(path: Path) -> bool:
    """Whether Hermes can rewrite this SKILL.md here: the file and its directory are writable. A
    shared skill directory (`skills.external_dirs`) is often kept read-only."""
    return os.access(path, os.W_OK) and os.access(path.parent, os.W_OK)


def pending_proposals(conn: sqlite3.Connection, home: Path, decided: Sequence[str]) -> int:
    """Proposals without a decision among `decided` that still make sense: a new skill, or an
    existing one that is still in the profile (a proposal for a removed skill is obsolete)."""
    marks = ",".join("?" * len(decided))
    rows = query_as(
        conn,
        PROPOSAL_ROW,
        "select * from proposal p where not exists (select 1 from decision d where "
        f"d.target_kind = 'proposal' and d.target_id = p.id and d.action in ({marks}))",
        tuple(decided),
    )
    present = skill_files(home)
    return sum(1 for r in rows if r["is_new_skill"] or r["skill_name"] in present)


WriteFn: TypeAlias = Callable[[sqlite3.Connection], None]
_Item: TypeAlias = tuple[str | WriteFn, Sequence[object]]


class Writer:
    """In-process write queue for one profile DB, drained by one daemon thread in batches.

    `put(sql, params)` and `call(fn)` (fn(conn) runs on the writer thread) never raise into the
    caller; a failing statement is logged and skipped. `params` are bound by sqlite3 on the
    writer thread: a value that is not an SQLite value fails (and is logged) there.
    """

    BATCH: Final = 200
    LINGER: Final = 0.2

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self._q: queue.Queue[_Item] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name=f"argus-writer:{profile_name(home)}", daemon=True
        )
        self._thread.start()

    def put(self, sql: str, params: Sequence[object] = ()) -> None:
        try:
            self._q.put_nowait((sql, tuple(params)))
        except Exception:
            log.debug("argus: enqueue failed", exc_info=True)

    def call(self, fn: WriteFn) -> None:
        try:
            self._q.put_nowait((fn, ()))
        except Exception:
            log.debug("argus: enqueue failed", exc_info=True)

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every queued item has been executed (or timeout); True when drained."""
        end = time.monotonic() + timeout
        while self._q.unfinished_tasks:
            if time.monotonic() > end or not self._thread.is_alive():
                return False
            time.sleep(0.02)
        return True

    def _run(self) -> None:
        conn: sqlite3.Connection | None = None
        while True:
            item = self._q.get()
            batch = [item]
            end = time.monotonic() + self.LINGER
            while len(batch) < self.BATCH:
                try:
                    batch.append(self._q.get(timeout=max(0.0, end - time.monotonic())))
                except queue.Empty:
                    break
            try:
                if conn is None:
                    conn = open_db(self.home)
                for op, params in batch:
                    try:
                        if callable(op):
                            op(conn)
                        else:
                            conn.execute(op, params)
                    except Exception:
                        log.warning(
                            "argus: write failed: %s",
                            op if isinstance(op, str) else op.__name__,
                            exc_info=True,
                        )
                conn.commit()
            except Exception:
                log.warning(
                    "argus: writer batch failed (%d items dropped)", len(batch), exc_info=True
                )
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
            finally:
                for _ in batch:
                    self._q.task_done()


_writers: dict[str, Writer] = {}
_writers_lock = threading.Lock()


def writer(home: Path) -> Writer:
    """The process-wide Writer of a profile home."""
    key = str(Path(home))
    with _writers_lock:
        w = _writers.get(key)
        if w is None:
            w = _writers[key] = Writer(Path(home))
        return w


@atexit.register
def _flush_all() -> None:
    for w in list(_writers.values()):
        w.flush(timeout=2.0)
