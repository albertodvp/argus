"""System prompts and JSON schemas of the derivation passes (ported from pipeline/intents.py).

Every model call goes through derive.py's `llm(instructions, text, schema, purpose)`; output is
English.
"""

from typing import Final

from .jsontypes import JsonObject

# ---------------------------------------------------------------- assign (one call per session)
ASSIGN_SYS: Final = (
    "You track the INTENTS of the people driving an agent harness. An intent is what "
    "someone wanted from the agent: one expected result, possibly pursued over many turns, "
    "sessions and days.\n"
    "Given a short list of candidate intents and the new user turns of ONE session, decide "
    "for each turn whether it continues a candidate intent or starts a new one.\n"
    "- The same expected result, whatever the wording, is the SAME intent, including "
    "refinements and corrections.\n"
    "- Repeated runs of the same request, test or check across sessions are the SAME "
    "intent.\n"
    "- A follow-up in the same session usually continues that session's current intent, "
    "unless the objective clearly changes.\n"
    "- Prefer a NEW intent when the deliverable changes (exploring data -> planning an "
    "integration -> verifying an implementation), even in the same session. A project is "
    "not an intent.\n"
    "For each turn answer with a candidate id, the label of an earlier turn of this list "
    "(same new intent as that turn), or NEW. For NEW give a title (plain English, max 8 "
    "words), a goal (one plain English sentence: what the person wanted), kind and who.\n"
    "kind: task = work the person wants done; probe = security or capability probing; chat "
    "= small talk or questions without a deliverable.\n"
    "who: real-user = an end user of the product; team = the developer/operator working on "
    "the harness; automated-test = scripted or repeated test prompts; scheduled = "
    "cron/routine.\n"
    "Answer JSON only."
)
ASSIGN_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "turns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "turn": {"type": "string", "description": "turn label, e.g. T1"},
                    "intent": {
                        "type": "string",
                        "description": (
                            "candidate intent id, label of an earlier turn of this list, or NEW"
                        ),
                    },
                    "title": {"type": "string", "description": "if NEW: max 8 words, else empty"},
                    "goal": {
                        "type": "string",
                        "description": "if NEW: one plain English sentence, else empty",
                    },
                    "kind": {"type": "string", "enum": ["task", "probe", "chat"]},
                    "who": {
                        "type": "string",
                        "enum": ["real-user", "team", "automated-test", "scheduled"],
                    },
                },
                "required": ["turn", "intent", "title", "goal", "kind", "who"],
            },
        }
    },
    "required": ["turns"],
}

# ---------------------------------------------------------------- consolidate (explicit operations)
CONS_SYS: Final = (
    "You consolidate intents extracted turn-by-turn from an agent harness. Incremental "
    "assignment over-splits: it sees one session at a time, so the same objective usually "
    "ends up in several intents. On real harnesses a first consolidation folds roughly a "
    "third to a half of the intents into others. Check every pair that could share an "
    "expected result.\n"
    "An intent is one expected result. Merge when two intents would be judged done by the "
    "same outcome:\n"
    "- repeated runs of the same test, check or import, even with different inputs, dates "
    "or wording;\n"
    "- automated probes of the same capability or tool family in one test campaign (for "
    "example several probes of tool errors and catalogs) form ONE probe intent with "
    "several runs;\n"
    "- paraphrases, retries after a failure, and follow-ups that were split by mistake.\n"
    "Keep apart: different deliverables (a document vs an import of that document), "
    "different products or workspaces when the person asked for each separately, and "
    "real-user work vs automated tests of it.\n"
    "Return explicit operations:\n"
    "- merge: `keep` survives (prefer the intent with more turns), `drop` is folded into "
    "it.\n"
    "- split: some listed turns of intent `from` belong to a different objective; `turns` "
    "are their labels, `title` and `goal` describe the new intent.\n"
    "- rename: the title or goal of intent `id` no longer describes its turns (use it on "
    "the surviving intent after merges when needed).\n"
    "Use only ids and turn labels from the input. Titles in English, max 8 words, plain "
    "and specific; goals one plain English sentence. Return an empty list only when "
    "nothing needs to change."
)
CONS_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["merge", "split", "rename"]},
                    "keep": {"type": "string", "description": "merge: surviving intent id"},
                    "drop": {"type": "string", "description": "merge: intent id folded into keep"},
                    "from": {"type": "string", "description": "split: intent id the turns leave"},
                    "turns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "split: turn labels moved to the new intent",
                    },
                    "id": {"type": "string", "description": "rename: intent id"},
                    "title": {"type": "string", "description": "split/rename: max 8 words"},
                    "goal": {
                        "type": "string",
                        "description": "split/rename: one plain English sentence",
                    },
                },
                "required": ["op"],
            },
        }
    },
    "required": ["ops"],
}

# ---------------------------------------------------------------- judge (outcome of one intent)
OUT_SYS: Final = (
    "You judge the outcome of one intent of an agent harness from its turns (user → agent "
    "reply, tool calls in that turn). Write in English. Be concrete, terse and "
    "evidence-based; use plain, neutral wording, no marketing or alarm words, no "
    "exclamation.\n"
    "Rules:\n"
    '- status "achieved" ONLY if the person actually got what they wanted AND repeated '
    "runs are consistent. Unstable results across identical or near-identical runs are at "
    'best "partial".\n'
    "- risk: name any security or safety problem (forbidden tool used, secret access "
    "attempted, data written where it should not, a defective result left pending "
    "approval). Empty if none.\n"
    "- who: real-user = an end user of the product; team = the developer/operator working "
    "on the harness; automated-test = scripted or repeated test prompts; scheduled = "
    "cron/routine.\n"
    "For a routine (scheduled job), judge whether the runs did their job and what they "
    "reported."
)
OUT_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["achieved", "partial", "failed", "unclear"]},
        "who": {"type": "string", "enum": ["real-user", "team", "automated-test", "scheduled"]},
        "stable": {"type": "string", "enum": ["stable", "unstable", "single-run"]},
        "risk": {
            "type": "string",
            "description": "security/safety problem in max 20 plain English words, or empty",
        },
        "headline": {
            "type": "string",
            "description": "one factual English sentence an operator reads first, max 18 words",
        },
        "outcome": {"type": "string", "description": "max 30 words"},
        "consistency": {
            "type": "string",
            "description": "if several runs: what differs across runs, max 30 words; else empty",
        },
        "friction": {
            "type": "string",
            "description": (
                "user corrections, errors, repeated asks, wasted work; max 30 words or empty"
            ),
        },
        "next": {
            "type": "string",
            "description": "the decision or next step for the human, max 20 words or empty",
        },
        "attention": {
            "type": "integer",
            "description": "0-3: how much this needs the operator now",
        },
    },
    "required": [
        "status",
        "who",
        "stable",
        "risk",
        "headline",
        "outcome",
        "consistency",
        "friction",
        "next",
        "attention",
    ],
}

# ------------------------------------------------- runs (one line per session of a repeated intent)
RUN_SYS: Final = (
    "Summarise the result of ONE run of a repeated agent task, so runs can be compared in "
    "a table. English, max 12 words, concrete values (counts, names) that could differ "
    "between runs."
)
RUN_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "result": {"type": "string"},
        "ok": {"type": "boolean", "description": "did this run do what was asked"},
    },
    "required": ["result", "ok"],
}

# ------------------------------------------------ edit review (evidence behind a Hermes skill edit)
REV_SYS: Final = (
    "You review an autonomous skill edit made by an agent harness. Read the diff and the "
    "run window that preceded it. Be concrete and terse. Write in plain English."
)
REV_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "learned": {"type": "string", "description": "the new or changed rule, max 25 words"},
        "trigger": {"type": "string", "description": "what in the run caused it, max 25 words"},
        "evidence_in_run": {
            "type": "string",
            "enum": ["observed-failure", "observed-success", "user-correction", "speculative"],
        },
    },
    "required": ["learned", "trigger", "evidence_in_run"],
}
LEARN_SYS: Final = (
    "You read an autonomous skill edit made by an agent harness. Only the diff is available, not "
    "the run that led to it. State the rule it adds or changes. Be concrete and terse. Write in "
    "plain English."
)
LEARN_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "learned": {"type": "string", "description": "the new or changed rule, max 25 words"},
    },
    "required": ["learned"],
}

# ------------------------------------------------ proposals (skill changes from intents and errors)
PROP_SYS: Final = (
    "You propose skill changes for an agent harness, derived from what the people using it "
    "tried to do (intents) and from recurring tool/runtime errors.\n"
    "Only propose a change backed by at least 2 intents or an error pattern with 5+ hits. "
    "Prefer patching an existing skill; propose a new skill only when none fits.\n"
    "Do not repeat a rule the harness already learned (listed under HERMES EDITS); if your "
    "proposal overlaps one, cite its id in overlaps.\n"
    "English, plain, specific: the rule must be something the agent can follow in its next "
    "run. Max 8 proposals."
)
PROP_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "description": "existing skill name, or new:<name>",
                    },
                    "rule": {
                        "type": "string",
                        "description": "the instruction to add, max 35 words",
                    },
                    "why": {
                        "type": "string",
                        "description": "the evidence in one sentence, max 25 words",
                    },
                    "intents": {"type": "array", "items": {"type": "string"}},
                    "patterns": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "indexes of ERROR PATTERNS used",
                    },
                    "overlaps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "ids of HERMES EDITS with the same rule",
                    },
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": [
                    "skill",
                    "rule",
                    "why",
                    "intents",
                    "patterns",
                    "overlaps",
                    "confidence",
                ],
            },
        }
    },
    "required": ["proposals"],
}
PATCH_SYS: Final = (
    "You turn a proposed rule into a minimal edit of a SKILL.md file. Pick the section "
    "where the rule belongs and insert it there, matching the surrounding style (bullet, "
    "numbered step or paragraph). Do not rewrite other lines. Return the line number after "
    "which to insert and the new lines."
)
PATCH_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "after_line": {"type": "integer"},
        "lines": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["after_line", "lines"],
}
