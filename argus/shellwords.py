"""Shell text as the simple commands it runs, for the `command` match of an enforced check
(docs/enforce.md, section 2).

The parser understands an explicit, small allowlist of shell syntax and nothing else. What it
models:

- words: unquoted characters, `\\x` escapes, '...' and "..." quotes (in "...", the escapes `\\"`,
  `\\\\`, `\\$`, `\\``), and the expansions `$NAME`, `${NAME}` and `$1`, `$@`, `$?`... (the word
  is then *dynamic*: its text is not what the shell passes on), an unquoted glob or brace pair
  (dynamic too), `#` comments;
- operators: `;`, `&`, `&&`, `|`, `||`, `|&`, `;;`, newlines, `(`/`)` as grouping, `NAME()`
  function definitions, and the redirections `<`, `>`, `>>`, `>|`, `<>`, `&>`, `&>>`, `>&`, `<&`
  (with a file descriptor before them), which are dropped with their target;
- per simple command where the shell reads one: leading keywords (`if`, `then`, `do`, `!`,
  `{`, ...), `VAR=value` assignments and `function NAME`, each only unquoted as in bash;
  `for`/`select`/`case` headers run nothing;
- wrappers with every option they take listed (`sudo`, `doas`, `env`, `nohup`, `setsid`, `nice`,
  `ionice`, `timeout`, `time`, `command`, `builtin`, `exec`, `stdbuf`, `chrt`, `taskset`,
  `unbuffer`, `busybox`, `xargs`), shells (`sh -c TEXT`, `bash -c TEXT`, ...), `eval` of static
  words and `find -exec/-execdir/-ok/-okdir PROGRAM ... ;` (or `{} +`), each parsed again
  (MAX_DEPTH levels); a wrapper's or find's command is a program and its arguments, with no
  shell syntax (but NAME=value for env and sudo, and `time` read both ways). Every program that
  runs is listed in `Parsed.commands`, wrappers and shells too.

Everything else is outside the model. A lexical construct outside it (command, process or
arithmetic substitution, backticks, `$'...'`, `$"..."`, `${...}` other than `${NAME}`, here-
documents, a `(` inside a word, `((...))`, the case fall-throughs `;&` and `;;&`, unbalanced
quotes, a NUL byte, nesting deeper than MAX_DEPTH)
raises UnparsableError: the whole text is opaque. A command outside it (a dynamic program word, a
wrapper option not listed, `env -S`, `xargs -I`, `sudo -s`, find's `{}` or an expansion in a place
that decides what runs, a shell reading its script from stdin, `eval`/`source` of an expansion,
an interpreter not run as `INTERPRETER SCRIPT ...` or `python -m MODULE ...` (inline code,
options, stdin), a program that runs a command Argus does not parse) is listed in
`Parsed.opaque`. The caller treats any opaque part as "cannot verify".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

__all__ = ["MAX_DEPTH", "Parsed", "UnparsableError", "Word", "parse"]

MAX_DEPTH: Final = 3


class UnparsableError(ValueError):
    """The text is not shell Argus can read (the reason is the message)."""


@dataclass(frozen=True)
class Word:
    """One shell word: its text with quotes and escapes removed; `dynamic` when it holds an
    expansion, so the text is not what the shell passes on (it may even become several words)."""

    text: str
    dynamic: bool = False
    quoted_from: int | None = None  # where quoting or an escape starts in `text`; None: none


@dataclass
class Parsed:
    """The simple commands of a shell text Argus can see (each a non-empty list of words, the
    program first, static), and why the other parts are opaque."""

    commands: list[list[Word]] = field(default_factory=list)
    opaque: list[str] = field(default_factory=list)


_OPERATORS: Final = (
    "&>>",
    "&&",
    "||",
    ";;",
    "|&",
    ">>",
    ">&",
    "<&",
    "&>",
    "<>",
    ">|",
    ";",
    "&",
    "|",
    "(",
    ")",
    "<",
    ">",
)
_SEPARATORS: Final = frozenset({";", "&", "&&", "|", "||", "|&", ";;", "(", ")"})
_REDIRECTS: Final = frozenset({"<", ">", ">>", ">&", "<&", "&>", "&>>", "<>", ">|"})
_KEYWORDS: Final = frozenset(
    {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "esac"}
)
_HEADERS: Final = frozenset({"for", "select", "case"})
_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BRACED: Final = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*#?$!-])\}")
_ASSIGNMENT: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=")
_SPECIAL_PARAMS: Final = frozenset("@*#?$!-0123456789")
# after `$`, these leave the `$` a literal character (end of the word)
_LITERAL_DOLLAR: Final = frozenset(" \t\r\n;&|<>)")


@dataclass(frozen=True)
class _Options:
    """Every option a program takes before its command: short letters without and with a value,
    long names without, with (`--x v`, `--x=v`) and with an optional `=value`; `positional`
    plain words before the command (timeout's duration); `numeric`: `-5` is an option (nice)."""

    short: str = ""
    short_valued: str = ""
    long: frozenset[str] = frozenset()
    long_valued: frozenset[str] = frozenset()
    long_optional: frozenset[str] = frozenset()
    positional: int = 0
    numeric: bool = False
    values_follow: bool = False  # sh/bash: each valued letter of a cluster takes the next word


# Options left out on purpose (outside the model, so opaque): sudo -s/-i/--shell/--login and
# doas -s (a shell runs the command text), env -S/--split-string (its own quoting), xargs
# -I/-i/--replace (the replace string can name the program), ionice/taskset/chrt -p (a pid).
_WRAPPERS: Final[dict[str, _Options]] = {
    "sudo": _Options(
        short="AbBEeHKkLlNnPSVv",
        short_valued="CDgprRtTUu",
        long=frozenset(
            {
                "--askpass",
                "--background",
                "--bell",
                "--edit",
                "--set-home",
                "--remove-timestamp",
                "--reset-timestamp",
                "--list",
                "--non-interactive",
                "--preserve-groups",
                "--stdin",
                "--validate",
                "--version",
            }
        ),
        long_valued=frozenset(
            {
                "--user",
                "--group",
                "--close-from",
                "--chdir",
                "--host",
                "--prompt",
                "--role",
                "--type",
                "--other-user",
                "--command-timeout",
                "--chroot",
            }
        ),
        long_optional=frozenset({"--preserve-env"}),
    ),
    "doas": _Options(short="nL", short_valued="uC"),
    "env": _Options(
        short="i0v",
        short_valued="uC",
        long=frozenset({"--ignore-environment", "--null", "--debug"}),
        long_valued=frozenset({"--unset", "--chdir"}),
    ),
    "nohup": _Options(),
    "setsid": _Options(short="cfw", long=frozenset({"--ctty", "--fork", "--wait"})),
    "nice": _Options(short_valued="n", long_valued=frozenset({"--adjustment"}), numeric=True),
    "ionice": _Options(
        short="t",
        short_valued="cn",
        long=frozenset({"--ignore"}),
        long_valued=frozenset({"--class", "--classdata"}),
    ),
    "timeout": _Options(
        short="v",
        short_valued="sk",
        long=frozenset({"--preserve-status", "--foreground", "--verbose"}),
        long_valued=frozenset({"--signal", "--kill-after"}),
        positional=1,
    ),
    "time": _Options(
        short="apqv",
        short_valued="fo",
        long=frozenset({"--append", "--portability", "--verbose", "--quiet"}),
        long_valued=frozenset({"--format", "--output"}),
    ),
    "command": _Options(short="pvV"),
    "builtin": _Options(),
    "exec": _Options(short="cl", short_valued="a"),
    "stdbuf": _Options(
        short_valued="ioe", long_valued=frozenset({"--input", "--output", "--error"})
    ),
    "chrt": _Options(
        short="abdfiorRv",
        short_valued="DPT",
        long=frozenset(
            {
                "--all-tasks",
                "--batch",
                "--deadline",
                "--fifo",
                "--idle",
                "--other",
                "--rr",
                "--reset-on-fork",
                "--verbose",
            }
        ),
        long_valued=frozenset({"--sched-runtime", "--sched-period", "--sched-deadline"}),
        positional=1,
    ),
    "taskset": _Options(short="ac", long=frozenset({"--all-tasks", "--cpu-list"}), positional=1),
    "unbuffer": _Options(short="p"),
    "busybox": _Options(),
    "xargs": _Options(
        short="0oprtx",
        short_valued="adELnPs",
        long=frozenset(
            {"--null", "--no-run-if-empty", "--verbose", "--interactive", "--exit", "--open-tty"}
        ),
        long_valued=frozenset(
            {
                "--arg-file",
                "--delimiter",
                "--max-args",
                "--max-procs",
                "--max-chars",
                "--process-slot-var",
            }
        ),
        long_optional=frozenset({"--eof", "--max-lines"}),  # a value only as --eof=STR
    ),
}
_SHELLS: Final = frozenset({"sh", "bash", "dash", "zsh", "ksh", "mksh", "ash", "yash", "posh"})
# sh/bash options (also as +x); `c` takes the script from the first word after the options, `s`
# (read the script from stdin) is left out: opaque
_SHELL_OPTIONS: Final = _Options(
    short="abcefhiklmnprtuvxBCEHPT",
    short_valued="oO",
    long=frozenset(
        {"--norc", "--noprofile", "--login", "--posix", "--restricted", "--verbose", "--noediting"}
    ),
    long_valued=frozenset({"--rcfile", "--init-file"}),
    values_follow=True,
)
# programs that run a command Argus does not parse out of their arguments
_RUNNERS: Final = frozenset(
    {
        "su",
        "runuser",
        "pkexec",
        "chroot",
        "nsenter",
        "unshare",
        "watch",
        "flock",
        "parallel",
        "script",
        "strace",
        "ltrace",
        "gdb",
        "systemd-run",
        "trap",
        "coproc",
        "expect",
        "chpst",
        "fakeroot",
        "firejail",
        "bwrap",
        "hash",  # hash -p FILE NAME: NAME runs FILE
        "alias",  # with expand_aliases, a later word runs the alias text
        "enable",  # enable -f FILE NAME loads a builtin
    }
)
# interpreters: seen into only as `INTERPRETER SCRIPT ...` (see _interpreter)
_INTERPRETERS: Final = frozenset(
    {
        "python",
        "python2",
        "python3",
        "pypy",
        "pypy3",
        "node",
        "nodejs",
        "deno",
        "bun",
        "perl",
        "ruby",
        "php",
        "lua",
        "tclsh",
        "osascript",
        "pwsh",
        "powershell",
        "julia",
        "irb",
    }
)
_FIND_EXEC: Final = frozenset({"-exec", "-execdir", "-ok", "-okdir"})


# ---------------------------------------------------------------- lexer
class _Lexer:
    """Words and operators of one shell text, quotes and escapes removed."""

    def __init__(self, text: str) -> None:
        self.s, self.i = text, 0
        self.out: list[Word | str] = []
        self.buf: list[str] = []
        self.size = 0  # characters in buf
        self.started = False  # a word is being built (it may be empty: '')
        self.dynamic = False
        self.quoted = False
        self.quoted_from: int | None = None
        self.special = ""  # unquoted glob and brace characters of the current word

    def run(self) -> list[Word | str]:
        while self.i < len(self.s):
            self._step()
        self._end_word()
        return self.out

    def _peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.s[j] if j < len(self.s) else ""

    def _add(self, text: str) -> None:
        self.buf.append(text)
        self.size += len(text)
        self.started = True

    def _quoting(self) -> None:
        """A quote or an escape starts here: the word's text from here on is not shell syntax
        (bash recognizes keywords and assignments only unquoted)."""
        if self.quoted_from is None:
            self.quoted_from = self.size

    def _end_word(self) -> None:
        if self.started:
            g, text = self.special, "".join(self.buf)
            brace = "{" in g and "}" in g and text != "{}"  # a lone {} is literal (find's)
            pattern = "*" in g or "?" in g or ("[" in g and "]" in g) or brace
            self.out.append(Word(text, self.dynamic or pattern, self.quoted_from))
        self.buf, self.started, self.dynamic, self.quoted = [], False, False, False
        self.special, self.size, self.quoted_from = "", 0, None

    def _step(self) -> None:
        c = self.s[self.i]
        if c in " \t\r":
            self._end_word()
            self.i += 1
        elif c == "\n":
            self._end_word()
            self.out.append(";")
            self.i += 1
        elif c == "#" and not self.started:
            while self.i < len(self.s) and self.s[self.i] != "\n":
                self.i += 1
        elif c == "\\":
            self._backslash()
        elif c == "'":
            self._single()
        elif c == '"':
            self._double()
        elif c == "$":
            self._dollar(in_double=False)
        elif c == "`":
            raise UnparsableError("command substitution (backticks)")
        elif c in ";&|()<>":
            self._operator()
        else:
            if c in "*?[]{}":  # a glob or a brace expansion once the word holds a pair
                self.special += c
            self._add(c)
            self.i += 1

    def _backslash(self) -> None:
        nxt = self._peek(1)
        if nxt == "\n":  # line continuation
            self.i += 2
            return
        self._quoting()
        self._add(nxt or "\\")
        self.i += 2

    def _single(self) -> None:
        end = self.s.find("'", self.i + 1)
        if end < 0:
            raise UnparsableError("unbalanced single quote")
        self._quoting()
        self._add(self.s[self.i + 1 : end])
        self.quoted = True
        self.i = end + 1

    def _double(self) -> None:
        self._quoting()
        self.i += 1
        self.quoted = True
        self.started = True
        while True:
            if self.i >= len(self.s):
                raise UnparsableError("unbalanced double quote")
            c = self.s[self.i]
            if c == '"':
                self.i += 1
                return
            if c == "\\" and self._peek(1) in ('"', "\\", "$", "`", "\n"):
                if self._peek(1) != "\n":
                    self._add(self._peek(1))
                self.i += 2
            elif c == "`":
                raise UnparsableError("command substitution (backticks)")
            elif c == "$":
                self._dollar(in_double=True)
            else:
                self._add(c)
                self.i += 1

    def _dollar(self, *, in_double: bool) -> None:
        """`$NAME`, `${NAME}`, `$1`, `$@` ... are expansions; `$` before a blank, an operator, the
        end (or, inside "...", a quote) is the character; every other `$` form is outside the
        model."""
        nxt = self._peek(1)
        if m := _BRACED.match(self.s, self.i):
            self._expansion(m.group(0))
            self.i = m.end()
        elif nxt and nxt in _SPECIAL_PARAMS:
            self._expansion(self.s[self.i : self.i + 2])
            self.i += 2
        elif m := _NAME.match(self.s, self.i + 1):
            self._expansion(self.s[self.i : m.end()])
            self.i = m.end()
        elif nxt == "" or nxt in _LITERAL_DOLLAR or (in_double and nxt in "\"'"):
            self._add("$")
            self.i += 1
        else:
            what = {"(": "command substitution $(...)", "{": "a ${...} form"}.get(
                nxt, f"the ${nxt} form"
            )
            raise UnparsableError(f"{what}: outside the shell syntax Argus models")

    def _expansion(self, raw: str) -> None:
        self._add(raw)
        self.dynamic = True

    def _operator(self) -> None:
        c, nxt = self.s[self.i], self._peek(1)
        if c in "<>" and nxt == "(":
            raise UnparsableError("process substitution")
        if c == "<" and nxt == "<":
            raise UnparsableError("here-document or here-string")
        if c == "(" and nxt == "(":
            raise UnparsableError("arithmetic command ((...))")
        if self.s.startswith((";&", ";;&"), self.i):
            raise UnparsableError("case fall-through ;& or ;;&")
        if c == "(" and self.started:
            self._paren_in_word()
            return
        op = next(o for o in _OPERATORS if self.s.startswith(o, self.i))
        # `2>file`: the digits right before a redirection are its file descriptor, not a word
        if op in _REDIRECTS and self.started and not self.quoted and "".join(self.buf).isdigit():
            self.buf, self.started, self.dynamic, self.special, self.size = [], False, False, "", 0
        self._end_word()
        self.out.append(op)
        self.i += len(op)

    def _paren_in_word(self) -> None:
        """`NAME()` defines a function (its body follows as a group); any other `(` inside a
        word (an array assignment, an extended glob) is outside the model."""
        name = "".join(self.buf)
        if self.quoted or self.dynamic or not _NAME.fullmatch(name) or self._peek(1) != ")":
            raise UnparsableError("a ( inside a word")
        self._end_word()
        self.out.append(";")
        self.i += 2


# ---------------------------------------------------------------- simple commands
def _segments(tokens: list[Word | str]) -> list[list[Word]]:
    """Words of each simple command, redirections and their targets removed."""
    out: list[list[Word]] = [[]]
    k = 0
    while k < len(tokens):
        t = tokens[k]
        if isinstance(t, Word):
            out[-1].append(t)
        elif t in _SEPARATORS:
            out.append([])
        elif t in _REDIRECTS:
            if k + 1 >= len(tokens) or not isinstance(tokens[k + 1], Word):
                raise UnparsableError(f"redirection {t} without a target")
            k += 1
        k += 1
    return [s for s in out if s]


def _syntax(w: Word, *words: str) -> bool:
    """`w` is one of the reserved `words` as bash sees them: unquoted, unescaped, no expansion."""
    return not w.dynamic and w.quoted_from is None and w.text in words


def _assignment(w: Word) -> bool:
    """`w` is a `NAME=value` assignment as bash sees it: `NAME=` unquoted (`A="x y"` is one,
    `"A"=1` and `A\\=1` are not)."""
    m = _ASSIGNMENT.match(w.text)
    return m is not None and (w.quoted_from is None or w.quoted_from >= m.end())


def _strip_prefix(words: list[Word]) -> list[Word]:
    """Without leading keywords (`if`, `do`, `!`, `{` ...), `function NAME` and `VAR=value`
    assignments (whose value is not word-split, so an expansion there runs nothing), each only
    where bash would see it as such (unquoted)."""
    k = 0
    while k < len(words):
        w = words[k]
        if _syntax(w, "function"):
            k += 2
        elif _syntax(w, *_KEYWORDS) or _assignment(w):
            k += 1
        else:
            break
    return words[k:]


class _OutsideError(Exception):
    """A command uses something outside the model (the reason is the message)."""


def _short(t: str, k: int, opts: _Options) -> tuple[int, str]:
    """The index after a cluster of short options at index k, and its letters (up to the one that
    takes a value). getopt: a value is the rest of the cluster or the next word (`-uroot`,
    `-nu root`); a shell: each valued letter takes the next word, in order (`-oc pipefail TEXT`)."""
    if opts.values_follow:
        if bad := [c for c in t[1:] if c not in opts.short and c not in opts.short_valued]:
            raise _OutsideError(f"option {t[0]}{bad[0]}")
        return k + 1 + sum(c in opts.short_valued for c in t[1:]), t[1:]
    for j, c in enumerate(t[1:], start=1):
        if c in opts.short_valued:
            return (k + 1 if j < len(t) - 1 else k + 2), t[1 : j + 1]
        if c not in opts.short:
            raise _OutsideError(f"option {t[0]}{c}")
    return k + 1, t[1:]


def _long(t: str, k: int, opts: _Options) -> int:
    name, eq, _ = t.partition("=")
    if name in opts.long_valued:
        return k + 1 if eq else k + 2
    if name in opts.long_optional or (name in opts.long and not eq):
        return k + 1
    raise _OutsideError(f"option {name}")


def _skip_options(words: list[Word], k: int, opts: _Options) -> tuple[int, str]:
    """Index of the first word after the options (and positional words) that start at `k`, and
    the short option letters seen; `_OutsideError` for an option not listed or an expansion among
    them (it may become any words)."""
    letters = ""
    while k < len(words):
        w = words[k]
        t = w.text
        if w.dynamic:
            raise _OutsideError("an expansion among its options")
        if t == "--":
            k += 1
            break
        if t.startswith("--"):
            nxt = _long(t, k, opts)
        elif opts.numeric and t[:1] == "-" and t[1:].isdigit():
            nxt = k + 1
        elif t[:1] in "-+" and len(t) > 1 and (t[0] == "-" or opts is _SHELL_OPTIONS):
            nxt, seen = _short(t, k, opts)
            letters += seen
        else:
            break
        if any(v.dynamic for v in words[k + 1 : nxt]):
            raise _OutsideError(f"an expansion as the value of {t}")
        k = nxt
    end = k + opts.positional
    if any(w.dynamic for w in words[k:end]):
        raise _OutsideError("an expansion before its command")
    return end, letters


def _program(w: Word) -> str:
    return w.text.rsplit("/", 1)[-1]


def _interpreter(words: list[Word]) -> str | None:
    """None when an interpreter runs a script file Argus can name: `python3 script.py ...`
    (its first argument a static word that is not an option) or `python3 -m module ...`. Any
    other form is opaque: options (inline code such as `-c`, `-e`, `-cCODE`, `--eval` cannot be
    told from other options without each interpreter's option table), `deno eval` / `bun eval`,
    an expansion, or no argument at all (the program comes from stdin)."""
    name = _program(words[0])
    if len(words) < 2:
        return f"{name} reads its program from stdin"
    first = words[1]
    if first.dynamic:
        return f"{name} runs an expansion"
    if first.text == "-m" and name.startswith(("python", "pypy")) and len(words) > 2:
        return None
    if first.text.startswith("-") or (name in ("deno", "bun") and first.text == "eval"):
        return f"{name} {first.text}: inline code cannot be ruled out"
    return None


class _Walker:
    """Expands wrappers, shells, `eval` and `find -exec` of the commands of one text into
    `parsed`."""

    def __init__(self, parsed: Parsed) -> None:
        self.parsed = parsed

    def text(self, text: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise UnparsableError(f"nested shell deeper than {MAX_DEPTH} levels")
        for seg in _segments(_Lexer(text).run()):
            self.command(seg, depth)

    def command(self, words: list[Word], depth: int, *, shell: bool = True) -> None:
        """One simple command: as the shell reads it (`shell`: keywords, assignments, headers),
        or as a program that runs its arguments directly (a wrapper's command, find -exec): the
        first word is the program, whatever it says."""
        if shell:
            words = _strip_prefix(words)
            if words and _syntax(words[0], *_HEADERS):
                return
        if not words:
            return
        try:
            self._dispatch(words, depth)
        except _OutsideError as e:
            shown = " ".join(w.text for w in words)[:80]
            self.parsed.opaque.append(f"`{shown}`: {e} (outside what Argus models)")

    def _dispatch(self, words: list[Word], depth: int) -> None:
        prog = words[0]
        if prog.dynamic:
            raise _OutsideError("the program is an expansion")
        name = _program(prog)
        # the command itself runs, whatever it runs in turn (a check may name `sudo` or `bash`)
        self.parsed.commands.append(words)
        if name in _WRAPPERS:
            self._wrapper(words, name, depth)
        elif name in _SHELLS:
            self._shell(words, depth)
        elif name == "eval":
            self._eval(words[1:], depth)
        elif name in ("source", ".") and any(w.dynamic for w in words[1:]):
            raise _OutsideError(f"{name} of an expansion")
        elif name in _RUNNERS:
            raise _OutsideError(f"{name} runs a command Argus does not parse")
        elif name in _INTERPRETERS and (why := _interpreter(words)) is not None:
            raise _OutsideError(why)
        elif name == "find":
            self._find(words, depth)

    def _find(self, words: list[Word], depth: int) -> None:
        """find, and the commands of its -exec/-execdir/-ok/-okdir PROGRAM ... ; (or +)."""
        if any(w.dynamic for w in words[1:]):
            raise _OutsideError("an expansion in find's expression (it may add an -exec)")
        inner: list[list[Word]] = []
        k = 1
        while k < len(words):
            if words[k].text in _FIND_EXEC:
                end = k + 1
                # `;` ends the command; `+` only right after `{}` (elsewhere it is an argument)
                while end < len(words) and not (
                    words[end].text == ";"
                    or (words[end].text == "+" and words[end - 1].text == "{}")
                ):
                    end += 1
                if end >= len(words) or end == k + 1:
                    raise _OutsideError(f"{words[k].text} without a program or a terminator")
                if "{}" in words[k + 1].text:
                    raise _OutsideError(f"{words[k].text} whose program is find's {{}}")
                # find replaces {} (quoted or not) with a path: not a known word
                inner.append([Word(w.text, "{}" in w.text) for w in words[k + 1 : end]])
                k = end
            k += 1
        for cmd in inner:
            self.command(cmd, depth, shell=False)

    def _wrapper(self, words: list[Word], name: str, depth: int) -> None:
        k, letters = _skip_options(words, 1, _WRAPPERS[name])
        if name == "command" and set(letters) & {"v", "V"}:
            return  # command -v / -V describe the name, they do not run it
        if name in ("env", "sudo"):  # they take NAME=value arguments before the command
            while k < len(words) and (
                (name == "env" and words[k].text == "-") or _ASSIGNMENT.match(words[k].text)
            ):
                if words[k].dynamic:  # their arguments are word-split, unlike an assignment
                    raise _OutsideError(f"an expansion among {name}'s assignments")
                k += 1
        rest = words[k:]
        if name == "xargs" and rest:
            # xargs appends what it reads from stdin to the command's arguments
            rest = [*rest, Word("<xargs input>", dynamic=True)]
        if not rest:
            return
        # the program runs its arguments directly; `time` may be bash's keyword, which reads a
        # whole command (keywords, assignments): both readings are checked
        self.command(rest, depth, shell=False)
        if name == "time":
            self.command(rest, depth, shell=True)

    def _shell(self, words: list[Word], depth: int) -> None:
        k, letters = _skip_options(words, 1, _SHELL_OPTIONS)
        if "c" in letters:
            if k >= len(words):
                raise _OutsideError(f"{_program(words[0])} -c without a script")
            if words[k].dynamic:
                raise _OutsideError("a shell script that is an expansion")
            self.text(words[k].text, depth + 1)
        elif k >= len(words):
            raise _OutsideError(f"{_program(words[0])} reads its script from stdin")
        # else a script file: Argus cannot see into it (docs/enforce.md, limits)

    def _eval(self, words: list[Word], depth: int) -> None:
        if any(w.dynamic for w in words):
            raise _OutsideError("eval of an expansion")
        self.text(" ".join(w.text for w in words), depth + 1)


def parse(text: str) -> Parsed:
    """The simple commands `text` runs (see the module docstring); raises UnparsableError."""
    if "\x00" in text:
        raise UnparsableError("NUL byte")
    parsed = Parsed()
    _Walker(parsed).text(text, 0)
    return parsed
