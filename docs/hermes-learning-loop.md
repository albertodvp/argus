# How Hermes learns: the parts Argus reads

What Hermes does when it writes, reviews and curates its own skills, and what of that reaches the
ledger Argus reads. Checked against Hermes 0.22.7 (commit `b200f5b`); paths are relative to the
hermes-agent repository and move between versions.

## Who writes a skill

| Path | Who decides | When | Ledger actor | `created_by` of a new skill |
|---|---|---|---|---|
| Foreground | the model, from system-prompt guidance | during the task | `agent` | `learn`: not curated |
| Background review | the same model in a fork of the turn | after the reply, when a nudge counter is due; `/refine` | `curator` | `agent`: curated |
| Curator | the curator's model (`auxiliary.curator`) | idle pass, at most every `interval_hours` | `curator` | `agent`: curated |
| A person | `hermes curator rollback`, `hermes curator purge`, an Argus apply | by hand | `user` | — |

**Foreground.** No threshold decides that a task deserves a skill. The guidance is text:
`SKILLS_GUIDANCE` ("When you work out a non-trivial workflow, record it with skill_manage for future
reuse.", `agent/prompt_builder.py`, only when `skill_manage` is available) and the Skills section
("After difficult/iterative tasks, offer to save as a skill. If a skill you loaded was missing steps,
had wrong commands, or needed pitfalls you discovered, update it before finishing."). The older "5+
tool calls" sentence survives only in a comment: it was removed because a provider filter rejected
it. One-shot `-q` sessions get a load-only variant without the "offer to save" line.

## When the background review runs

Counters decide when to ask; the model decides whether and what to save.

| Counter | Config | Counts | Reset |
|---|---|---|---|
| skill nudge | `skills.creation_nudge_interval`, 10 in code (`cli-config.yaml.example` says 15) | agent-loop iterations (API calls) while `skill_manage` is available, including the final text-only one; carried across turns | any `skill_manage` call, or the review it triggers |
| memory nudge | `memory.nudge_interval`, 10 | user turns (seeded on resume) | any `memory` call |

Both are checked at the end of a turn (`agent/turn_finalizer.py`); when both are due, one combined
review runs. `/refine [focus]` runs the review on demand, focused on the given text, past `enabled`
and the idle deferral.

The review does not run for cron jobs, managed or kanban workers, subagents (`_delegate_depth > 0`),
finite gateway runs (`chat -q` / `-z`), interrupted turns, turns without a final reply or vetoed by a
`pre_llm_call` hook, or with `enabled: false`.

When it would run on Hermes' own managed llama-server (`auxiliary.background_review.defer: auto`),
it waits until the server has been idle 15 s, at most `defer_max_age_s` (30 min); one review per
session is queued. Any other endpoint, local or not, runs it at once. A live turn always comes
first: it cancels a running review (waiting at most 2 s), and a cancelled review is queued again at
most 3 times.

## What the review can do

- **Fork.** A new `AIAgent` (`build_cache_parity_fork`, `agent/background_review.py`) with the
  turn's session id, system prompt and tool list, so the prompt cache stays warm. Its messages are
  not persisted. The model is `auxiliary.background_review` (`auto`: the agent's own); a different
  model gets a digest of the last 24 messages instead.
- **Tools.** Calls are allowed only to the `skills` toolset, `read_file`, `search_files` (plus
  `auxiliary.background_review.extra_tools`), and to `memory` only when the memory nudge fired.
  `terminal`, `write_file` and `patch` are refused. The full tool list is still advertised, for the
  cache.
- **Limits.** 16 iterations; an input budget of 75 % of the context window, at most 600k tokens.
- **Guards.** A skill must be read in this review before it is patched; a locked skill is refused;
  the review may delete only curator-owned skills; dangerous commands are denied.
- **Prompt.** "Be ACTIVE — most sessions produce at least one skill update, even if small. A pass that
  does nothing is a missed learning opportunity, not a neutral outcome." Order of preference: update
  a skill loaded in the turn, update an existing umbrella, add a support file (`references/`,
  `templates/`, `scripts/`), create a new class-level umbrella skill. "'Nothing to save.' is a real
  option but should NOT be the default."

## The curator

- **Schedule.** `curator.interval_hours` 168 (7 days), `min_idle_hours` 2, `stale_after_days` 14,
  `archive_after_days` 30. The first time it sees a home it only records the time. The gateway's
  housekeeping and CLI start call it as fully idle; only the dashboard's web server measures idle
  time.
- **Scope.** Only skills with `created_by: agent`, i.e. created by the background review or by the
  curator itself; a skill created in a foreground turn is not curated unless `hermes curator adopt`
  hands it over. Bundled skills only with `prune_builtins`; hub-installed and external skills never.
- **Passes.** A deterministic prune always (`active` → `stale` → `archived` by last use, view or
  patch). With `curator.consolidate: true` (off by default) also a model pass in a fork limited to the
  `skills` toolset, after a tarball snapshot; its prompt pushes towards consolidation. Skills a cron
  job references are protected, and the references are rewritten after a merge.
- **Never deletes** in an automatic run; `hermes curator purge` can, for archived skills past
  `archive_ttl_days`.

## The ledger

`skills/.curator_ledger.jsonl`, one entry per mutation: `id, ts, actor, action, skill, evidence,
before, after` (`tools/skill_ledger.py`); file contents are stored once under
`.curator_backups/blobs/`. `hermes curator rollback <id>` restores one entry and appends a `rollback`
entry with `rollback_target`, refused while a later change to the same file exists.

- **Actor.** One of `agent`, `curator`, `user`. An explicit actor wins (the CLI sets `user`, automatic
  archiving `curator`); otherwise a write from the background review is `curator` and any other
  `agent`. The curator's model pass also runs as a background review, so the ledger cannot tell the
  review from the curator.
- **Evidence.** `session_id` and `file_path` of a `skill_manage` write (`absorbed_into` / `archived`
  on a delete); no write origin, task or model. The review's entries carry the turn's session id;
  the curator's fork is not persisted, so its session has no messages in `state.db`. Argus uses that
  difference to label an entry `review` or `curator` (`export.writer_of`).
- **`on_skill_lifecycle`** (`tools/skill_usage.py`) is the only skill hook a plugin gets: `action`
  (loaded, created, patched, edited, installed, archived, stale, restored), `skill_name`,
  `provenance` (installed, agent_created, external, local, unknown: where the skill came from, not
  who wrote this edit), `task_id`, `session_id`, `use_count`, `reused`, `reuse_after_patch`. There is
  no hook for the review or the curator as such.

## Guards a person controls

- `hermes curator lock <skill>` refuses every write to the skill (`skill_manage`, `write_file`,
  `patch`, terminal writes) from turns, the review and the curator alike, until the person unlocks
  it. A pinned skill can be patched but is never archived or deleted automatically.
- `skills.write_approval: true` stages every skill write, from turns and the review, under
  `pending/skills/<id>.json` until `/skills approve`.

## Outside the running agent

[hermes-agent-self-evolution](https://github.com/NousResearch/hermes-agent-self-evolution) evolves a
`SKILL.md` with DSPy + GEPA against an evaluation set (synthetic or from session history), behind
tests and size limits, and proposes the result as a pull request for human review. It is a separate
project; nothing of it runs inside Hermes.
