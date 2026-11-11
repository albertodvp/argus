-- Argus plugin data model, one SQLite file per Hermes profile:
--   plugin_db("argus") -> <HERMES_HOME>/plugin-data/argus/data.db (WAL).
--
-- Conversations are NOT copied. Sessions, messages, tool calls and their text are read from
-- Hermes' own state.db (read-only); skill edits from the curator ledger. This file holds only:
--   1 captured   what Hermes does not persist, recorded by hooks, keyed by Hermes ids
--   2 derived    computed from state.db + layer 1; nullable columns or rows that carry derived_by
--                and can be cleared and recomputed
--   3 judgments  what a person decided; never recomputed
-- Rule: derived data that is 1:1 with a captured row is a column on that row; derived data with an
-- identity of its own (intent, run, proposal) is a table.

pragma journal_mode = wal;
pragma foreign_keys = on;

create table if not exists meta (
  key   text primary key,                -- schema_version, backfill cursor, state.db schema seen
  value text not null
);

-- ---------------------------------------------------------------- 1 captured

-- result of one tool call; joins state.db messages.tool_call_id
create table if not exists tool_outcome (
  tool_call_id   text primary key,
  session_id     text not null,           -- state.db sessions.id
  ts             real not null,
  tool_name      text not null,
  status         text,                    -- post_tool_call status: ok | error | blocked
  error_type     text,
  error_message  text,                    -- as Hermes reports it (short); the full result stays in state.db
  duration_ms    integer,
  origin         text not null check (origin in ('hook', 'backfill')),
  -- derived, deterministic: normalised error signature for recurring errors
  error_sig      text,
  error_category text,                    -- runtime | policy | env | timeout | input | exit
  derived_by     text
);
create index if not exists tool_outcome_session on tool_outcome(session_id, ts);
create index if not exists tool_outcome_errors on tool_outcome(error_sig) where error_sig is not null;

-- how one turn ended (on_session_end, agent_loop_stopped); not in state.db
create table if not exists turn_outcome (
  turn_id      text primary key,          -- Hermes turn_id
  session_id   text not null,
  started_at   real not null,             -- matched to the state.db user message by session + time
  ended_at     real,
  exit_reason  text,                      -- turn_exit_reason
  completed    integer,
  interrupted  integer,
  failed       integer
);
create index if not exists turn_outcome_session on turn_outcome(session_id, started_at);

-- annotation of one curator-ledger entry: who wrote it and after which turn, plus its review
create table if not exists skill_edit (
  ledger_entry_id text primary key,       -- `hermes curator rollback <id>`
  ts              real not null,
  skill_name      text not null,
  provenance      text,                   -- where the skill came from (on_skill_lifecycle): installed | agent_created | external | local | unknown; not who wrote the edit
  session_id      text,
  turn_id         text,
  tool_call_id    text,
  origin          text not null check (origin in ('hook', 'backfill')),
  -- derived (edit_review): what the edit says and what preceded it
  evidence        text,                   -- model: speculative | user-correction | observed-failure | observed-success; rule: no-run (state.db holds no run for it); null: not reviewed (no diff, a person's edit)
  learned         text,
  trigger         text,
  derived_by      text
);
create index if not exists skill_edit_skill on skill_edit(skill_name, ts);

-- ---------------------------------------------------------------- 2 derived

-- an intent belongs to one profile: each profile has its own state.db and its own plugin data.
-- The same goal pursued in two profiles is two intents.
create table if not exists intent (
  id          text primary key,
  title       text not null,
  goal        text,
  kind        text not null,              -- task | routine (cron) | probe | chat
  who         text,                       -- real-user | team | automated-test | scheduled
  -- model judgement of the intent as a whole (model output, not ground truth)
  status      text,                       -- achieved | partial | failed | unclear
  stable      text,                       -- stable | unstable | single-run
  risk        text,
  headline    text,
  outcome     text,
  consistency text,
  friction    text,
  next        text,
  attention   integer,
  -- lifecycle: the id is stable; title/goal/judgement evolve, merges redirect
  merged_into text references intent(id), -- set when consolidation folded this intent into another
  created_at  real not null,
  updated_at  real not null,              -- last assignment, rename or merge; consolidation re-reads these
  judged_at   real,                       -- judgement is redone when a run starts after this
  derived_by  text not null
);

-- a turn = one user message in state.db and the replies up to the next one
create table if not exists turn_intent (
  session_id      text not null,
  user_message_id integer not null,       -- state.db messages.id of the user message
  intent_id       text not null references intent(id) on delete cascade,
  gate            text not null,          -- follow-up | repeat | cron | llm
  derived_by      text not null,
  primary key (session_id, user_message_id)
);
create index if not exists turn_intent_intent on turn_intent(intent_id);

-- one session's attempt at an intent: deterministic signal and model result side by side
create table if not exists run (
  intent_id     text not null references intent(id) on delete cascade,
  session_id    text not null,
  started_at    real not null,
  signal_status text,                     -- from evidence only: ok | error | interrupted | cron_failure | silent
  model_ok      integer,                  -- model judgement for this run; null when not judged
  model_result  text,
  derived_by    text not null,
  primary key (intent_id, session_id)
);

-- skill edit proposed by this plugin; id = hash(skill, rule) so a decision keeps pointing at
-- the same proposal when its wording or diff is regenerated
create table if not exists proposal (
  id           text primary key,
  skill_name   text not null,
  is_new_skill integer not null default 0,
  file         text,
  rule         text not null,
  why          text,
  diff         text,
  confidence   text,                      -- high | medium | low
  evidence     text not null,             -- JSON: {"intents": [...], "error_sigs": [...], "overlaps": [ledger ids]}
  derived_by   text not null
);

-- ---------------------------------------------------------------- 3 judgments

-- a person's decision on an applied edit (keep/reject) or a proposal (accept/dismiss).
-- target_snapshot keeps what was decided on, so the decision survives a recompute of layer 2.
create table if not exists decision (
  id               integer primary key,
  target_kind      text not null check (target_kind in ('skill_edit', 'proposal')),
  target_id        text not null,         -- ledger_entry_id or proposal id
  action           text not null check (action in ('keep', 'reject', 'accept', 'dismiss')),
  target_snapshot  text not null,         -- JSON: rule, diff, evidence label shown at decision time
  decided_at       real not null,
  decided_by       text,
  note             text,
  executed_at      real,                  -- when the action ran (rollback, skill_manage); null if only recorded
  execution_result text,
  ledger_entry_id  text                   -- ledger entry created by the execution
);
create index if not exists decision_target on decision(target_kind, target_id, decided_at);

-- each `hermes argus apply` (CLI or the dashboard's apply button): the decision carried out through
-- Hermes (rollback_entry for a rejected edit, skill_manage patch for an accepted proposal), actor user.
-- --dry-run is not recorded. The export shows per target the latest successful row (applied, or staged
-- while its pending write still exists) and the latest row of any outcome.
create table if not exists apply_log (
  id           integer primary key,
  target_kind  text not null check (target_kind in ('skill_edit', 'proposal')),
  target_id    text not null,             -- ledger_entry_id or proposal id
  outcome      text not null check (outcome in ('applied', 'staged', 'refused', 'failed', 'unknown')),
                                          -- unknown: the dashboard stopped waiting, or no ledger entry proves the file written; check the ledger
  message      text,                      -- Hermes' message, or why Argus refused
  ledger_entry text,                      -- ledger entry the apply created (the rollback or patch entry)
  pending_id   text,                      -- staged: <home>/pending/skills/<pending_id>.json
  applied_at   real not null,
  applied_by   text                       -- cli | dashboard, `--force` appended when forced
);
create index if not exists apply_log_target on apply_log(target_kind, target_id, applied_at);
