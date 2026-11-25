"""The check format (argus/checkspec.py) and its shell parser (argus/shellwords.py): parsing is
closed, and matching holds against adversarial argument shapes. `unknown` (cannot verify) counts as
a match in the hook, so every shape that could hide a match must answer `match` or `unknown`."""

import random
import string
from pathlib import Path

import pytest

from argus import checkspec, shellwords
from argus.checkspec import Spec

FORCE_PUSH = checkspec.parse_spec(
    {
        "v": 1,
        "tool": "terminal",
        "field": "command",
        "match": "command",
        "program": "git",
        "args": ["push"],
        "flags": ["--force", "-f"],
    }
)
RM = checkspec.parse_spec(
    {"v": 1, "tool": "terminal", "field": "command", "match": "command", "program": "rm"}
)


def kind(spec: Spec, value: object, field: str = "command") -> str:
    return checkspec.evaluate(spec, {field: value})[0]


# ---------------------------------------------------------------- parse
@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ("text", "must be a JSON object"),
        ({"v": 2, "tool": "t", "match": "tool"}, "v must be 1"),
        ({"v": True, "tool": "t", "match": "tool"}, "v must be 1"),
        ({"v": 1, "tool": "t", "match": "regex"}, "match must be one of"),
        ({"v": 1, "tool": "t", "match": "tool", "code": "x"}, "unknown key"),
        ({"v": 1, "tool": "t", "match": "tool", "field": "x"}, "unknown key"),
        ({"v": 1, "tool": "", "match": "tool"}, "tool must be a non-empty string"),
        ({"v": 1, "tool": "a b", "match": "tool"}, "is not a tool name"),
        ({"v": 1, "tool": "t", "match": "equals", "field": "f"}, "missing key"),
        ({"v": 1, "tool": "t", "match": "equals", "field": "a.b", "value": "x"}, "argument"),
        ({"v": 1, "tool": "t", "match": "equals", "field": "f", "value": 3}, "non-empty string"),
        ({"v": 1, "tool": "t", "match": "equals", "field": "f", "value": "x" * 501}, "longer"),
        ({"v": 1, "tool": "t", "match": "path", "field": "p", "under": "etc"}, "absolute"),
        ({"v": 1, "tool": "t", "match": "path", "field": "p", "under": "/e\x00"}, "NUL"),
        (
            {"v": 1, "tool": "t", "match": "command", "field": "c", "program": "/bin/rm"},
            "program name",
        ),
        (
            {"v": 1, "tool": "t", "match": "command", "field": "c", "program": "rm", "flags": "-f"},
            "list",
        ),
        (
            {
                "v": 1,
                "tool": "t",
                "match": "command",
                "field": "c",
                "program": "rm",
                "flags": ["f"],
            },
            "not allowed",
        ),
        (
            {"v": 1, "tool": "t", "match": "command", "field": "c", "program": "rm", "args": [1]},
            "not allowed",
        ),
    ],
)
def test_parse_refuses_what_is_not_the_closed_format(raw: object, error: str) -> None:
    with pytest.raises(checkspec.SpecError, match=error):
        checkspec.parse_spec(raw)


def test_canonical_text_and_hash_are_stable() -> None:
    a = checkspec.parse_spec(
        {
            "match": "path",
            "under": "/etc/../etc//ssh/",
            "field": "path",
            "tool": "write_file",
            "v": 1,
        }
    )
    b = checkspec.parse_spec(
        {"v": 1, "tool": "write_file", "field": "path", "match": "path", "under": "/etc/ssh"}
    )
    assert checkspec.canonical(a) == checkspec.canonical(b)
    text = checkspec.canonical(FORCE_PUSH)
    assert checkspec.load(text) == FORCE_PUSH
    assert checkspec.digest(text) == checkspec.digest(checkspec.canonical(checkspec.load(text)))
    assert len(checkspec.digest(text)) == 64
    with pytest.raises(checkspec.SpecError, match="not JSON"):
        checkspec.load("{")


def test_describe() -> None:
    assert checkspec.describe(FORCE_PUSH) == "terminal.command runs `git push` with --force|-f"


# ---------------------------------------------------------------- command
@pytest.mark.parametrize(
    "command",
    [
        "git push --force",
        "git push origin main -f",
        "git push -fu origin main",  # a cluster of short flags
        "git push --force=yes",
        "git -C /repo push --force",  # options between program and args
        "/usr/bin/git push --force",
        "ls; git push --force",
        "ls && git push -f",
        "false || git push -f",
        "echo hi | git push -f",
        "ls\ngit push -f",
        "(git push -f)",
        "{ git push -f; }",
        "if true; then git push -f; fi",
        "for b in a; do git push -f; done",
        "FOO=1 git push -f",
        "sudo -u root git push -f",
        "sudo -- git push -f",
        "env -u HOME X=1 git push -f",
        "timeout -s KILL 5 git push -f",
        "nohup nice -n 5 git push -f &",
        "xargs -n1 git push -f",
        "xargs --eof git push -f",  # --eof and --max-lines take a value only after =
        "xargs --max-lines git push -f",
        "xargs --eof=x --max-lines=1 git push -f",
        "command git push -f",
        "bash -c 'git push -f'",
        "bash -lc 'ls; git push -f'",
        'sh -c "cd /x && git push --force"',
        "eval git push -f",
        "eval 'git push -f'",
        "\\git push -f",
        "g'i't push -f",
        'g""it push "--force"',
        "git push -f 2>&1 >/dev/null",
        ">out git push -f",
        "! git push -f",
        "case x in a) git push -f;; esac",
        "find . -name x -exec git push -f \\;",
        "find . -exec git push {} -f \\;",
        "find . -exec git push + -f \\;",  # + ends -exec only right after {}
        "bash -oc pipefail 'git push -f'",  # bash: -o's value is the next word, then -c's
        "bash -co pipefail 'git push -f'",
        "sh -oo pipefail errexit -c 'git push -f'",
        "find . -exec git push {} + -exec git push -f {} +",
        "f() { git push -f; }",
        "sudo -nu root -- git push -f",
        "time -p nice -n 3 setsid -f git push -f",
        "env -i - A=1 git push -f",
        "git push --forc",  # getopt_long accepts an unambiguous prefix of a long option
        "git push --fo=yes",
        "busybox sh -c 'git push -f'",
        "sudo -nu root git push -f",  # a cluster ending in an option that takes a value
        "sudo -uroot git push -f",
        "bash -euo pipefail -c 'git push -f'",
        "bash -ce 'git push -f'",
        "env -iu HOME git push -f",
        "function f { git push -f; }; f",
        "function f() { git push -f; }",
        "function f { function g { git push -f; }; g; }; f",
        "find . -execdir sh -c 'git push -f' {} +",
    ],
)
def test_command_matches_through_shell_syntax(command: str) -> None:
    assert kind(FORCE_PUSH, command) == "match", command


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git pull --force",
        "git commit -m 'git push -f'",  # quoted text is an argument, not a command
        "echo git push -f",
        "# git push -f",
        "ls # ; git push -f",
        "git push --force-with-lease",  # another flag
        "gitk push -f",
        "cd $HOME && ls",  # expansions where they cannot hide git
        "echo ${HOME}",
        "python3 script.py --force",  # a script file: a known limit, not inline code
        "python3 -m pytest -q -p no:cacheprovider -c x",  # python -m MODULE: its arguments
        "find . -name '*.py' -exec black {} +",
        "git push --f",  # `--` and one letter: not taken as an abbreviation
        "xargs",  # runs echo
        "command -v git push -f",  # -v / -V describe the name, they do not run it
        "command -pV git push -f",
        "f() { ls; }",
        "echo $ costs; echo \"$\" '$(x)'",  # a lone $ is a character; '...' is literal
        "find . -name '*.py' -exec black '{}' \\;",
        "sudo -u root -- ls -f",
        "env -i A=1 ls -f",
        "nice -5 ls -f",
        "timeout -k 5 10 ls -f",
        "bash -o pipefail -c 'ls -f'",  # -o's value is not a letter cluster
        "bash +x script.sh -f",
    ],
)
def test_command_does_not_match_other_commands(command: str) -> None:
    assert kind(FORCE_PUSH, command) == "no", command


@pytest.mark.parametrize(
    "command",
    [
        "git push $(echo --force)",  # command substitution anywhere
        "git push `echo -f`",
        'git commit -m "$(cat msg)"',  # harmless, still unreadable: denied by design
        "$CMD push -f",  # the program is an expansion
        "${GIT} push -f",
        "/usr/bin/gi* push -f",  # a glob in program position
        "gi{t,} push -f",  # brace expansion
        "git push $FLAGS",  # the flag could be in the expansion
        "git $SUB -f",  # the arg could be in the expansion
        "echo 'git push -f' | sh",  # a shell reading stdin
        "bash < script.sh",
        'bash -c "$X"',
        "eval $X",
        "cat <<EOF\ngit push -f\nEOF",
        "diff <(ls) <(ls)",
        "git push 'unbalanced",
        'git push "unbalanced',
        'bash -c \'bash -c "bash -c \\"bash -c ls\\""\'',  # deeper than MAX_DEPTH
        "git push -f >",
        "python3 -c 'import os; os.system(\"git push -f\")'",  # inline code
        "node -e 'x'",
        "echo 'import os; os.system(\"git push -f\")' | python3",  # a program from stdin
        "python3 - < x.py",
        'python3 -c"import os" x',  # inline code attached to its flag
        "python3 -W error -c 'x'",  # an option with a value before -c
        "perl -we 'x'",
        "python3 $X",
        "deno eval 'x'",
        "python3 -u script.py",  # options before the script: over-wide on purpose
        "perl -Mstrict -e 'x'",
        "su -c 'git push -f' me",  # a runner whose command Argus does not parse
        "watch git push -f",
        "hash -p /usr/bin/git ls; ls push -f",  # a later name runs another program
        "alias ls=git\nls push -f",
        "printf '%s\\n' --force | xargs git push",  # xargs appends its input as arguments
        "echo --force | xargs -0 git push origin",
        "env -S '$GIT push -f'",
        "env -S 'git push; ls' -f",
        "find /x -exec {} push -f \\;",  # {} names the program
        'sudo -s "$CMD"',
        # review 2's reproductions: each runs `git push -f`
        '$"git" push -f',  # $"..." quoting is outside the model
        "env -S 'git\\_push\\_-f'",  # env -S splits with escapes of its own
        "find /scratch/git -maxdepth 0 -exec {} push -f \\;",  # {} names the program
        # the opaque classes of docs/enforce.md, section 2
        "env -S 'git push -f'",
        "env -Sgit push -f",
        "env --split-string='git push -f'",
        "env -i -S 'git push -f'",
        "env -iS 'git push -f'",
        "git push $'-f'",  # $'...' quoting
        "git push ${F:--f}",  # a ${...} form other than ${NAME}
        "git push $[1]",
        "sudo -s 'git push -f'",  # sudo -s / -i hand the text to a shell
        "sudo -i git push -f",
        "sudo --login git push -f",
        "sudo -X git push -f",  # an option Argus does not list
        "sudo --us=root git push -f",  # a long-option prefix of a wrapper
        "sudo -u $U push -f",  # an expansion where it decides the program
        "nohup $X push -f",
        "timeout $T git push -f",
        "env A=1 $X push -f",
        "env A=$X git push -f",  # env's arguments are word-split
        "xargs -I{} {} push -f",  # the replace string can name the program
        "xargs -i git push {}",
        "find . -exec ./{} push -f \\;",
        "find . $EXPR",  # the expansion may be an -exec
        "find . -exec git push -f",  # no terminator
        "bash -s",  # a shell reading stdin
        "bash -c",
        "source $F",
        '. "$F"',
        "git push -@(f)",  # a ( inside a word
        "a=(git push -f)",
        "((x++)); git push",
        "case x in x) ls ;& y) git push -f;; esac",  # case fall-through: outside the model
        "case x in x) ls ;;& y) ls;; esac",
    ],
)
def test_command_cannot_verify_what_it_cannot_read(command: str) -> None:
    assert kind(FORCE_PUSH, command) == "unknown", command


def _program_check(program: str) -> Spec:
    return checkspec.parse_spec(
        {
            "v": 1,
            "tool": "terminal",
            "field": "command",
            "match": "command",
            "program": program,
            "args": ["push"],
            "flags": ["-f"],
        }
    )


@pytest.mark.parametrize(
    ("program", "command", "verdict"),
    [
        # bash sees keywords, `function` and assignments only unquoted: quoted, they are programs
        ("for", "'for' push -f", "match"),
        ("for", "\\for push -f", "match"),
        ("for", "fo''r push -f", "match"),
        ("for", "for x in push -f; do ls; done", "no"),  # a for header runs nothing
        ("if", '"if" push -f', "match"),
        ("function", "'function' push -f", "match"),
        ("A=1", '"A"=1 push -f', "match"),
        ("A=1", "A\\=1 push -f", "match"),
        ("A=1", "A=1 push -f", "no"),  # an assignment, then the program push
        # a wrapper or find -exec runs its first argument as a program, whatever it says
        ("for", "nohup for push -f", "match"),
        ("for", "find . -exec for push -f \\;", "match"),
        ("for", "time for push -f", "match"),  # /usr/bin/time, or bash's keyword
        ("A=1", "command A=1 push -f", "match"),
        ("git", "time A=1 git push -f", "match"),
        ("git", "sudo A=1 git push -f", "match"),  # sudo takes NAME=value before the command
        ("git", "sudo A=$X git push -f", "unknown"),
        # a check may name a wrapper or a shell itself: it runs too
        ("time", "nohup time push -f", "match"),
        ("sudo", "sudo push -f", "match"),
        ("bash", "bash -c 'ls' push -f", "match"),
    ],
)
def test_shell_syntax_only_where_bash_sees_it(program: str, command: str, verdict: str) -> None:
    assert kind(_program_check(program), command) == verdict, command


def test_command_reasons_and_non_string_values() -> None:
    assert checkspec.evaluate(FORCE_PUSH, {}) == ("unknown", "argument command is missing")
    assert kind(FORCE_PUSH, ["git", "push", "-f"]) == "unknown"
    assert kind(FORCE_PUSH, {"cmd": "git"}) == "unknown"
    assert kind(FORCE_PUSH, 3) == "unknown"
    assert kind(FORCE_PUSH, "git push -f\x00") == "unknown"
    verdict, why = checkspec.evaluate(FORCE_PUSH, {"command": "git push $(x)"})
    assert verdict == "unknown"
    assert "command substitution" in why
    assert kind(RM, "rm -rf /tmp/x") == "match"
    assert kind(RM, "ls") == "no"


def test_shellwords_reports_words() -> None:
    parsed = shellwords.parse("A=1 sudo -u me env B=2 git -c x=y push 'a b' $V\nls")
    git = ["git", "-c", "x=y", "push", "a b", "$V"]
    assert [[w.text for w in c] for c in parsed.commands] == [
        ["sudo", "-u", "me", "env", "B=2", *git],  # each program that runs, wrappers too
        ["env", "B=2", *git],
        git,
        ["ls"],
    ]
    assert parsed.commands[2][-1].dynamic
    assert not parsed.commands[2][-2].dynamic
    assert parsed.commands[2][-2].quoted_from == 0
    assert parsed.commands[2][0].quoted_from is None
    with pytest.raises(shellwords.UnparsableError, match="NUL"):
        shellwords.parse("ls\x00")


def _quoted(rng: random.Random, w: str) -> str:
    r = rng.random()
    if r < 0.2:
        return "'" + w + "'"
    if r < 0.4:
        return '"' + w + '"'
    if r < 0.55:
        return "".join("\\" + c if rng.random() < 0.3 else c for c in w)
    if r < 0.65 and len(w) > 1:
        i = rng.randrange(1, len(w))
        return w[:i] + "''" + w[i:]
    if r < 0.7:
        return '$"' + w + '"'
    return w


_SHELL_PREFIXES = ("", "A=1 B=2 ", "! ", "{ ", "if true; then ", "function f { ", "f() { ")
"""Shell syntax: only where the shell reads a command (not after a wrapper or find -exec)."""
_PREFIXES = (
    *("", "sudo ", "sudo -u x ", "env A=1 ", "nohup ", "timeout 5 ", "command ", "exec "),
    *("time ", "nice -n 3 ", "xargs -0 ", "stdbuf -o0 ", "sudo A=1 "),
    *("/usr/bin/env ", "busybox ", "env -S ", "env -i ", "env -u X ", "setsid ", "ionice -c3 "),
    *("sudo -nu root ", "sudo -s ", "env -iu X ", "sudo -X "),
    *("env -S 'nice -n 3' ", "xargs -I{} ", "nice -5 ", "doas ", "exec -a x "),
)
_SEPS = ("; ", " && ", " || ", " | ", "\n", " & ", "(", "")
_FORCE = ("-f", "--force", "-fu", "--force=1", "-vf", "--forc", "--fo")


def _force_push_shape(rng: random.Random) -> str:
    prefix = "".join(rng.choice(_PREFIXES) for _ in range(rng.randrange(0, 4)))
    words = [_quoted(rng, rng.choice(["git", "/usr/bin/git"])), _quoted(rng, "push")]
    if rng.random() < 0.5:
        words.append(_quoted(rng, rng.choice(["origin", "-v", "--quiet"])))
    words.append(_quoted(rng, rng.choice(_FORCE)))
    cmd = prefix + " ".join(words)
    if rng.random() < 0.2:
        cmd = rng.choice(["find . -exec ", "find . -execdir "]) + cmd + " \\;"
    cmd = rng.choice(_SHELL_PREFIXES) + cmd
    if sep := rng.choice(_SEPS):
        cmd = rng.choice(["ls", "echo hi", "cd /tmp"]) + sep + cmd
    if rng.random() < 0.3:
        inner = cmd.replace("'", "'\\''")
        cmd = rng.choice(["bash -c '%s'", "sh -lc '%s'", "eval '%s'"]) % inner
    return cmd


def test_fuzz_no_force_push_shape_evaluates_no() -> None:
    """Random shapes that run `git push --force` (quoting, wrappers, separators, nested shells,
    find -exec, option prefixes): each is a match or "cannot verify", never "no"."""
    rng = random.Random(11)
    misses = [
        c for c in (_force_push_shape(rng) for _ in range(6000)) if kind(FORCE_PUSH, c) == "no"
    ]
    assert misses == []


def test_fuzz_junk_never_raises() -> None:
    rng = random.Random(7)
    alphabet = string.printable + "äé€\x00"
    for _ in range(5000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 60)))
        assert checkspec.evaluate(FORCE_PUSH, {"command": text})[0] in ("no", "match", "unknown")


# ---------------------------------------------------------------- path
def path_check(under: str, tool: str = "write_file") -> Spec:
    return checkspec.parse_spec(
        {"v": 1, "tool": tool, "field": "path", "match": "path", "under": under}
    )


def test_path_matches_after_normalisation(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.mkdir()
    spec = path_check(str(secret))
    for p in (
        secret / "k",
        f"{secret}/",
        f"{secret}",
        f"{tmp_path}/x/../secret/k",
        f"{tmp_path}/./secret//k",
        f"/{secret}/k",  # a leading //
        f"  {secret}/k",
    ):
        assert kind(spec, str(p), "path") == "match", p
    assert kind(spec, f"{tmp_path}/secretive/k", "path") == "no"
    assert kind(spec, f"{tmp_path}/other", "path") == "no"


def test_path_follows_symlinks_both_ways(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.mkdir()
    (tmp_path / "link").symlink_to(secret)
    spec = path_check(str(secret))
    assert kind(spec, str(tmp_path / "link" / "k"), "path") == "match"
    # the check names the link: the real directory behind it matches too
    by_link = path_check(str(tmp_path / "link"))
    assert kind(by_link, str(secret / "k"), "path") == "match"


def test_path_resolves_dotdot_after_a_symlink(tmp_path: Path) -> None:
    """`link/../target` is the link target's parent's `target` for the filesystem, not the
    lexical sibling of `link`: resolved from the path as written."""
    secret = tmp_path / "secret"
    (secret / "dir").mkdir(parents=True)
    (tmp_path / "link").symlink_to(secret / "dir")
    spec = path_check(str(secret))
    assert kind(spec, f"{tmp_path}/link/../target", "path") == "match"
    assert kind(spec, f"{tmp_path}/other/../target", "path") == "no"


def _no_home(self: Path) -> Path:
    raise RuntimeError("Could not determine home directory.")


def test_path_home_and_relative(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = path_check("~/.ssh")
    assert kind(spec, f"{tmp_path}/.ssh/id_rsa", "path") == "match"
    assert kind(spec, "~/.ssh/id_rsa", "path") == "match"
    assert kind(spec, "~/.sshx", "path") == "no"
    assert kind(spec, ".ssh/id_rsa", "path") == "unknown"  # relative: base unknown
    assert kind(spec, "../.ssh/id_rsa", "path") == "unknown"
    assert kind(spec, "~nosuchuser_argus/.ssh", "path") == "unknown"
    assert kind(spec, "/etc/\x00", "path") == "unknown"
    assert kind(spec, None, "path") == "unknown"
    monkeypatch.delenv("HOME")
    monkeypatch.setattr(Path, "expanduser", _no_home)
    assert kind(spec, "/root/.ssh/k", "path") == "unknown"  # ~ cannot be expanded: deny
    everything = path_check("/")
    assert kind(everything, "/anything", "path") == "match"


def test_equals_and_tool() -> None:
    spec = checkspec.parse_spec(
        {"v": 1, "tool": "send_message", "field": "target", "match": "equals", "value": "#General"}
    )
    assert kind(spec, " #general ", "target") == "match"
    assert kind(spec, "#random", "target") == "no"
    assert kind(spec, 7, "target") == "unknown"
    any_call = checkspec.parse_spec({"v": 1, "tool": "browser_navigate", "match": "tool"})
    assert checkspec.evaluate(any_call, {})[0] == "match"
