# Bench: reproducible derive numbers

`just bench` reruns Argus' backfill and derive on a frozen copy of the fictional demo home, with
every model call answered from a recorded cassette, and checks the headline numbers. No model, no
API key, the same result on any machine and any day.

| File | What |
|---|---|
| `fixture.tar.xz` | what backfill and derive read from the demo home (`bench/fixture.py` lists it), with the history window frozen in `manifest.json` |
| `cassette.jsonl` | one line per model call of a derive on that fixture: sha256 of the prompt, purpose, answer (no prompt text) |
| `expected.json` | the numbers the replay must give |
| `fixture.py` | `build` the fixture from a Hermes home; `prepare` a scratch Hermes home from it |
| `check.py` | the runner behind `just bench` |

```bash
nix develop -c just bench                     # replay and compare; exit 1 on any difference or miss
nix develop -c just bench --write-expected    # write expected.json from a clean replay instead
```

The numbers: live intents (as the dashboard counts them), proposals written and those dropped for
want of verbatim quotes, skill edits by evidence label (`unlabelled`: not reviewed, such as a
person's edit, one without a diff or one whose session has messages only before the window), the
share of `speculative` among the edits judged on their run, runs by signal status, and the model
calls replayed.

A replay that misses a call (exit 1, naming the stage and the prompt hash) means a prompt changed:
the fixture, `argus/prompts.py` or how derive builds a prompt. Record the cassette again (below) when
that change is intended, and commit the three files together.

## Regenerate

Only from the **fictional demo home** (`~/Projects/argus-demo/home`), never from a real user's
Hermes home: `build` reads only the home given with `--home` and refuses `~/.hermes`. Recording makes
real model calls on the demo's model (about 210); the scratch home's `prepare` prints the estimate
first. Run from the repository root:

```bash
# 1. the fixture: the demo's state.db rows of its Argus history window, ledger, blobs, SKILL.md files
nix develop -c uv run --locked python -m bench.fixture build --home ~/Projects/argus-demo/home

# 2. a scratch Hermes home from it, backfilled at the fixture's frozen time (prints the next derive's calls)
S="$(mktemp -d)" && trap 'rm -rf "$S"' EXIT   # the scratch dir (and the login copied into it) goes on exit
B="$S/home"
nix develop -c uv run --locked python -m bench.fixture prepare "$B"

# 3. model access for the recording only: Argus, the demo's model settings and its login
mkdir -p "$B/plugins" && cp -r argus "$B/plugins/argus"
install -m 600 ~/Projects/argus-demo/home/auth.json "$B/auth.json"
cat > "$B/config.yaml" <<'EOF'
model: {default: gpt-6.1-sol, provider: openai-codex}
auxiliary: {argus: {provider: openai-codex, model: gpt-6.1-sol}}
plugins: {enabled: [argus]}
EOF

# 4. record: the demo's Hermes on the scratch home
~/Projects/argus-demo/bin/demo-env bash -c \
  'HERMES_HOME="$0" exec "$HERMES_VENV/bin/hermes" argus derive --record "$1"' "$B" "$PWD/bench/cassette.jsonl"

# 5. the expected numbers from a replay, then the check itself
nix develop -c just bench --write-expected
nix develop -c just bench
```

Keep the scratch `config.yaml` to the model settings above: `skills.create_dir` or
`skills.external_dirs` would add skill roots the replay (without Hermes) does not see, and change
the skill list in the proposals prompt.

## What keeps the prompts the same

- **The window.** Backfill counts a `--since DAYS` window back from the clock; `prepare` backfills
  with the manifest's `since_days` at its frozen `now` instead (`backfill.run(now=...)`), so the
  same sessions, messages and ledger entries are read on any day.
- **The plugin DB.** Derive's prompts depend on what earlier passes stored (intents, judgements,
  `consolidated_at`, `proposals_at`, applied rules); the fixture has no plugin DB, so every run
  starts from the same empty one.
- **The history around the window.** Sessions active in the window and those an edit in the window
  names are copied with all their messages: whether an edit's run is outside the window
  (`sessions_with_messages`) reads the same as on the demo home.
- **Time zones.** Ledger times are parsed with their UTC offset; `build` refuses a ledger time
  without one (it would read as local time). Turn times in prompts are formatted in UTC.
- **Paths.** Prompts carry ledger paths (copied verbatim) cut at `/skills/`, skill names and paths
  relative to the skill root, never the Hermes home's path: the scratch home can live anywhere.
- **Order.** Derive is sequential; its sets are only counted or tested for membership, every listing
  that reaches a prompt is sorted or follows insertion order in SQLite, and the fixture is
  unpacked byte for byte, so a recorded prompt asked twice gets its answers in recorded order.
- Wall-clock values derive writes (`created_at`, `updated_at`, `judged_at`, `consolidated_at`,
  `proposals_at`) differ between runs, but within one pass from an empty DB they are only compared
  with values of the same pass and never reach a prompt.
