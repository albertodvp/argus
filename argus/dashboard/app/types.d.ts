// Types of the Argus dashboard (type-check only; app.js stays plain JS served as-is).
// Argus.*: the JSON of the plugin routes, mirroring argus/exporttypes.py (same names). Responses are
// asserted to these types where app.js receives them (the same-origin backend that types them in Python).
// Argus.App.*: what app.js derives from it in model().

declare namespace Argus {
  type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

  type DecisionKind = 'skill_edit' | 'proposal';
  type DecisionAction = 'keep' | 'reject' | 'accept' | 'dismiss';
  type ApplyOutcome = 'applied' | 'staged' | 'refused' | 'failed' | 'unknown';
  type RunStatus = 'failed' | 'silent' | 'reported' | 'empty';

  // ---------------------------------------------------------------- /data (export.build)
  interface Turn {
    ts: number;
    sid: string;
    profile: string;
    source: string;
    text: string;
    view: string;
    reply: string;
    tools: number;
    /** tool -> [calls, failed calls] */
    tc: Record<string, [number, number]>;
    /** [tool, error signature, category] */
    errs: [string, string, string][];
    /** runtime signals */
    rt: string[];
    /** seconds */
    dur: number;
    /** category of each runtime signal */
    rtcat: string[];
    /** skill -> explicit reads during the turn */
    sk: Record<string, number>;
  }

  /** A subagent session started during one of the intent's turns. */
  interface Child {
    sid: string;
    title: string;
    start: number;
    tools: number;
  }

  interface RunOutcome {
    ok: boolean;
    result: string;
  }

  interface Judgement {
    status: string;
    who: string;
    stable: string;
    risk: string;
    headline: string;
    outcome: string;
    consistency: string;
    friction: string;
    next: string;
    attention: number;
  }

  /** One session of a routine (cron) intent. */
  interface Run {
    ts: number;
    status: RunStatus;
    tools: number;
    reply: string;
  }

  interface Intent {
    id: string;
    title: string;
    kind: string;
    goal: string;
    sessions: string[];
    profiles: string[];
    children: Child[];
    turns: Turn[];
    /** session id -> the model's verdict on the run */
    runs_out: Record<string, RunOutcome>;
    judgement: Judgement;
    /** routine intents only */
    runs?: Run[];
  }

  /** One apply_log row (an `argus apply` attempt). */
  interface ApplyAttempt {
    outcome: ApplyOutcome;
    message: string | null;
    ledger_entry: string | null;
    pending_id: string | null;
    at: number;
    /** cli | dashboard | auto (the auto_apply policy), ` --force` appended when forced */
    by: string | null;
    /** set on a proposal's `applied` whose ledger entry was rolled back since: the rollback entry */
    rolled_back?: string;
  }

  interface Review {
    learned: string;
    trigger: string;
    evidence_in_run: string;
  }

  /** One skill file changed by one curator ledger entry. */
  interface Learning {
    /** "<ledger entry id>:<file>" */
    id: string;
    /** ledger entry id */
    entry: string;
    profile: string;
    ts: number;
    /** ledger actor: agent | curator | user */
    actor: string | null;
    /** the actor with `curator` split: review (the background review after a turn) | curator */
    writer: string | null;
    action: string | null;
    skill: string | null;
    file: string;
    sid: string | null;
    plus: number;
    minus: number;
    words_before: number;
    words_after: number;
    diff: string;
    intent: string | null;
    intent_who: string | null;
    desc_change: string | null;
    applied?: ApplyAttempt;
    last_attempt?: ApplyAttempt;
    review?: Review;
  }

  interface BlameLine {
    /** line text */
    t: string;
    /** origin: the learning id that introduced it, "base" or "untracked" */
    o: string;
  }

  interface Session {
    id: string;
    profile: string;
    source: string;
    title: string;
    start: number;
    end: number;
    tools: number;
    parent: string | null;
  }

  interface Trend {
    verdict: string;
    summary: string;
  }

  interface Proposal {
    id: string;
    skill: string;
    rule: string;
    why: string;
    intents: string[];
    patterns: string[];
    overlaps: JsonValue;
    confidence: string;
    profile: string;
    file: string;
    diff: string;
    plus: number;
    minus: number;
    ts: number;
    applied: ApplyAttempt | null;
    last_attempt: ApplyAttempt | null;
    /** an existing-skill proposal whose skill is no longer in the profile */
    skill_gone: boolean;
    /** its skill's SKILL.md cannot be written here (e.g. a read-only `skills.external_dirs`) */
    read_only: boolean;
  }

  /** A skill with history in the export that is no longer in its profile. */
  interface SkillAbsence {
    /** removed: by a ledger cleanup (archive, delete); missing: outside Hermes */
    state: 'removed' | 'missing';
    ts: number | null;
    entry: string | null;
    action: string | null;
  }

  /** One Hermes install (every profile of it). */
  interface Harness {
    label: string;
    desc: string;
    profiles: Record<string, string>;
    intents: Intent[];
    learnings: Learning[];
    /** "<profile>:<file>" -> final text by line */
    blame: Record<string, BlameLine[]>;
    sessions: Record<string, Session>;
    /** "<profile>:<skill>" -> flag */
    trends: Record<string, Trend>;
    /** "<tool>|<signature>" -> excerpt */
    examples: Record<string, string>;
    proposals: Proposal[];
    /** "edit:<profile>:<learning id>" | "prop:<profile>:<id>" */
    decisions: Record<string, DecisionAction>;
    /** profile -> installed skills */
    installed: Record<string, number>;
    /** "<profile>:<skill>" -> why a skill with history here is no longer in its profile */
    absent: Record<string, SkillAbsence>;
  }

  interface ExportMeta {
    generated: string;
    /** profile -> why it is missing or incomplete */
    errors?: Record<string, string>;
  }

  interface Export {
    meta: ExportMeta;
    harnesses: Record<string, Harness>;
  }

  // ---------------------------------------------------------------- /status
  interface ProfileCounts {
    intents: number;
    edits_without_evidence: number;
    pending_proposals: number;
  }

  interface LastDerive {
    profile: string;
    file: string;
    ts: number;
    /** the run report, null when large; absent when unreadable */
    report?: JsonValue;
  }

  /** The newest finished derive, when it exited non-zero. */
  interface RunError {
    exit_code: number;
    at: number;
    log: string;
    profile: string;
  }

  interface Status {
    profiles: Record<string, ProfileCounts>;
    intents: number;
    edits_without_evidence: number;
    pending_proposals: number;
    last_derive: LastDerive | null;
    last_run_error: RunError | null;
    running: boolean;
    errors: Record<string, string>;
  }

  // ---------------------------------------------------------------- /decision, /apply
  /** POST /decision body (the response is not read). */
  interface DecisionBody {
    target_kind: DecisionKind;
    target_id: string;
    profile: string;
    /** the clicked button's data-dec / data-pdec (keep, reject, accept, dismiss), or clear */
    action: string;
    snapshot?: Record<string, JsonValue>;
  }

  type Outcome = ApplyOutcome | 'dry-run';

  /** POST /apply response: apply.ApplyResult with the `profile` the plugin API adds. */
  interface ApplyResult {
    ok: boolean;
    outcome: Outcome;
    message: string;
    ledger_entry: string | null;
    target_kind: string;
    target_id: string;
    newer?: JsonValue[];
    pending_id?: string | null;
    old_string?: string;
    new_string?: string;
    profile: string;
  }

  // ---------------------------------------------------------------- derived in app.js
  namespace App {
    /** An exported intent after model() added its derived fields (in place). */
    interface Intent extends Argus.Intent {
      edits: Learning[];
      first: number;
      last: number;
      who: string;
      unstable: boolean;
      varies: boolean;
      risk: string;
      security: boolean;
      status: string;
      calls: number;
      errs: number;
      rt: number;
      /** tool -> [calls, failed calls] */
      tools: Record<string, [number, number]>;
      score: number;
      /** skill key -> reads */
      uses: Record<string, number>;
      /** session id -> skill key -> reads */
      useSid: Record<string, Record<string, number>>;
    }

    /** A harness filtered to the selected profile. */
    interface Harness extends Omit<Argus.Harness, 'intents'> {
      intents: Intent[];
      /** proposals for a skill no longer in the profile: kept apart, out of every count */
      obsolete: Proposal[];
      /** proposals carried out (applied or staged, not rolled back): kept apart, out of every count */
      carried: Proposal[];
    }

    /** A recurring error signature across the filtered intents. */
    interface Sig {
      tool: string;
      sig: string;
      cat: string;
      hits: number;
      sessions: Set<string>;
      /** intent id -> hits */
      intents: Record<string, number>;
      first: number;
      last: number;
      nsess: number;
      k: number;
    }

    /** One session that read a skill. */
    interface UseRun {
      ts: number;
      sid: string;
      iid: string;
      ok: boolean | null;
    }

    interface Use {
      loads: number;
      intents: Record<string, number>;
      runs: UseRun[];
    }

    interface Model {
      hk: string;
      h: Harness;
      I: Record<string, Intent>;
      E: Record<string, Learning>;
      /** "<profile>:<skill>" -> its ledger edits, oldest first */
      groups: Record<string, Learning[]>;
      prof: (p: string) => string;
      pf: string;
      visible: Intent[];
      use: Record<string, Use>;
      sigs: Sig[];
      t0: number;
      t1: number;
    }

    /** A bar / legend segment of the overview: count, colour, label. */
    type Seg = [number, string, string];

    /** One intent × skill cell of the home matrix. */
    interface MapCell {
      reads: number;
      es: Learning[];
      ps: Proposal[];
    }

    type Severity = 'security' | 'risk' | 'review';
    type QueueKind = 'intent' | 'skill' | 'error' | 'proposal' | 'cleanup';

    /** The skill an intent's period edited most, and how many of its edits preceded a run. */
    interface SkillVersion {
      skill: string | undefined;
      v: number;
    }

    /** One session (run) of an intent on its detail page. */
    interface SessionRun {
      sid: string;
      tt: Turn[];
      ts: number;
      tools: number;
      errs: number;
      rt: number;
      ro: RunOutcome | undefined;
      sv: SkillVersion | null;
    }

    /** One skill row of an intent's skills × runs grid. */
    interface SkillRow {
      k: string;
      reads: number;
      es: Learning[];
      ps: Proposal[];
    }

    interface QueueItem {
      score: number;
      kind: QueueKind;
      sev: Severity;
      detail: string;
      what: string;
      /** the concrete action; undefined for an error class without one */
      todo: string | undefined;
      ctx: string;
      href: string;
      since: number;
      /** absent on the cleanup item */
      sess?: number;
      edits?: number;
    }

    /** Hermes edit (applied) or plugin proposal (pending), as one change. */
    interface HermesChange {
      id: string;
      src: 'hermes';
      status: 'removed' | 'applied';
      ts: number;
      who: string | null;
      ev: Evidence;
      rule: string;
      why: string;
      diff: string;
      plus: number;
      minus: number;
      file: string;
      e: Learning;
    }

    interface PluginChange {
      id: string;
      src: 'plugin';
      status: 'pending';
      ts: number;
      who: 'plugin';
      ev: 'proposal';
      rule: string;
      why: string;
      diff: string;
      plus: number;
      minus: number;
      file: string;
      p: Proposal;
    }

    type Change = HermesChange | PluginChange;

    interface Changes {
      hermes: HermesChange[];
      plugin: PluginChange[];
      obsolete: PluginChange[];
      carried: PluginChange[];
      prof: string | undefined;
      skill: string | undefined;
    }

    /** evOf(): the evidence class of an edit. */
    type Evidence = string;

    /** Where an edit came from and which runs read its skill afterwards. */
    interface Lineage {
      src: Intent | undefined;
      after: UseRun[];
      ids: string[];
      ok: number;
      ko: number;
      leak: boolean;
    }

    /** An intent linked to a skill: reads, edits applied after its turns, proposals citing it. */
    interface Related {
      id: string;
      reads: number;
      applied: number;
      proposed: number;
    }

    /** What applyState / applyTag / outcomeHtml read of an apply attempt or result. */
    interface ApplyView {
      /** an Outcome, or 'running' while this page waits for POST /apply */
      outcome: string;
      message?: string | null;
      ledger_entry?: string | null;
      rolled_back?: string;
      /** who ran the apply; `auto` for the auto_apply policy */
      by?: string | null;
    }

    /** A Learning or Proposal: what applyState reads of the export's apply state. */
    interface Applicable {
      applied?: ApplyView | null;
      last_attempt?: ApplyView | null;
    }

    interface DockRow {
      kind: DecisionKind;
      id: string;
      prof: string;
      item: Learning | Proposal;
      can: boolean;
      /** why apply is not offered when `can` is false */
      off: string;
      /** the one-line summary of the row (the command shows when the row is expanded) */
      label: string;
      cmd: string;
      note: string;
      cli: string;
      ask: Ask;
    }

    /** What the confirmation dialog shows before a change: facts, a preview, the conditions. */
    interface Ask {
      title: string;
      /** go: a write the person asked for; danger: a restore or a reset */
      tone: 'go' | 'danger';
      facts: [string, string][];
      /** a unified diff (`+`/`-` lines colored) or a plain file preview */
      preview?: { label: string; text: string; diff: boolean };
      notes: string[];
      ok: string;
    }

    interface DecisionTarget {
      ids: string[];
      body: Omit<DecisionBody, 'action'>;
    }
  }
}
