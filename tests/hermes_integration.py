"""Apply and skill discovery against a real Hermes, in throw-away Hermes homes.

Not collected by pytest (the dev env has no Hermes). Run it with the interpreter of a Hermes
install, from the repository root:

    just integration /path/to/hermes-env/bin/python3

(for a source checkout of Hermes, put the checkout on PYTHONPATH).

Each case runs in its own process with HERMES_HOME set to a fresh temporary profile, because
Hermes reads its home when its modules are imported. The cases write only under that temporary
directory; the script is not a sandbox (it inherits the environment, and Hermes may prepare its
runtime for a new data root). Exit status 0 only when every case holds.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CASES_FILE = Path(__file__).resolve().parent / "hermes_integration_cases.py"


def _pythonpath() -> str:
    """The repository first, then whatever made Hermes importable here (a source checkout)."""
    return os.pathsep.join([str(REPO), *filter(None, [os.environ.get("PYTHONPATH")])])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="argus-it-list-") as scratch:  # Hermes reads it
        names = subprocess.run(
            [sys.executable, str(CASES_FILE), "--list"],
            env={**os.environ, "PYTHONPATH": _pythonpath(), "HERMES_HOME": scratch},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    failed = 0
    for name in names:
        tmp = Path(tempfile.mkdtemp(prefix=f"argus-it-{name}-"))
        env = {
            # TERMINAL_CWD (set inside a Hermes terminal) would replace the case's cwd as the
            # project Hermes looks at
            **{k: v for k, v in os.environ.items() if k != "TERMINAL_CWD"},
            "HERMES_HOME": str(tmp / "home"),
            "HOME": str(tmp / "user"),
            # the repository first, then whatever made Hermes importable here (a source checkout)
            "PYTHONPATH": _pythonpath(),
        }
        if name == "every_profile_from_one_process":
            env["HERMES_HOME"] = str(tmp / "a" / "home")
        (tmp / "user").mkdir()
        p = subprocess.run(
            [sys.executable, str(CASES_FILE), name, str(tmp)],
            env=env,
            cwd=tmp,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        ok = p.returncode == 0
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}", flush=True)
        if not ok:
            print((p.stdout + p.stderr)[-3000:], flush=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{len(names) - failed}/{len(names)} cases passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
