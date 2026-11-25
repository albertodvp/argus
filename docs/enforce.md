# Enforced checks: an accepted rule as a deterministic `pre_tool_call` check

Argus proposes rules; a person accepts them and Argus writes them into a SKILL.md. A line in a skill
is advice: the model may skip it. This document describes how an accepted rule can also become a
**check** that Argus evaluates on every tool call in Hermes' `pre_tool_call` hook, outside the
model, and blocks when a person asked for that.

The idea comes from Mandate (Hermes Plugin Hackathon, #6): a policy learned per cron job, approved
by a person (versioned, sha256), enforced in `pre_tool_call`; observe first, then enforce; what it
cannot parse is denied; the model never approves. Argus keeps those principles and applies them to
single rules instead of a per-job allowlist. No Mandate code is used.

## 1. Which rules are checkable

Most Argus rules are procedural prose: "before deploying, run the tests", "check the lock first",
"prefer the staging profile". They describe an order of steps, an intention or a judgement. A
single tool call does not carry the information to decide them, so they are **not checkable** and
stay what they are today: a line in SKILL.md, followed (or not) by the model.

A rule is checkable only when it forbids something that **one tool call shows by itself**, in one
argument:

- "never force-push" -> a `terminal` call whose `command` runs `git push` with `--force`/`-f`;
- "never write under ~/.ssh" -> a `write_file` (or `patch`) call whose `path` is under `~/.ssh`;
- "do not use the browser for this profile" -> any `browser_navigate` call;
- "never send to the #general channel" -> a `send_message` call whose `target` equals a value.

Argus tells the two apart in two steps:

1. The `argus` auxiliary model drafts (section 3). It is asked whether the rule forbids something a
   single call shows, and answers `checkable: false` with a reason for everything else.
2. A deterministic parser accepts only the closed format below. A draft that does not parse is
   stored as not checkable, with the parser's reason. Nothing else becomes a check.

The rest (most rules) is shown in the dashboard and `hermes argus checks` as "not checkable" with
the reason, and is never enforced. Argus does not try to enforce sequences ("before X do Y"):
that would need state across calls, which is exactly where a deterministic check stops being
small and obviously correct.

## 2. The check format

A check is one JSON object, closed (no other key is accepted), version 1. It names one tool, one
argument field and one match on a normalised value. There is no code, no regular expression and
no eval. Four match kinds:

```json
{"v": 1, "tool": "browser_navigate", "match": "tool"}
{"v": 1, "tool": "send_message", "field": "target", "match": "equals", "value": "slack:#general"}
{"v": 1, "tool": "write_file", "field": "path", "match": "path", "under": "~/.ssh"}
{"v": 1, "tool": "terminal", "field": "command", "match": "command",
 "program": "git", "args": ["push"], "flags": ["--force", "-f"]}
```

| kind | matches a call of `tool` when | normalisation |
|---|---|---|
| `tool` | always (no field) | - |
| `equals` | `args[field]` equals `value` | both stripped and case-folded |
| `path` | `args[field]` is `under` or inside it | `~` expanded; compared in two forms, on both sides, a match in either counts: as written with `.`/`..` and `//` folded, and as the filesystem resolves it (`realpath` of the path as written: symlinks and `..` in order, so `link/..` is the link target's parent) |
| `command` | one command of the shell text in `args[field]` runs `program` (basename) with every word of `args` in order (not necessarily adjacent) and, when `flags` is not empty, one of the flags | shell words parsed with quotes and escapes removed (below) |

The command parser (`argus/shellwords.py`) understands an explicit, small allowlist of shell
syntax. **Anything outside it makes the text, or the command it is in, opaque, and an opaque part
is "cannot verify"** (a match, below). Argus does not try to model more shell to stay precise:
each new construct it would have to understand is a new way to be wrong.

What it models, and nothing else:

- **Words**: unquoted characters, backslash escapes, `'...'`, `"..."` (with the escapes `\"`,
  `\\`, `\$`, `` \` ``), `#` comments. The expansions `$NAME`, `${NAME}` and the special
  parameters (`$1`, `$@`, `$?`, ...) are allowed but make their word *dynamic*: its text is not
  what runs, and it may become several words or none. An unquoted glob (`*`, `?`, `[...]`) or
  brace pair (`{a,b}`; a lone `{}` is literal) makes a word dynamic too. A `$` before a blank, an
  operator or the end is the character `$`.
- **Operators**: `;`, `&`, `&&`, `|`, `||`, `|&`, `;;`, newlines, `(`/`)` as grouping, `NAME()`
  function definitions (the body is checked as if it ran), and redirections (`<`, `>`, `>>`,
  `>|`, `<>`, `&>`, `&>>`, `>&`, `<&`, with a file descriptor before them), which are dropped with
  their target.
- **Simple commands**: leading keywords (`if`, `then`, `do`, `!`, `{`, ...), `VAR=value`
  assignments and `function NAME` are skipped; a `for`/`select`/`case` header runs nothing. As in
  bash, only where the shell reads a command and only unquoted: `'for' x` or `"A"=1 x` run a
  program named `for` or `A=1`.
- **Programs that run a command**, each with every option it takes listed: `sudo`, `doas`, `env`,
  `nohup`, `setsid`, `nice`, `ionice`, `timeout`, `time`, `command` (`-v`/`-V` run nothing),
  `builtin`, `exec`,
  `stdbuf`, `chrt`, `taskset`, `unbuffer`, `busybox`, `xargs` (it appends its input: the command
  gets a dynamic last word); the shells `sh`, `bash`, `dash`, `zsh`, `ksh`, ... with `-c TEXT`;
  `eval` of static words; `find ... -exec/-execdir/-ok/-okdir PROGRAM ... ;` (or `{} +`), where
  find's `{}` in an argument is dynamic. The command a wrapper or `-exec` runs is a program
  and its arguments, with no shell syntax (`nohup for x` runs a program named `for`), except
  `NAME=value` words for `env` and `sudo`, and `time`, which is read both ways (bash's keyword,
  or `/usr/bin/time`). Every program that runs is matched, the wrapper itself too: a check may
  name `sudo` or `bash`. Option values follow each tool's own rules: getopt's
  (`-uroot`, `-nu root`) for the wrappers, the shell's for `sh`/`bash` (each valued letter of a
  cluster takes the next word: `bash -euo pipefail -c TEXT`, `bash -oc pipefail TEXT`). Their command is parsed again, 3 levels deep at most.

The command match then looks at each simple command: its program (basename) must be `program`,
and every word of `args` must appear in order (not necessarily adjacent), and, when `flags` is not
empty, one of the flags. A flag `-f` also matches inside a cluster of short flags (`-fu`), a long
flag `--force` also matches `--force=...` and the prefixes `getopt_long` accepts (`--forc`, at
least two letters). A dynamic word makes the command "cannot verify" only when it could hide what
the check looks for (a required word or flag not found otherwise). Example: with a force-push
check, `cd $HOME && ls` is not a match (`cd` and `ls` are not `git`); `git push $FLAGS` is
"cannot verify".

**Anything Argus cannot read is a match** ("cannot verify"), Mandate's rule: a missing field, a
field that is not a string, a relative path (Argus does not know the directory Hermes resolves it
against), a NUL byte, and every opaque part of a command text. The opaque classes:

| class | examples |
|---|---|
| a lexical form outside the allowlist (the whole text is opaque) | unbalanced quotes; `$(...)`, backticks, `$((...))`, `$[...]`; process substitution `<(...)`; here-documents and here-strings; `$'...'` and `$"..."` quoting; `${...}` other than `${NAME}` (`${X:-y}`, `${#X}`); any other `$` form; a `(` inside a word (`a=(...)`, `-@(f)`); `((...))`; the case fall-throughs `;&` and `;;&`; a redirection without a target; nesting deeper than 3 |
| a dynamic word in program position | `$CMD push -f`, `${GIT} push`, `/usr/bin/gi* push`, `gi{t,} push` |
| a dynamic word where it decides what runs | among a wrapper's options or as an option's value (`sudo -u $U ...`, `timeout $T ...`), among env's `NAME=value` arguments (env's arguments are word-split), anywhere in find's expression (it could add an `-exec`) |
| an option of a wrapper or shell that is not listed (including long-option prefixes) | `sudo -X`, `sudo --us=root`, `nice --5`, `env -P` |
| options left out on purpose | `env -S` / `--split-string` (env's own quoting and escapes), `sudo -s` / `-i` / `--shell` / `--login` and `doas -s` (a shell runs the text), `xargs -I` / `-i` / `--replace` (the replace string can name the program), `sh -s` |
| find's `{}` as the program, or an `-exec` without program or terminator | `find /x -exec {} push -f \;`, `find . -exec ./{} ...` |
| a shell or `eval`/`source` with dynamic or missing text | `bash -c "$X"`, `eval $X`, `source $F`, `bash -c` without a script, `... \| sh` (a shell reading stdin) |
| an interpreter (`python`, `node`, `perl`, `ruby`, `php`, `lua`, `deno`, `bun`, `pwsh`, ...) not run as `INTERPRETER SCRIPT ...` or `python -m MODULE ...`: any option before the script (inline code such as `-c`, `-cCODE`, `-e`, `--eval` is not told apart from other options), an expansion as the script, `deno eval`, no script (stdin) | `python3 -c '...'`, `python3 -W error -c '...'`, `perl -we '...'`, `python3 $X`, `... \| python3`, `python3 - < x.py` |
| a program that runs a command Argus does not parse out of its arguments | `su`, `runuser`, `pkexec`, `chroot`, `nsenter`, `unshare`, `watch`, `flock`, `parallel`, `script`, `strace`, `gdb`, `systemd-run`, `trap`, `coproc`, `expect`, `fakeroot`, `firejail`, `bwrap`; the builtins that make a later name run something else: `hash` (`-p FILE NAME`), `alias`, `enable` |

Only the programs listed in this section are seen into. Every other program is a plain command,
matched by its name: what it runs on its own (section 10, indirect execution) is not seen.

Each check has a canonical text (`json.dumps(sort_keys=True, separators=(",", ":"))`) and a
sha256 of that text. The parser rejects a value longer than its limit, an unknown key, an empty
tool or field, a relative `under`, a `program` with a `/`.

## 3. Drafting (the model may draft, never approve)

`derive` has a new last stage, `checks` (`hermes argus derive --stage checks`). For every proposal
whose current decision is `accept` (by a person, or by the auto_apply policy) and that has no
draft yet, it makes **one model call** (`argus.check`, prompt `prompts.CHECK_SYS`) with:

- the rule, its why and its skill;
- the proposal's evidence: titles of the intents it cites, the error signatures and the tools that
  produced them;
- the tool names this profile actually called (from `tool_outcome`), so the draft names real tools.

The answer is parsed into the closed format, or recorded as not checkable with the model's or the
parser's reason (`check_draft`, layer 2: it can be dropped and recomputed). A rule is drafted once;
`hermes argus checks draft ID` drafts it again on request. The call is counted like the others:
`hermes argus status` and backfill print it in the estimate of the next derive (`checks` in
`derive.Estimate`). The drafting step never writes an approval.

## 4. Approval (only a person)

A draft does nothing until a person approves it. Approval, and every later mode change, is a
judgement (layer 3, `check_approval`, append-only: the latest row per rule is in force):

- **CLI**: `hermes argus checks approve ID` prints the check and its sha256 and asks the person to
  type the first 8 characters of that hash. It refuses when stdin or stdout is not a terminal;
  when the environment carries a Hermes session marker (`HERMES_SESSION_ID`, `HERMES_SESSION_KEY`,
  `HERMES_CRON_SESSION`, `HERMES_KANBAN_TASK`, which Hermes bridges into every command its
  terminal tool runs, or `HERMES_RPC_SOCKET`, which `execute_code` gives its sandbox after
  scrubbing the others); or (Linux) when any parent process carries one, so `env -u ...` inside
  the agent's terminal does not hide it: every parent up to pid 1 is read, however deep the chain.
  A process whose parent exited is re-parented (to init or a subreaper) and leaves the marked
  chain: that is the "detached process" limit of section 10. On Linux the environment read is the
  one each process was started with (`/proc/<pid>/environ`). The same gate guards `enforce`,
  `observe` and `revoke`.
- **Dashboard**: the check block under the rule has approve / enforce / observe / revoke buttons
  behind a confirmation dialog that shows the check and its hash. The request carries the sha256
  the page showed; the backend refuses when it is not the current one (the draft changed since).
  The dashboard API is protected by Hermes' dashboard session token (or its login).

The gate holds at the mutation boundary, not only in the CLI and the route. `checks.approve` and
`checks.set_mode`, the only functions that create or change approvals, and the row writer they
share (`checks._insert`, the only statement that writes `check_approval`), take as their decider
a `checks.Person` proof, nothing else (a string like `"cli"` is refused), and each checks it
itself. A rule's decision turns its check on and off too (a check is in force only while its rule
is accepted, below), so `checks.record_decision`, the only function that writes or clears a
person's decision, takes the same proof and checks it the same way: the dashboard's `/decision`
route (accept, dismiss, keep, reject, clear) is gated exactly like `/check`, and an unauthorized
request can neither dismiss a rule to switch its enforced check off nor accept it again to switch
it back on. A proof is minted only by:

- `checks.at_terminal()`, which runs the whole CLI gate above;
- `checks.on_dashboard(request)`, called by the `/check` and `/decision` routes with their own
  request object: Hermes' dashboard server running in this process must authorize that request
  with the guard of its own
  sensitive routes (`web_server._require_token`: the session token, or the gated login), and the
  process must run outside any Hermes session. In a process that does not serve the dashboard
  (an agent's script, cron) the guard is absent and the proof is refused.

A dashboard proof carries the request it was made from. `approve`, `set_mode`, `record_decision`
and the row writer check that the proof was minted by Argus and run its whole gate again
themselves (the row records the proof's kind as `decided_by`, whatever the caller wrote): for a
CLI proof, the TTY and the session markers; for a dashboard proof, Hermes' authorization of the
request it carries and the session markers. So a proof does not outlive its gate, and a proof
built by other code (with no request Hermes authorized) is refused in any process that is not
serving the dashboard.

The approval row stores the rule text, the check's canonical text, its sha256, a version (1, 2,
...: a new spec approved for the same rule; re-approving the same spec keeps its version), the
mode, when and by whom (`cli` | `dashboard`). A new draft never changes an approved check: the
approved text is a copy. When a draft differs from the approved check, the dashboard and the CLI
say so; the person approves the new version or not.

A check is **enforced at all** only when its latest approval row is not `revoked`, the rule's
current decision is still `accept` (otherwise the check is *suspended*), and the sha256 of the
stored text equals the approved sha256. The hash covers the check's text, and the text names its
tool: a row whose `tool` column differs from it counts as a mismatch too, also for a text in a
format this version cannot evaluate (its JSON `tool` field is compared; a text that is not a JSON
object with a `tool` is a mismatch). A hash mismatch means the stored check was changed
without an approval (a hand edit of the DB, a bug): the check is **not evaluated**, the mismatch is
logged and recorded once per process (`check_event` outcome `unverified`), and the CLI and the
dashboard show it as "hash mismatch: not enforced" until a person approves again. The one
exception is the unreadable-DB fallback of section 8: a process that cannot read the DB at all
blocks the tools of the last verified state written to the sidecar, which a mismatch nobody could
read (or record) yet does not change.

## 5. Modes

| mode | on a matching call |
|---|---|
| `observe` (default after approve) | the call runs; a `would_block` event is recorded |
| `enforce` | the call is blocked; a `blocked` event is recorded |
| `revoked` | the check is not evaluated |

Observe first: the person sees what the check would have blocked (`hermes argus checks show ID`,
the dashboard) before switching it to enforce. Events (`check_event`, layer 1) carry the session,
tool call, tool and what matched (cut to 300 characters).

## 6. The block

Argus answers `pre_tool_call` with `{"action": "block", "message": ...}`. Hermes 0.23 makes that
the tool's result (`{"error": message}`, `post_tool_call` status `blocked`), in an interactive
session, in the gateway, in cron and in YOLO alike: YOLO only skips approval prompts, and a block
is not one. Argus never answers `approve`: that escalates to a human prompt which YOLO
auto-approves and cron resolves by `approvals.cron_mode`, so it would not be deterministic. When
several plugins answer, Hermes takes any valid block over every approve; among blocks, the first
in registration order supplies the message.

The message names the rule, the check id and version, what matched, and how a person lifts it:

```
Argus blocked this terminal call: check 3f2a9c1e0b7d4a66 v1 (terminal.command runs `git push`
with --force|-f) of the accepted rule "Never force-push to shared branches." (skill git-flow)
matches: git push origin main --force. A person approved this check in enforce mode; do not try to
reach the same effect another way. To lift it, a person runs `hermes argus checks observe
3f2a9c1e0b7d4a66` (record only) or `hermes argus checks revoke 3f2a9c1e0b7d4a66`, or uses the Argus
dashboard tab.
```

For "cannot verify" it says why and how to write a call Argus can read (an absolute path, no
`$(...)`).

## 7. Hot path

The hook makes no model call and no network call. Per tool call it does one `stat` of the plugin
DB, takes a lock, and asks a long-lived read-only SQLite connection for `pragma data_version`
(a counter that changes when another connection commits). Only when it changed does it re-read
the approvals (one small query) and, after such a re-read, reads the small sidecar file and
rewrites it when its verified enforced set changed (section 8). Then it evaluates only the checks
of this tool. Events go to the
existing asynchronous writer queue (`store.Writer`), never written on the hook's thread. A missing
plugin DB (with no sidecar), or one whose schema predates checks, means no checks. Only the DB's
meta `schema_version` (below 3) proves that a schema predates checks: a check table missing from a
DB of a later or unknown version is an unreadable DB (section 8), not "no checks".

## 8. Failure semantics

Hermes 0.23 fails **closed** when a `pre_tool_call` callback raises or exceeds
`plugins.hook_callback_timeout`: it blocks the call. A raising Argus hook would therefore block
every tool call of the agent. Argus picks this instead:

| failure | behaviour | why |
|---|---|---|
| evaluating an **enforced** check raises | block, saying Argus failed on that check | an error inside an enforced check must never silently allow |
| evaluating an **observed** check raises | allow; `error` event | observe never changes what runs |
| the argument cannot be read (section 2) | a match: enforce blocks ("cannot verify"), observe records | what Argus cannot read is denied |
| the stored check text does not parse (written by a newer Argus) but its hash matches and its JSON `tool` equals the row's `tool` column | treated as an enforced/observed check that raised, for that tool | same as above, scoped to that tool |
| the hash does not match | not evaluated; `unverified` event once per process; shown as such | the task's rule: a check is enforced only as approved |
| the approvals cannot be read (locked past 2 s, corrupt, I/O error, a row that does not decode, a check table missing from a DB whose `schema_version` is not one from before checks) | the last snapshot this process read is used; with none, the tools listed in the sidecar `plugin-data/argus/enforced.json` are blocked with a message saying Argus cannot read its checks; every other tool is allowed. The sidecar lists only verified enforced checks: it is written with every approval change (before it commits) and rewritten by the hook whenever a re-read of the approvals finds a different verified set (a check unverified, revoked or suspended since); a read that fails never rewrites it. It reflects the last verified state Argus could write: a failed rewrite is logged and retried on every call. What remains: a row changed, then the DB made unreadable before any process re-read it (or while the sidecar cannot be written, e.g. a read-only directory), still blocks the tools of the last state written. This errs closed on purpose: without the DB, Argus cannot tell a changed row from a broken read, and the sidecar only ever lists checks a person enforced and Argus verified | fail closed only where a person enforced a check; a broken plugin DB must not brick every tool call, including the ones a person needs to repair it |
| the sidecar is missing or unreadable too, or any other unexpected error in the hook | allow, log a warning (once a minute) | there is no evidence that anything is enforced; blocking everything would brick the agent |
| recording an event fails (including the `unverified` event of a hash mismatch) | logged; the decision stands | events are evidence, not the decision |
| an unexpected error after the approvals were read | the last snapshot's enforced checks on that tool block, and only those whose hash matches; otherwise allow | a check whose mismatch Argus has read is never enforced, on any path (for the unreadable-DB sidecar, see the row above) |

The hook never raises into Hermes: every path above returns a decision.

## 9. Auto-apply

The auto_apply policy may accept and apply a rule to SKILL.md; it never approves a check. Its
rules are drafted like a person's accepted rules (a draft is inert), and become enforced only
through the person-only gate of section 4. It never accepts a rule that has a check approval (of
any mode): such a rule was accepted by a person before, and accepting it again after a person
cleared that decision would turn the check back on. Nor does it undo its own accept after a failed
apply once the rule has a check approval (a person approved it meanwhile): that would suspend it.

## 10. Threat model and known limits

Argus enforcement raises the cost of a known bad call; **it is not a sandbox**.

- **Same user, same files.** The agent runs as the same OS user as Argus. Through its terminal it
  can edit `plugin-data/argus/data.db` (approvals, modes), the sidecar, Hermes' `config.yaml`
  (disable the plugin), or Argus' code. The sha256 detects a changed check text whose hash was not
  updated; it does not authenticate who wrote the row (there is no secret the agent cannot read).
  The person gate (TTY, session markers, parent environments, a proof minted by the gate and
  checked again by the functions that write approvals) stops the agent from approving through
  `hermes argus` or Argus' own functions by accident or naively. Code the agent writes on
  purpose can still pass it: a process it starts outside every marked process (detached, with a
  pseudo-terminal), a call to the dashboard API with a session token it read, code that imports
  Hermes' dashboard server into its own process and authorizes a request of its own with it, or
  code that writes the plugin DB directly. The markers are environment variables: a Hermes
  version that stops setting them (or a process tree Argus cannot read without `/proc`) weakens
  the gate to the TTY and, for the dashboard, Hermes' authorization of the request.
- **Indirect execution.** A check sees one argument of one call. `execute_code` running Python
  (`subprocess`, `os.system`), a script or Makefile the agent wrote first (`make deploy`,
  `./run.sh`, `bash script.sh`), `source FILE`, an alias or function defined in a file it
  sources, a binary under another name or a
  symlink to it, a command assembled at run time, any program outside section 2's list that runs
  a command from its arguments or configuration (`ssh host '...'`, `nix develop -c`, `uv run`,
  `rsync -e`, `awk` with `system()`, `vim -c`, `git` aliases and `git -c`), code a program reads
  from its environment (`PS4`, `BASH_ENV`, `GIT_SSH_COMMAND`, `LD_PRELOAD`): none of them is
  visible as `git push --force`. Calls
  made by `execute_code` through Hermes' tool RPC do pass through `pre_tool_call`.
- **Paths.** The `path` match resolves symlinks at check time; a symlink created after the check
  (time-of-check/time-of-use) or a hard link is not seen. Paths compare case-sensitively and
  without Unicode normalisation: on a case-insensitive filesystem (macOS by default) `~/.SSH` is
  not under `~/.ssh` for Argus. `~` is the home of the Hermes process. On
  docker/ssh/modal terminal backends, paths and commands run elsewhere: only the lexical comparison
  is meaningful there.
- **Other plugins.** A plugin's `modify` directive changes the arguments after every
  `pre_tool_call` hook saw the original ones; Argus does not see the final call.
- **Hermes modes.** Hermes runs no plugin hooks in its safe-worker and `HERMES_SAFE_MODE` modes, and
  none for a disabled plugin.
- **Prompt injection into the draft.** The draft is made from text the agent and tools produced
  (intent titles from user messages, error messages from tool output). A crafted text can steer the
  draft: a check that matches nothing, or one that blocks harmless calls. The format bounds what a
  draft can be; a person reads it before approving; observe mode shows its effect before enforce.
  A draft cannot approve itself and cannot change an approved check.
- **Over-blocking.** "Cannot verify" blocks commands Argus cannot parse even when they are
  harmless (`git commit -m "$(cat msg)"` under a git check). This is deliberate; observe mode
  shows how often it happens before a person enforces.
- **Names.** A check names a tool exactly; an MCP tool or a plugin tool that does the same thing
  under another name is not covered.
