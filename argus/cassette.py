"""Record derive's model calls into a cassette and replay them, so a derive runs again without a
model (`hermes argus derive --record PATH | --replay PATH`, and `just bench`).

A cassette is a JSONL file, one line per call in call order:

    {"key": "<sha256>", "purpose": "argus.assign", "answer": {...}}    the answer (null: none)
    {"key": "<sha256>", "purpose": "argus.judge", "error": "TimeoutError: ..."}    the call raised

`key` is the sha256 of the canonical JSON of [purpose, instructions, text, schema]: the prompt
itself is not stored (cassettes stay small and hold no conversation text), so the same prompt finds
its answer and a changed one finds none. The answer is stored as derive reads it (a JSON object, or
null for anything else), so a replayed call is counted exactly as the recorded one was.

Replay hands out each key's recorded outcomes in recorded order (one prompt asked twice gets its
two answers in turn); a recorded failure fails again, as an exception or as no answer like when it
was recorded. A prompt with no outcome left is a miss, counted: in strict mode it raises
CassetteMissError and the run fails afterwards (`Replayer.failure`) naming each missed purpose and
key prefix; in lenient mode it is a failed call (no answer), never an exception, so misses in a row
do not stop the run as a provider that keeps raising would.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from pathlib import Path
from typing import Final, NotRequired

from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypedDict

from .hermes_api import LlmFn
from .jsontypes import JsonObject, JsonValue, as_object

KEY_SHOWN: Final = 12  # key prefix in messages


class Entry(TypedDict):
    """One cassette line: an answer (`answer`, null for none) or a raised call (`error`)."""

    key: str
    purpose: str
    answer: NotRequired[JsonObject | None]
    error: NotRequired[str]


ENTRY: Final = TypeAdapter(Entry)


class CassetteMissError(Exception):
    """A replayed call whose prompt the cassette holds no (further) answer for."""

    def __init__(self, purpose: str, key: str) -> None:
        super().__init__(f"cassette miss: no recorded answer for {purpose} (key {key[:KEY_SHOWN]})")
        self.purpose, self.key = purpose, key


class RecordedCallError(Exception):
    """A call that raised when it was recorded raises this when replayed."""


def call_key(instructions: str, text: str, schema: JsonObject, purpose: str) -> str:
    """sha256 of the canonical JSON of [purpose, instructions, text, schema]."""
    parts: list[JsonValue] = [purpose, instructions, text, schema]
    canon = json.dumps(
        parts,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def dump_entry(e: Entry) -> str:
    return json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n"


class Recorder:
    """An LlmFn that calls `llm` and appends each call's outcome to the cassette at `path`
    (truncated first: one cassette is one run). Returns or raises what `llm` does."""

    def __init__(self, llm: LlmFn, path: Path) -> None:
        self.llm, self.path, self.calls = llm, Path(path), 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def _append(self, e: Entry) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(dump_entry(e))

    def __call__(self, instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
        key = call_key(instructions, text, schema, purpose)
        self.calls += 1
        try:
            answer = self.llm(instructions, text, schema, purpose)
        except Exception as e:
            self._append(Entry(key=key, purpose=purpose, error=f"{type(e).__name__}: {e}"))
            raise
        self._append(Entry(key=key, purpose=purpose, answer=as_object(answer)))
        return answer


class Replayer:
    """An LlmFn answering from a cassette (see the module doc). `replayed` counts the calls
    answered from it (recorded failures included), `missed` the (purpose, key) of each miss; a miss
    raises CassetteMissError when `strict`, else answers None."""

    def __init__(self, entries: list[Entry], strict: bool = True, source: str = "cassette") -> None:
        self.strict, self.source = strict, source
        self.queues: dict[str, deque[Entry]] = {}
        for e in entries:
            self.queues.setdefault(e["key"], deque()).append(e)
        self.recorded = len(entries)
        self.replayed = 0
        self.missed: list[tuple[str, str]] = []

    @classmethod
    def load(cls, path: Path, strict: bool = True) -> Replayer:
        """The cassette at `path`; raises OSError when unreadable, ValueError naming the line
        that is not a cassette entry."""
        entries: list[Entry] = []
        with Path(path).open(encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    raw: object = json.loads(line)
                    entries.append(ENTRY.validate_python(raw, strict=True))
                except (ValueError, ValidationError) as e:
                    raise ValueError(f"{path}:{n}: not a cassette entry: {e}") from e
        return cls(entries, strict=strict, source=str(path))

    def __call__(self, instructions: str, text: str, schema: JsonObject, purpose: str, /) -> object:
        key = call_key(instructions, text, schema, purpose)
        q = self.queues.get(key)
        if not q:
            self.missed.append((purpose, key))
            if not self.strict:
                return None
            raise CassetteMissError(purpose, key)
        e = q.popleft()
        self.replayed += 1
        if "error" in e:
            raise RecordedCallError(e["error"])
        return e.get("answer")

    def unused(self) -> int:
        """Recorded calls this replay has not asked for (yet)."""
        return sum(len(q) for q in self.queues.values())

    def summary(self) -> str:
        return (
            f"replayed {self.replayed} of {self.recorded} recorded model calls from {self.source}"
            f", {len(self.missed)} missed"
        )

    def failure(self) -> str | None:
        """In strict mode, the run's failure when a call missed: each missed purpose and key
        prefix; None otherwise."""
        if not self.strict or not self.missed:
            return None
        shown = ", ".join(f"{p} {k[:KEY_SHOWN]}" for p, k in self.missed[:10])
        more = f" and {len(self.missed) - 10} more" if len(self.missed) > 10 else ""
        return (
            f"{len(self.missed)} model call(s) not in {self.source}: {shown}{more}; the prompts"
            " changed since the recording (inputs, prompts.py or derive), record it again"
        )
