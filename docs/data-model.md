# Data model

Schema: [`argus/schema.sql`](../argus/schema.sql). One SQLite file per Hermes profile, written by
the Argus plugin and opened with `plugin_db("argus")` (`<HERMES_HOME>/plugin-data/argus/data.db`,
WAL). It survives `hermes plugins update` and `remove`, and follows the active profile.

## Read, don't copy

Conversations stay in Hermes. The plugin reads them, read-only, from the profile's `state.db`:

| Needed | state.db |
|---|---|
| sessions, profile, source (cron, subagent, ...), parent, start/end, end reason, tokens, cost | `sessions` (`profile_name`, `source`, `parent_session_id`, `end_reason`, `origin_json`, ...) |
| user messages, replies, tool calls and results, full text | `messages` (`role`, `content`, `tool_calls`, `tool_call_id`, `tool_name`, `finish_reason`, `timestamp`) |
| cron runs | `sessions.source = 'cron'`, `worker_executions` |
| skill edits, before/after content | `~/.hermes/skills/.curator_ledger.jsonl` + blob store |

Checked on live state.db files (Hermes 0.22.5 to 0.23.0, November 2026). Neither state.db nor the
ledger is a documented Hermes API: the plugin records the state.db schema it last saw in `meta`,
checks the columns it reads on open, and stops with a clear message when they are missing.

Consequences: no second copy of customer text, and the plugin file stays small. Derived rows still
summarise what they were computed from (intent titles, judgements, proposal rules), so the plugin
listens to `on_session_delete` and clears the layer-1 and layer-2 rows of a deleted session; the
next derivation pass rebuilds intents without it. Its skill edits stay, unlinked from the session
with their review, since the ledger entry and the change outlive it. Archiving
(`reason = "archive"`) keeps the messages in state.db, so archived sessions keep their plugin rows.

### History window

`meta.window_start` (epoch seconds) bounds what a profile reads from state.db and the ledger.

- **Set:** a profile's first `hermes argus backfill` sets it to now minus `since_days` days (setting,
  default 30; 0 sets none) or, given `--since DAYS`, to now minus DAYS. An explicit `--since` also
  sets it on a profile backfilled before without one (an older Argus version); the setting does not.
- **Widen:** a later `--since` asking for an earlier start widens it, and backfill re-reads from the
  new start (its cursor only covers what it read before). `--since 0` stores `0`: the whole history.
- **Never narrow:** a `--since` asking for a later start is reported (`window kept from …` or
  `whole history kept`) and not applied. A backfill without `--since` changes nothing and says
  nothing. An absent key means the whole history.
- **No backfill, no derive:** derive skips a profile with neither a window nor a backfill cursor
  (report `skipped`), so the idle trigger and the dashboard's refresh never derive a long history
  before a backfill bounded it.

Derive and export load the sessions *active* in the window: started at or after its start, or with
a message at or after it (a resumed CLI session, a long gateway chat), and of those only the
messages from the start on (`hermes_data.load_dump(home, since=…)`, one index seek per session on
`messages(session_id, timestamp)`); ledger entries from the start on. Backfill filters tool messages
by the same message timestamp. So:

- assign sees only the window's turns and offers only intents with turns in it as candidates;
  consolidate, judge, runs and proposals see only intents with turns in it, judged on those turns;
- runs of sessions outside the window are left as they are (not removed as stale);
- skill edits before the window stay unreviewed until a wider window includes them, and so do
  edits inside it whose session has messages only before the window (edit_review `skipped`):
  reviewed without their run they would read as speculative. An edit whose session has no messages
  in state.db at all is labelled `no-run` (see "Edit review" below);
- the dashboard shows only the window's intents, sessions, skill reads and edits, and its counts
  (`GET status`, the Skills banner, `/argus`) follow it: speculative edits by `skill_edit.ts`, intents
  by a `run` started in the window. Rows of older history stay in the plugin DB and come back when
  the window widens.

## Three layers in the plugin file

| Layer | Tables | Written by | Lifetime |
|---|---|---|---|
| 1 captured | `tool_outcome`, `turn_outcome`, `skill_edit` | hooks (live) and `hermes argus backfill` | append-only; only what state.db lacks |
| 2 derived | `intent`, `turn_intent`, `run`, `proposal`, plus the derived columns of layer 1 | derivation passes, deterministic or `ctx.llm` | every value carries `derived_by` (stage and version, e.g. `edit_review/v2`). A stage redoes rows of an older version only where it says so: edit_review v2 relabels v1 rows by its rules without model calls; the other stages keep theirs |
| 3 judgments | `decision`, `apply_log` | dashboard, slash commands; `hermes argus apply` (CLI or the dashboard's apply button) | never recomputed; `decision` keeps a snapshot of what was decided on, `apply_log` one row per apply attempt |

Derived data that is 1:1 with a captured row is a column on it (`tool_outcome.error_sig`,
`skill_edit.evidence`); derived data with an identity of its own is a table.

### Applying a decision

`hermes argus apply` (current profile) and the dashboard's apply button (`apply_child.py`, run with
HERMES_HOME = the decision's profile, so also where the plugin is not enabled) carry out a decision
through Hermes, with ledger actor `user`, one apply per profile at a time (`apply.lock`). The target's
current decision must be reject / accept, re-read under the lock (`--force` overrides it from the CLI and
is recorded in `applied_by`; `--dry-run` needs no decision and writes nothing).

- `--rollback <entry>` (a rejected edit) calls `tools.skill_ledger.rollback_entry`, a plain restore (no
  curator lock, no write approval), only while every file of the entry still hashes to the entry's
  after-state; otherwise it is refused and names the newer ledger entries to undo first.
- `--proposal <id>` (an accepted proposal) turns the stored diff into `old_string`/`new_string` and calls
  `skill_manage(action="patch")`. Refused if the rule is already in the file, if the diff's context is
  not in SKILL.md exactly once (the file changed since the proposal: re-run derive), or if the same patch
  is already pending. Hermes refuses a locked skill and, with `skills.write_approval` on, stages the
  write (outcome `staged`); the ledger entry written on `/skills approve` carries the approver's actor.

**Auto-apply** (`argus/auto.py`, setting `auto_apply`, off by default) runs at the end of a derive
pass of the profile whose setting enables it, under that profile's derive.lock (one pass at a time),
and writes the same two rows a person's accept and apply write, with `decision.decided_by` and
`apply_log.applied_by` = `auto` (`decision.note` = the policy version, `auto/v1`), then calls the
same `apply.run`. It considers only the proposals of the latest proposals pass (meta
`proposals_last_ids`, written by the proposals stage), never one with a decision or an apply
attempt, nor one citing an intent that a decided or attempted proposal for the same skill cites (the
same lesson, reworded). The decision is inserted only while the proposal has no decision and no
attempt (one statement). When the apply is refused or fails it deletes its own decision again, so
the proposal returns to the person's queue; the attempt stays in `apply_log` and is not repeated. An
`unknown` outcome keeps the decision, counts against the budget and ends the pass. A ledger
`rollback` of any change to the skill made since auto-apply first wrote or staged one there (read
from the ledger, so a staged write approved later counts; from an hour before the attempt's record)
stops auto-apply on that skill for good. The proposals stage lists the rules Argus applied, staged
or may have written next to the harness' edits (under their ledger entry, or `argus-<proposal id>`),
so it does not propose them again reworded, and may cite them as overlaps.

Each attempt (not `--dry-run`) is a row in `apply_log` (`outcome` applied | staged | refused | failed |
unknown, Hermes' message, the ledger entry it wrote, the pending id of a staged write); applied and
staged ones also fill `executed_at`, `execution_result` and `ledger_entry_id` of the target's latest
matching decision. `unknown`: the dashboard stopped waiting (the child refuses to start a write within
5 s of its deadline), or skill_manage reported a patch but no new ledger entry names the file apply
checked (it wrote another file, or the ledger is off): the decision stays unexecuted. The export shows per target the latest success (`applied`: applied, or staged
while `pending/skills/<id>.json` exists) and the latest attempt (`last_attempt`). A staged proposal
whose pending write is gone was approved or rejected; the ledger does not name the pending id, so the
export looks for the proposal's write (a `patch` whose `SKILL.md` gains its new text, or a `create`
of its `SKILL.md`) at or after the attempt, in the history window. Found, it is the latest success
(and the latest attempt, if it was), as `applied` with that ledger entry, hence its rollback; not
found (rejected), an earlier success stands, if any. `SCHEMA_VERSION` 2
adds the table; `store.open_db` re-runs the idempotent `schema.sql` on an older file, so existing
databases upgrade in place.

### What hooks capture

| Table | Hook | Why state.db is not enough |
|---|---|---|
| `tool_outcome` | `post_tool_call` (`status`, `error_type`, `error_message`, `duration_ms`) | state.db keeps the result text, not whether the call failed or how long it took |
| `turn_outcome` | `on_session_end` (`turn_exit_reason`, `completed`, `interrupted`, `failed`), `agent_loop_stopped` | state.db has a per-message `finish_reason`, not why the turn ended |
| `skill_edit` | `on_skill_lifecycle` (`provenance`), `post_tool_call` on `skill_manage` (session, turn, tool call) | the ledger does not name the turn or the tool call |

`provenance` is where the skill came from (`installed`, `agent_created`, `external`, `local`,
`unknown`), not who wrote the edit. Who wrote it comes from the ledger actor (`agent`, `curator`,
`user`); Hermes writes `curator` both for the background review and for the curator, and the export
splits it (`writer`): `review` when the entry's session has messages in state.db (the review runs in
the turn's session), else `curator`.

Backfill fills `tool_outcome` from tool messages in state.db (status and error signature inferred
from the result text, no duration) and `skill_edit` from the ledger (no provenance). `origin` says
which path wrote a row.

Hooks never call a model. They append through an in-process queue drained by one writer thread.
Model work (intents, outcomes, edit review, proposals) runs later on sessions without a current
derivation: on demand, from `hermes argus derive`, or from an automation blueprint.

### Edit review

`edit_review` fills `skill_edit.evidence`, `learned` and `trigger`. An edit is judged only on a run
state.db holds: the model reads its diff and the run window before it (`observed-failure`,
`observed-success`, `user-correction`, `speculative`). An edit whose session has no messages in
state.db (a curator pass, a deleted session, no session) gets `no-run` and only `learned`, from the
diff alone. A cleanup without a diff and a person's edit (actor `user`, a `rollback` entry) are not
reviewed: all three columns stay empty. Version 1 judged edits without a run as well, so most of them
read `speculative`; v2 relabels v1 rows by these rules, keeping the model's labels where the run
exists, without model calls.

## Decisions

- **Intents are per profile.** Each profile has its own state.db and its own plugin data; a goal
  pursued in two profiles (for example operator and production) is two intents.
- **Outcomes keep both kinds of signal**: `run.signal_status` comes from evidence only;
  `run.model_ok` and the judgement columns of `intent` are model output and are labelled as such.
- **SQLite, not DuckDB.** Measured on 1M tool calls (about two years of a busy instance): dashboard
  queries 1-17 ms in SQLite vs 1-8 ms in DuckDB, but a hook insert costs 0.07 ms in SQLite vs 11 ms
  in DuckDB, and DuckDB refuses a write from a second process while the file is open. Hermes fires
  hooks in several processes (gateway, `hermes chat`, cron, kanban workers).

## Not modelled

- **Whether a change helped.** Before/after comparisons of runs are not causal: Hermes keeps editing
  the same skill, requests differ between runs, outcomes are partly model judgement, samples are
  small. The UI shows before/after counts as description only. Possible later methods: lock the skill
  during a trial (`hermes curator lock`), or replay past requests of an intent against the old and
  new skill in an isolated profile.
- **Skill trend labels** (improving / degrading). They are a judgement of the same kind. The UI's
  "needs review" is a query: share of a skill's edits whose `evidence` is `speculative`, among Hermes'
  own edits (ledger actor other than `user`: an applied proposal neither counts nor dilutes it).
