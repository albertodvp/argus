"""Child process of the dashboard's `POST /apply`:
`python apply_child.py <hermes repo> <kind> <id> <deadline>`.

Hermes' skill functions act on the HERMES_HOME of the process that calls them, and
`hermes -p <profile> argus apply` only exists in profiles that enable the plugin. So the dashboard
runs this file with the install's interpreter and HERMES_HOME=<the decision's profile home>; it
imports the plugin package from its directory without running its __init__ (hooks, CLI
registration), like plugin_api._mod, and prints the result of apply.run as one JSON line.
"""

import importlib
import json
import os
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING


def main() -> int:
    repo, kind, tid, deadline = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
    if repo not in sys.path:
        # the gateway's interpreter finds hermes_cli/tools only through its launcher's sys.path
        sys.path.insert(0, repo)
    try:
        import hermes_bootstrap  # noqa: F401  first import of every Hermes entry point
    except ImportError:
        pass
    pkg_name = "argus_apply_child_pkg"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules[pkg_name] = pkg
    if TYPE_CHECKING:  # the module loaded below is this package's apply.py
        from . import apply
    else:
        apply = importlib.import_module(f"{pkg_name}.apply")
    r = apply.run(
        Path(os.environ["HERMES_HOME"]), kind, tid, applied_by="dashboard", deadline=deadline
    )
    print(json.dumps(r, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
