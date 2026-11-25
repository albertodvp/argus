"""A cassette records derive's model calls by prompt hash and replays them: answers in recorded
order per prompt, failures as failures, a prompt it does not hold as a miss that fails a strict
run; `hermes argus derive --replay` constructs no model."""

import argparse
import time
from pathlib import Path

import pytest
from test_window import make_home, quiet

from argus import backfill, cassette, cli, derive
from argus.jsontypes import JsonObject, loads_object

SCHEMA: JsonObject = {"type": "object", "properties": {"x": {"type": "string"}}}


class Counting:
    """Answers {"n": <how many calls so far>}; raises for purpose `boom`, None for `none` and a list
    (not an object) for `list`."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self, instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
        self.n += 1
        if purpose == "boom":
            raise ConnectionError("provider unreachable")
        if purpose == "none":
            return None
        if purpose == "list":
            return [1, 2]
        return {"n": self.n}


def test_record_then_replay_round_trip_without_prompt_text(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    rec = cassette.Recorder(Counting(), path)
    asked = [("sys", f"secret prompt {i}", "argus.judge") for i in range(3)]
    answers = [rec(i, t, SCHEMA, p) for i, t, p in asked]
    text = path.read_text(encoding="utf-8")
    assert "secret prompt" not in text
    assert "sys" not in text
    lines = [loads_object(x) for x in text.splitlines()]
    assert [sorted(x) for x in lines] == [["answer", "key", "purpose"]] * 3
    tape = cassette.Replayer.load(path)
    assert [tape(i, t, SCHEMA, p) for i, t, p in reversed(asked)] == answers[::-1]
    assert (tape.replayed, tape.missed, tape.unused(), tape.failure()) == (3, [], 0, None)


def test_the_key_covers_purpose_instructions_text_and_schema() -> None:
    base = cassette.call_key("i", "t", SCHEMA, "p")
    assert base == cassette.call_key("i", "t", dict(reversed(SCHEMA.items())), "p")
    others = {
        cassette.call_key("I", "t", SCHEMA, "p"),
        cassette.call_key("i", "T", SCHEMA, "p"),
        cassette.call_key("i", "t", {"type": "object"}, "p"),
        cassette.call_key("i", "t", SCHEMA, "q"),
    }
    assert base not in others
    assert len(others) == 4


def test_a_miss_raises_and_fails_a_strict_run(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    cassette.Recorder(Counting(), path)("i", "t", SCHEMA, "argus.judge")
    strict = cassette.Replayer.load(path)
    with pytest.raises(cassette.CassetteMissError, match=r"argus\.assign \(key [0-9a-f]{12}\)"):
        strict("i", "other text", SCHEMA, "argus.assign")
    key = cassette.call_key("i", "other text", SCHEMA, "argus.assign")
    assert strict.missed == [("argus.assign", key)]
    why = strict.failure()
    assert why is not None
    assert f"argus.assign {key[:12]}" in why


def test_a_lenient_miss_is_a_failed_call_that_never_stops_the_run(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    path.write_text("", encoding="utf-8")
    lenient = cassette.Replayer.load(path, strict=False)
    calls = derive.Calls(lenient, quiet)
    many = 2 * derive.ABORT_AFTER  # more misses in a row than a raising model may make
    for n in range(many):
        assert calls("i", f"text {n}", SCHEMA, "argus.assign") is None
    assert (calls.calls, calls.failures, calls.raised_in_row) == (many, many, 0)
    assert len(lenient.missed) == many
    assert lenient.failure() is None


def test_one_prompt_asked_twice_replays_in_recorded_order(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    rec = cassette.Recorder(Counting(), path)
    first, second = rec("i", "t", SCHEMA, "p"), rec("i", "t", SCHEMA, "p")
    assert first != second
    tape = cassette.Replayer.load(path)
    assert tape("i", "t", SCHEMA, "p") == first
    assert tape("i", "t", SCHEMA, "p") == second
    with pytest.raises(cassette.CassetteMissError):
        tape("i", "t", SCHEMA, "p")


def test_failed_calls_replay_as_failures(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    rec = cassette.Recorder(Counting(), path)
    with pytest.raises(ConnectionError):
        rec("i", "t", SCHEMA, "boom")
    assert rec("i", "t", SCHEMA, "none") is None
    assert rec("i", "t", SCHEMA, "list") == [1, 2]
    lines = [loads_object(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["error"] == "ConnectionError: provider unreachable"
    assert [x.get("answer", "absent") for x in lines] == ["absent", None, None]
    tape = cassette.Replayer.load(path)
    with pytest.raises(cassette.RecordedCallError, match="provider unreachable"):
        tape("i", "t", SCHEMA, "boom")
    assert tape("i", "t", SCHEMA, "none") is None
    assert tape("i", "t", SCHEMA, "list") is None  # derive reads anything but an object as none
    assert (tape.replayed, tape.missed) == (3, [])
    # derive counts them as it counted the recorded calls
    for llm in (Counting(), cassette.Replayer.load(path)):
        calls = derive.Calls(llm, quiet)
        for p in ("boom", "none", "list"):
            assert calls("i", "t", SCHEMA, p) is None
        assert (calls.calls, calls.failures, calls.raised_in_row) == (3, 3, 0)


def test_a_malformed_cassette_names_the_line(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    path.write_text('{"key": "k", "purpose": "p", "answer": null}\n{"key": 1}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"c\.jsonl:2: not a cassette entry"):
        cassette.Replayer.load(path)


class NoModelCtx:
    """A plugin context whose model must not be touched."""

    @property
    def llm(self) -> object:
        raise AssertionError("--replay constructed a model")

    def get_config(self, key: str, default: object = None) -> object:
        return default


def _derive_args(**kw: object) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    ns = parser.parse_args(["derive", "--quiet"])
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_record_and_replay_are_exclusive() -> None:
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["derive", "--record", "a", "--replay", "b"])


def test_cli_replay_uses_no_model_and_a_miss_fails_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "hermes"
    make_home(home, time.time())
    backfill.run(home, since_days=30, log=quiet)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    ctx = NoModelCtx()
    code = cli.derive_cmd(ctx, _derive_args(replay=str(empty)), home, home)  # type: ignore[arg-type]
    assert code == 1
    err = capsys.readouterr().err
    assert "replay failed: 3 model call(s) not in" in err
    assert "argus.assign " in err
    lenient = _derive_args(replay=str(empty), lenient=True)
    assert cli.derive_cmd(ctx, lenient, home, home) == 0  # type: ignore[arg-type]
    assert "--lenient needs --replay" not in capsys.readouterr().err
    assert cli.derive_cmd(ctx, _derive_args(lenient=True), home, home) == 2  # type: ignore[arg-type]
    assert "--lenient needs --replay" in capsys.readouterr().err
    missing = _derive_args(replay=str(tmp_path / "nope.jsonl"))
    assert cli.derive_cmd(ctx, missing, home, home) == 2  # type: ignore[arg-type]
