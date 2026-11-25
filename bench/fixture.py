"""The bench fixture: what backfill and derive read from one Hermes home, compact, and a scratch
HERMES_HOME made from it.

    uv run python -m bench.fixture build --home DIR [--since-days N] [--out bench/fixture.tar.xz]
    uv run python -m bench.fixture prepare DEST [--fixture bench/fixture.tar.xz]

`build` reads only the home passed with --home (never HERMES_HOME or ~/.hermes, which it refuses:
build it from the fictional demo home, never from a real user's) and writes a tar.xz holding
`manifest.json` and `home/`. Every file it copies is a regular file inside that home: a symlink on
its path, or a path resolving outside the home, is refused.

- `home/state.db`: only the `sessions` and `messages` tables (their own CREATE statements and
  indexes; no FTS, no other table), with the sessions active in the history window (started in it
  or with a message in it) and the sessions the window's ledger entries name, each with all its
  messages, vacuumed. Derive and backfill read nothing else of state.db.
- `home/skills/.curator_ledger.jsonl`: the ledger lines from the window start on, verbatim.
- `home/.curator_backups/blobs/`: the .md blobs those lines reference (derive reads no other).
- `home/skills/**/SKILL.md`: the current SKILL.md of every skill under `skills/` (hidden
  directories, such as the curator's archive, and every other skill file left out).
- `manifest.json`: the frozen `now` and `since_days` of the window (it starts `since_days` days
  before `now`), and counts. No config.yaml: the window is the only setting backfill and derive
  read, and the manifest holds it.

The window is the home's own Argus window (meta `window_start` of its plugin DB) unless
--since-days sets one, counted back from the build time, which becomes the fixture's `now`.

`prepare` unpacks `home/` into DEST (new or empty) and backfills it with the manifest's window at
its frozen `now`: the same history window, so the same prompts, on every run and every machine.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sqlite3
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Final

from pydantic import TypeAdapter
from typing_extensions import TypedDict

from argus import backfill, hermes_data, store
from argus.dbtypes import count, query

FORMAT: Final = 1
DAY: Final = backfill.DAY
DEFAULT: Final = Path(__file__).with_name("fixture.tar.xz")
TABLES: Final = ("sessions", "messages")
BLOB_NAME: Final = re.compile(r"[0-9A-Za-z_-]+")


class Manifest(TypedDict):
    format: int
    now: float  # the frozen clock: the window starts since_days days before it
    since_days: float  # 0: the whole history
    window_start: float
    counts: dict[str, int]


MANIFEST: Final = TypeAdapter(Manifest)


class FixtureError(Exception):
    """The home cannot be built into a fixture, or the fixture cannot be prepared."""


def _quiet(_line: str) -> None:
    return None


def _inside(home: Path, p: Path) -> Path:
    """`p`, a path under `home`, when neither it nor a directory between them is a symlink and it
    resolves inside `home`; FixtureError otherwise: the fixture would archive a file from
    elsewhere."""
    try:
        rel = p.relative_to(home)
    except ValueError:
        raise FixtureError(f"{p} is not inside {home}") from None
    cur = home
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            raise FixtureError(
                f"{cur} is a symlink: the fixture copies only files inside {home}, never links"
            )
    if not p.resolve().is_relative_to(home.resolve()):
        raise FixtureError(f"{p} resolves outside {home}")
    return p


def _refuse(home: Path) -> None:
    if home.resolve() == (Path.home() / ".hermes").resolve():
        raise FixtureError(
            f"refusing {home}: the bench fixture is built from the fictional demo home, never from"
            " a real user's Hermes home"
        )
    if not _inside(home, home / "state.db").is_file():
        raise FixtureError(f"no state.db at {home}: not a Hermes home")


def _window(home: Path, since_days: float | None, now: float) -> tuple[float, float]:
    """(since_days, window start) of the fixture: --since-days, else the home's Argus window."""
    if since_days is None:
        start = store.read_window(home)
        if start is None:
            raise FixtureError(f"{home} has no Argus history window: pass --since-days")
        since_days = (now - start) / DAY if start else 0.0
    if since_days < 0:
        raise FixtureError(f"--since-days must be 0 (whole history) or positive: {since_days}")
    since_days = float(since_days)
    # the expression backfill.window uses, so the copy and the window agree to the last bit
    return since_days, (now - since_days * DAY if since_days else 0.0)


def _ledger(home: Path, start: float) -> tuple[list[str], list[hermes_data.LedgerEntry]]:
    """The ledger lines (verbatim) and entries from `start` on. A timestamp without a UTC offset
    would read in the local time zone, so on another machine as another time: refused."""
    path = hermes_data.ledger_path(home)
    if not path.exists() and not path.is_symlink():
        return [], []
    _inside(home, path)
    lines: list[str] = []
    entries: list[hermes_data.LedgerEntry] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            e = hermes_data.parse_ledger_line(line)
            if e is None or e["ts_epoch"] < start:
                continue
            if datetime.fromisoformat(e["ts"]).tzinfo is None:
                raise FixtureError(f"ledger entry {e['id']}: time {e['ts']!r} has no UTC offset")
            lines.append(line if line.endswith("\n") else line + "\n")
            entries.append(e)
    return lines, entries


def _blobs(home: Path, entries: list[hermes_data.LedgerEntry]) -> dict[str, bytes]:
    """The .md blobs the entries reference, as derive reads them (larger ones it ignores)."""
    dirs = hermes_data.blob_dirs(home)
    out: dict[str, bytes] = {}
    for e in entries:
        for side in (e.get("before"), e.get("after")):
            for it in side or []:
                sha = it.get("sha256")
                if not sha or sha in out or not it.get("path", "").endswith(".md"):
                    continue
                if not BLOB_NAME.fullmatch(sha):
                    continue
                for d in dirs:
                    p = d / sha
                    if p.exists() or p.is_symlink():
                        _inside(home, p)
                    if p.is_file() and p.stat().st_size < hermes_data.BLOB_MAX:
                        out[sha] = p.read_bytes()
                        break
    return out


def _state(src_path: Path, dst_path: Path, start: float, named: set[str]) -> dict[str, int]:
    """Copy into a new state.db the sessions active from `start` on, plus the `named` ones, with
    all their messages; returns the row counts."""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        schema = query(
            src,
            "select type, sql from sqlite_master where tbl_name in (?, ?) and sql is not null"
            " and type in ('table', 'index') order by type = 'index', name",
            TABLES,
        )
    finally:
        src.close()
    if sum(r["type"] == "table" for r in schema) != len(TABLES):
        raise FixtureError(f"{src_path}: no sessions/messages tables")
    dst = sqlite3.connect(str(dst_path), uri=True)
    try:
        for r in schema:
            sql = r["sql"]
            if isinstance(sql, str):
                dst.execute(sql)
        dst.execute("attach database ? as src", (f"file:{src_path}?mode=ro",))
        dst.execute("create temp table keep (id text primary key)")
        dst.execute(
            "insert or ignore into keep select s.id from src.sessions s where s.started_at >= ?"
            " or exists (select 1 from src.messages m where m.session_id = s.id"
            " and m.timestamp >= ?)",
            (start, start),
        )
        dst.executemany(
            "insert or ignore into keep select id from src.sessions where id = ?",
            [(sid,) for sid in sorted(named)],
        )
        dst.execute(
            "insert into main.sessions select * from src.sessions where id in (select id from keep)"
            " order by started_at, id"
        )
        dst.execute(
            "insert into main.messages select * from src.messages"
            " where session_id in (select id from keep) order by id"
        )
        dst.commit()
        dst.execute("detach database src")
        counts = {t: count(dst, f"select count(*) from main.{t}") for t in TABLES}
        dst.execute("vacuum")
    finally:
        dst.close()
    return counts


def _skills(home: Path) -> dict[str, bytes]:
    """`skills/<path>/SKILL.md` -> content, hidden directories excluded (store.skill_files)."""
    base = home / "skills"
    out: dict[str, bytes] = {}
    if base.is_dir():
        for p in sorted(base.rglob("SKILL.md")):
            rel = p.relative_to(base)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if _inside(home, p).is_file():
                out[f"skills/{rel.as_posix()}"] = p.read_bytes()
    return out


def _pack(out: Path, files: dict[str, bytes]) -> None:
    """A tar.xz of `files` with fixed metadata, members sorted: the same bytes for the same
    files."""
    tmp = out.with_name(out.name + ".tmp")
    with tarfile.open(tmp, "w:xz") as tf:
        for name in sorted(files):
            data = files[name]
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tf.addfile(info, io.BytesIO(data))
    tmp.replace(out)


def build(
    home: Path, out: Path = DEFAULT, since_days: float | None = None, now: float | None = None
) -> Manifest:
    """Write the fixture of the Hermes home `home` to `out` (see the module doc)."""
    home = Path(home)
    _refuse(home)
    now = time.time() if now is None else now
    since_days, start = _window(home, since_days, now)
    lines, entries = _ledger(home, start)
    blobs = _blobs(home, entries)
    named = {sid for e in entries if (sid := (e.get("evidence") or {}).get("session_id"))}
    skills = _skills(home)
    with tempfile.TemporaryDirectory(prefix="argus-fixture-") as tmp:
        db = Path(tmp) / "state.db"
        counts = _state(home / "state.db", db, start, named)
        state = db.read_bytes()
    counts |= {"ledger": len(lines), "blobs": len(blobs), "skills": len(skills)}
    manifest = Manifest(
        format=FORMAT, now=now, since_days=since_days, window_start=start, counts=counts
    )
    files = {
        "manifest.json": (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode(),
        "home/state.db": state,
        "home/skills/.curator_ledger.jsonl": "".join(lines).encode("utf-8"),
        **{f"home/.curator_backups/blobs/{sha}": data for sha, data in blobs.items()},
        **{f"home/{name}": data for name, data in skills.items()},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    _pack(out, files)
    return manifest


def unpack(fixture: Path, dest: Path) -> Manifest:
    """Write the fixture's `home/` into `dest` (new or empty); returns its manifest. Only plain
    files under `home/` are written, never outside `dest`."""
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise FixtureError(f"{dest} exists and is not an empty directory")
    manifest: Manifest | None = None
    with tarfile.open(fixture, "r:*") as tf:
        for m in tf.getmembers():
            name = PurePosixPath(m.name)
            f = tf.extractfile(m) if m.isfile() else None
            if f is None or name.is_absolute() or ".." in name.parts:
                raise FixtureError(f"{fixture}: unexpected member {m.name!r}")
            data = f.read()
            if m.name == "manifest.json":
                manifest = MANIFEST.validate_json(data, strict=True)
            elif name.parts[0] == "home" and len(name.parts) > 1:
                target = dest.joinpath(*name.parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            else:
                raise FixtureError(f"{fixture}: unexpected member {m.name!r}")
    if manifest is None or manifest["format"] != FORMAT:
        raise FixtureError(f"{fixture}: no manifest of format {FORMAT}")
    return manifest


def prepare(
    fixture: Path, dest: Path, log: Callable[[str], object] = _quiet
) -> tuple[Manifest, backfill.ProfileReport]:
    """Unpack the fixture into `dest` and backfill it with the manifest's window at its frozen
    `now`: a home derive can run on (in-process, or `hermes argus derive` with HERMES_HOME=dest)."""
    manifest = unpack(fixture, dest)
    report = backfill.run(dest, since_days=manifest["since_days"], now=manifest["now"], log=log)
    return manifest, report["default"]


class Args(argparse.Namespace):
    cmd: str
    home: Path
    since_days: float | None
    out: Path
    dest: Path
    fixture: Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bench.fixture", description="the bench fixture (bench/README.md)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build the fixture from a Hermes home (the demo's)")
    b.add_argument("--home", required=True, type=Path, help="the Hermes home to read (required)")
    b.add_argument("--since-days", type=float, help="window: the last N days (default: the home's)")
    b.add_argument("--out", type=Path, default=DEFAULT, help=f"output (default: {DEFAULT})")
    p = sub.add_parser("prepare", help="unpack the fixture into a scratch HERMES_HOME, backfilled")
    p.add_argument("dest", type=Path, help="new or empty directory")
    p.add_argument("--fixture", type=Path, default=DEFAULT, help=f"(default: {DEFAULT})")
    args = parser.parse_args(argv, namespace=Args())
    try:
        if args.cmd == "build":
            m = build(args.home, args.out, args.since_days)
            print(f"wrote {args.out}: {json.dumps(m['counts'])}, {m['since_days']:g} days")
        else:
            m, rep = prepare(args.fixture, args.dest, log=print)
            print(f"prepared {args.dest}: {json.dumps(rep)}")
    except FixtureError as e:
        print(f"bench.fixture: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
