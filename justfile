# Development checks; the plugin itself needs neither uv nor just (it is copied into a Hermes install).
check: lint typecheck typecheck-js test

setup:
    uv sync --locked

lint: setup
    if command -v ruff >/dev/null 2>&1; then ruff check .; else uv tool run --from ruff==0.16.10 ruff check .; fi
    if command -v ruff >/dev/null 2>&1; then ruff format --check .; else uv tool run --from ruff==0.16.10 ruff format --check .; fi

typecheck: setup
    uv run --locked mypy --config-file pyproject.toml --no-incremental

# The dashboard JS is plain and served as-is (no build): tsc, pinned in package-lock.json, only checks it.
typecheck-js:
    npm ci --ignore-scripts --no-audit --no-fund --loglevel=error
    node_modules/.bin/tsc -p tsconfig.json

test: setup
    uv run --locked pytest

# Optional, needs a Hermes checkout with a .venv (verified on 0.22.7 and 0.23.0): mypy.stubtest runs
# in a throw-away venv on the Hermes interpreter; nothing is installed into Hermes, and HOME and
# HERMES_HOME are temporary.
# Check typings/ against the runtime of the Hermes checkout at `hermes`.
stubtest hermes:
    #!/usr/bin/env bash
    set -euo pipefail
    hermes="$(cd "{{invocation_directory()}}" && cd "{{hermes}}" 2>/dev/null && pwd)" || { echo "stubtest: no directory {{hermes}}" >&2; exit 1; }
    [ -x "$hermes/.venv/bin/python" ] || { echo "stubtest: no Hermes venv at $hermes" >&2; exit 1; }
    echo "stubtest: $hermes ($(git -C "$hermes" rev-parse --short HEAD 2>/dev/null || echo unknown))"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    site="$("$hermes/.venv/bin/python" -c 'import site; print(site.getsitepackages()[0])')"
    "$hermes/.venv/bin/python" -m venv --without-pip "$tmp/venv"
    uv pip install --quiet --python "$tmp/venv/bin/python" mypy==2.4.0
    mkdir -p "$tmp/home" "$tmp/user"
    cd "$tmp"
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$hermes:$site" HOME="$tmp/user" HERMES_HOME="$tmp/home" HERMES_DISABLE_LAZY_INSTALLS=1 \
        MYPYPATH="{{justfile_directory()}}/typings" "$tmp/venv/bin/python" -m mypy.stubtest --ignore-missing-stub \
        --allowlist "{{justfile_directory()}}/typings/stubtest-allowlist.txt" \
        hermes_constants hermes_bootstrap tools.skill_ledger tools.skill_manager_tool agent.plugin_llm \
        hermes_cli.plugins hermes_cli.plugins_ledger hermes_cli.config

# The plugin catalog's admission check (`hermes plugins validate --install-deps`) on the committed
# argus/ at `rev` (what a catalog pin would read), with a fresh Hermes checkout at `hermes_ref` (the
# catalog CI runs main). HOME-independent: Hermes and its HERMES_HOME live in a temporary directory.
# On Linux it runs inside the flake's FHS env, because Hermes stages a prebuilt uv NixOS cannot exec.
validate rev="HEAD" hermes_ref="main":
    #!/usr/bin/env bash
    set -euo pipefail
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    git -C "{{justfile_directory()}}" archive "{{rev}}" argus | tar -x -C "$tmp"
    git init -q "$tmp/hermes"
    git -C "$tmp/hermes" fetch -q --depth 1 https://github.com/NousResearch/hermes-agent.git "{{hermes_ref}}"
    git -C "$tmp/hermes" -c advice.detachedHead=false checkout -q FETCH_HEAD
    echo "hermes-agent $(git -C "$tmp/hermes" rev-parse HEAD), argus $(git -C "{{justfile_directory()}}" rev-parse "{{rev}}")"
    # the devShell's UV_PYTHON is Argus' 3.11; Hermes needs its own (3.14), found or downloaded by uv
    run="cd '$tmp/hermes' && env -u UV_PYTHON HERMES_HOME='$tmp/home' UV_PYTHON_DOWNLOADS=automatic uv run --frozen hermes plugins validate '$tmp/argus' --install-deps"
    if [ "$(uname -s)" = Linux ]; then
        nix run "{{justfile_directory()}}#catalog-env" -- -c "$run"
    else
        bash -c "$run"
    fi

