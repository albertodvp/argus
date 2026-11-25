"""`just bench`: derive on the bench fixture with every model call answered from the bench cassette
(strict: a call it does not hold fails), then the headline numbers compared with
bench/expected.json. No model and no API key; exit 1 on any difference or miss, 2 when an input is
missing.

    uv run python -m bench.check [--fixture F] [--cassette C] [--expected E] [--write-expected]

It runs the code of `hermes argus derive --replay` (cli.run_replay: catch-up backfill, derive, the
cassette's strict check) on a scratch HERMES_HOME that `bench.fixture.prepare` unpacks and
backfills at the fixture's frozen time. --write-expected writes the numbers instead of comparing
them, after a clean replay only.

The numbers: live intents (as the dashboard counts them), proposals written and proposals dropped
for want of verbatim quotes (the derive run report's `unquoted`), skill edits by evidence label
(`unlabelled`: not reviewed, e.g. a person's edit or one without a diff) and the share of
speculative ones among those judged on their run, runs by signal status, and the model calls
replayed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Final

from typing_extensions import TypedDict

from argus import cassette, cli, derive, store
from argus.dbtypes import count, query
from argus.jsontypes import JsonObject, loads_object

from . import fixture

HERE: Final = Path(__file__).parent
CASSETTE: Final = HERE / "cassette.jsonl"
EXPECTED: Final = HERE / "expected.json"


class Numbers(TypedDict):
    intents: int
    proposals: int
    proposals_unquoted: int  # dropped: too few intents quoted verbatim, no frequent error pattern
    skill_edits: dict[str, int]
    speculative_pct: float  # speculative / edits judged on their run (derive.EVIDENCE), in %
    runs: dict[str, int]
    model_calls: int


def _counts(conn: sqlite3.Connection, sql: str) -> dict[str, int]:
    """`select label, n ...` as {label: n}."""
    out: dict[str, int] = {}
    for r in query(conn, sql):
        label, n = r["label"], r["n"]
        if not isinstance(label, str) or not isinstance(n, int):
            raise TypeError(f"unexpected row {r!r}")
        out[label] = n
    return out


def unquoted(home: Path) -> int:
    """Proposals the derive passes of the profile at `home` dropped as unquoted, from their run
    reports (`runs/<utc>.json`, stage `proposals`)."""
    n = 0
    for f in sorted((store.data_dir(home) / "runs").glob("*.json")):
        profiles = loads_object(f.read_text(encoding="utf-8")).get("profiles")
        for rep in profiles.values() if isinstance(profiles, dict) else []:
            stages = rep.get("stages") if isinstance(rep, dict) else None
            stage = stages.get("proposals") if isinstance(stages, dict) else None
            dropped = stage.get("unquoted") if isinstance(stage, dict) else None
            n += dropped if isinstance(dropped, int) else 0
    return n


def numbers(home: Path, model_calls: int) -> Numbers:
    """The headline numbers of the profile at `home` after a derive."""
    conn = store.open_db(home)
    try:
        edits = _counts(
            conn,
            "select coalesce(evidence, 'unlabelled') label, count(*) n from skill_edit"
            " group by 1 order by 1",
        )
        judged = sum(edits.get(e, 0) for e in derive.EVIDENCE)
        return Numbers(
            intents=store.live_intents(conn),
            proposals=count(conn, "select count(*) from proposal"),
            proposals_unquoted=unquoted(home),
            skill_edits=edits,
            speculative_pct=round(100 * edits.get("speculative", 0) / judged, 1) if judged else 0.0,
            runs=_counts(
                conn,
                "select coalesce(signal_status, 'none') label, count(*) n from run"
                " group by 1 order by 1",
            ),
            model_calls=model_calls,
        )
    finally:
        conn.close()


def replay(fixture_path: Path, cassette_path: Path) -> tuple[int, Numbers, cassette.Replayer]:
    """Prepare the fixture in a scratch home and derive on it from the cassette (strict): (exit
    code of the derive, the numbers, the replayer)."""
    tape = cassette.Replayer.load(cassette_path, strict=True)
    with tempfile.TemporaryDirectory(prefix="argus-bench-") as tmp:
        home = Path(tmp) / "home"
        fixture.prepare(fixture_path, home)
        code = cli.run_replay(tape, home, None, None, quiet=True)
        return code, numbers(home, tape.replayed), tape


def differences(got: JsonObject, want: JsonObject) -> list[str]:
    """One line per headline number that differs (key: expected -> got)."""
    return [
        f"{k}: expected {json.dumps(want.get(k))}, got {json.dumps(got.get(k))}"
        for k in sorted(set(got) | set(want))
        if got.get(k) != want.get(k)
    ]


class Args(argparse.Namespace):
    fixture: Path
    cassette: Path
    expected: Path
    write_expected: bool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bench.check", description="replay the bench and compare its numbers"
    )
    parser.add_argument("--fixture", type=Path, default=fixture.DEFAULT)
    parser.add_argument("--cassette", type=Path, default=CASSETTE)
    parser.add_argument("--expected", type=Path, default=EXPECTED)
    parser.add_argument(
        "--write-expected", action="store_true", help="write the numbers instead of comparing"
    )
    args = parser.parse_args(argv, namespace=Args())
    inputs = [args.fixture, args.cassette] + ([] if args.write_expected else [args.expected])
    if missing := [str(p) for p in inputs if not p.is_file()]:
        print(
            f"bench: missing {', '.join(missing)}: see bench/README.md to build and record them",
            file=sys.stderr,
        )
        return 2
    try:
        code, got, tape = replay(args.fixture, args.cassette)
    except (OSError, ValueError, fixture.FixtureError) as e:
        print(f"bench: {e}", file=sys.stderr)
        return 2
    print(f"bench: {tape.summary()}")
    print(json.dumps(got, indent=1, sort_keys=True))
    if code != 0 or tape.missed:
        print(f"bench: the replay failed (exit {code}, {len(tape.missed)} missed)", file=sys.stderr)
        return 1
    got_json = loads_object(json.dumps(got))
    if args.write_expected:
        args.expected.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n", "utf-8")
        print(f"bench: wrote {args.expected}")
        return 0
    if diff := differences(got_json, loads_object(args.expected.read_text(encoding="utf-8"))):
        print("bench: numbers differ from " + str(args.expected), file=sys.stderr)
        for line in diff:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"bench: numbers match {args.expected}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
