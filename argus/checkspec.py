"""The check format of enforced rules (docs/enforce.md, section 2): parse, canonical text, sha256,
and the match of one tool call. Pure: no database, no Hermes, no model.

A check is a closed JSON object, version 1:

    {"v": 1, "tool": T, "match": "tool"}
    {"v": 1, "tool": T, "field": F, "match": "equals", "value": V}
    {"v": 1, "tool": T, "field": F, "match": "path", "under": DIR}
    {"v": 1, "tool": T, "field": F, "match": "command", "program": P, "args": [...], "flags": [...]}

`evaluate(spec, args)` answers `("no", "")`, `("match", what matched)` or `("unknown", why)`: an
argument Argus cannot read is `unknown`, which the hook treats as a match (what it cannot read is
denied). It may raise on a bug or an OS error; the hook decides what that means (section 8).
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal, NotRequired, TypeAlias

from typing_extensions import TypedDict

from . import shellwords
from .jsontypes import JsonObject, as_object, loads

__all__ = [
    "KINDS",
    "SHOWN",
    "VERSION",
    "Kind",
    "Spec",
    "SpecError",
    "Verdict",
    "canonical",
    "describe",
    "digest",
    "evaluate",
    "load",
    "parse_spec",
]

Kind: TypeAlias = Literal["tool", "equals", "path", "command"]
KINDS: Final[tuple[Kind, ...]] = ("tool", "equals", "path", "command")
VERSION: Final = 1

Verdict: TypeAlias = tuple[Literal["no", "match", "unknown"], str]


class Spec(TypedDict):
    """One parsed check; the fields of its kind only (`args`/`flags` always for `command`)."""

    v: int
    tool: str
    match: Kind
    field: NotRequired[str]
    value: NotRequired[str]
    under: NotRequired[str]
    program: NotRequired[str]
    args: NotRequired[list[str]]
    flags: NotRequired[list[str]]


class SpecError(ValueError):
    """Not a check of the closed format (the reason is the message)."""


_KEYS: Final[dict[Kind, frozenset[str]]] = {
    "tool": frozenset({"v", "tool", "match"}),
    "equals": frozenset({"v", "tool", "match", "field", "value"}),
    "path": frozenset({"v", "tool", "match", "field", "under"}),
    "command": frozenset({"v", "tool", "match", "field", "program", "args", "flags"}),
}
_OPTIONAL: Final = frozenset({"args", "flags"})
_TOOL: Final = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")
_FIELD: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_PROGRAM: Final = re.compile(r"^[^/\s\x00]{1,128}$")
_WORD: Final = re.compile(r"^[^\s\x00]{1,128}$")
_FLAG: Final = re.compile(r"^--?[^\s=\x00-][^\s=\x00]{0,62}$")
MAX_VALUE: Final = 500
MAX_PATH: Final = 1024
MAX_ARGS: Final = 8
MAX_FLAGS: Final = 16
SHOWN: Final = 300  # characters of a matched value kept in an event or a message


# ---------------------------------------------------------------- parse
def _str(obj: JsonObject, key: str, limit: int) -> str:
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        raise SpecError(f"{key} must be a non-empty string")
    if len(v) > limit or "\x00" in v:
        raise SpecError(f"{key} is longer than {limit} characters or holds a NUL byte")
    return v.strip()


def _strs(obj: JsonObject, key: str, most: int, pattern: re.Pattern[str]) -> list[str]:
    v = obj.get(key, [])
    if not isinstance(v, list) or len(v) > most:
        raise SpecError(f"{key} must be a list of at most {most} strings")
    out: list[str] = []
    for item in v:
        if not isinstance(item, str) or not pattern.match(item):
            raise SpecError(f"{key}: {item!r} is not allowed")
        out.append(item)
    return out


def _kind(obj: JsonObject) -> Kind:
    m = obj.get("match")
    for k in KINDS:
        if m == k:
            return k
    raise SpecError(f"match must be one of {', '.join(KINDS)}")


def _under(raw: str) -> str:
    if not (raw == "~" or raw.startswith(("/", "~/"))):
        raise SpecError("under must be an absolute path or start with ~/")
    p = posixpath.normpath(raw)
    return "/" + p.lstrip("/") if p.startswith("/") else p


def parse_spec(raw: object) -> Spec:
    """`raw` (a JSON object) as a Spec, normalised; SpecError when it is not one."""
    obj = as_object(raw)
    if obj is None:
        raise SpecError("a check must be a JSON object")
    v = obj.get("v")
    if not isinstance(v, int) or isinstance(v, bool) or v != VERSION:
        raise SpecError(f"v must be {VERSION}")
    kind = _kind(obj)
    if extra := set(obj) - _KEYS[kind]:
        raise SpecError(f"unknown key(s) for match {kind}: {', '.join(sorted(extra))}")
    if missing := _KEYS[kind] - _OPTIONAL - set(obj):
        raise SpecError(f"missing key(s): {', '.join(sorted(missing))}")
    tool = _str(obj, "tool", 128)
    if not _TOOL.match(tool):
        raise SpecError(f"tool {tool!r} is not a tool name")
    spec = Spec(v=VERSION, tool=tool, match=kind)
    if kind == "tool":
        return spec
    field = _str(obj, "field", 64)
    if not _FIELD.match(field):
        raise SpecError(f"field {field!r} is not an argument name")
    spec["field"] = field
    if kind == "equals":
        spec["value"] = _str(obj, "value", MAX_VALUE)
    elif kind == "path":
        spec["under"] = _under(_str(obj, "under", MAX_PATH))
    else:
        program = _str(obj, "program", 128)
        if not _PROGRAM.match(program):
            raise SpecError("program must be a program name (no / or whitespace)")
        spec["program"] = program
        spec["args"] = _strs(obj, "args", MAX_ARGS, _WORD)
        spec["flags"] = _strs(obj, "flags", MAX_FLAGS, _FLAG)
    return spec


def canonical(spec: Spec) -> str:
    """The text a check is stored, hashed and approved as."""
    return json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(text: str) -> str:
    """sha256 (hex) of a check's canonical text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load(text: str) -> Spec:
    """A stored check's text as a Spec; SpecError when it does not parse."""
    try:
        raw = loads(text)
    except ValueError as e:
        raise SpecError(f"not JSON: {e}") from e
    return parse_spec(raw)


def describe(spec: Spec) -> str:
    """One line for a person: what the check matches."""
    at = f"{spec['tool']}.{spec.get('field', '')}"
    kind = spec["match"]
    if kind == "tool":
        return f"any {spec['tool']} call"
    if kind == "equals":
        return f"{at} equals {spec.get('value', '')!r}"
    if kind == "path":
        return f"{at} is under {spec.get('under', '')}"
    words = " ".join([spec.get("program", ""), *spec.get("args", [])])
    flags = spec.get("flags", [])
    return f"{at} runs `{words}`" + (f" with {'|'.join(flags)}" if flags else "")


# ---------------------------------------------------------------- match
def _shown(text: str) -> str:
    return text if len(text) <= SHOWN else text[: SHOWN - 1] + "…"


def _expand(path: str) -> str:
    """`~` and `~user` expanded as this process sees them; unchanged when they cannot be."""
    if not path.startswith("~"):
        return path
    try:
        return str(Path(path).expanduser())
    except RuntimeError:  # no home directory for that user
        return path


def _forms(path: str) -> set[str]:
    """An absolute path as written (normalised) and as the filesystem resolves it (symlinks and
    `..` in order, from the path as written: `link/..` is the link target's parent); empty for a
    path that is not absolute once `~` is expanded."""
    expanded = _expand(path)
    if not expanded.startswith("/"):
        return set()
    lexical = "/" + posixpath.normpath(expanded).lstrip("/")
    return {lexical, os.path.realpath(expanded)}


def _inside(path: str, base: str) -> bool:
    return base == "/" or path == base or path.startswith(base.rstrip("/") + "/")


def _path(under: str, value: str) -> Verdict:
    expanded = _expand(value.strip())
    if not expanded.startswith("/"):
        return ("unknown", f"relative path {_shown(value)!r}: use an absolute path")
    bases = _forms(under)
    if not bases:
        return ("unknown", f"the check's directory {under} cannot be expanded here")
    for p in _forms(expanded):
        for b in bases:
            if _inside(p, b):
                return ("match", _shown(value))
    return ("no", "")


def _flag_hit(word: str, flag: str) -> bool:
    if word == flag:
        return True
    if flag.startswith("--"):
        # getopt_long takes an unambiguous prefix of a long option: --forc for --force
        name = word.split("=", 1)[0]
        return name.startswith("--") and len(name) > 3 and flag.startswith(name)
    # a one-letter short flag also counts inside a cluster: -f in -fu
    return (
        len(flag) == 2
        and word.startswith("-")
        and not word.startswith("--")
        and flag[1] in word[1:]
    )


def _one(spec: Spec, words: list[shellwords.Word]) -> Verdict:
    prog, rest = words[0], words[1:]
    shown = _shown(" ".join(w.text for w in words))
    if prog.dynamic:
        return ("unknown", f"the program of `{shown}` is an expansion")
    if prog.text.rsplit("/", 1)[-1] != spec.get("program"):
        return ("no", "")
    dynamic = any(w.dynamic for w in rest)
    need = spec.get("args", [])
    k = 0
    for w in rest:
        if k < len(need) and not w.dynamic and w.text == need[k]:
            k += 1
    if k < len(need):
        return ("unknown", f"`{shown}` holds an expansion") if dynamic else ("no", "")
    flags = spec.get("flags", [])
    if not flags or any(_flag_hit(w.text, f) for w in rest if not w.dynamic for f in flags):
        return ("match", shown)
    return ("unknown", f"`{shown}` holds an expansion") if dynamic else ("no", "")


def _command(spec: Spec, text: str) -> Verdict:
    try:
        parsed = shellwords.parse(text)
    except shellwords.UnparsableError as e:
        return ("unknown", f"cannot parse the command ({e})")
    maybe = parsed.opaque[0] if parsed.opaque else ""
    for words in parsed.commands:
        verdict = _one(spec, words)
        if verdict[0] == "match":
            return verdict
        if verdict[0] == "unknown" and not maybe:
            maybe = verdict[1]
    return ("unknown", maybe) if maybe else ("no", "")


def evaluate(spec: Spec, args: Mapping[str, object]) -> Verdict:
    """Whether a call of `spec["tool"]` with `args` matches the check (the caller has checked the
    tool name)."""
    kind = spec["match"]
    if kind == "tool":
        return ("match", f"a {spec['tool']} call")
    field = spec.get("field", "")
    value = args.get(field)
    if not isinstance(value, str):
        what = "missing" if value is None else f"a {type(value).__name__}, not a string"
        return ("unknown", f"argument {field} is {what}")
    if "\x00" in value:
        return ("unknown", f"argument {field} holds a NUL byte")
    if kind == "equals":
        hit = value.strip().casefold() == spec.get("value", "").strip().casefold()
        return ("match", _shown(value)) if hit else ("no", "")
    if kind == "path":
        return _path(spec.get("under", ""), value)
    return _command(spec, value)
