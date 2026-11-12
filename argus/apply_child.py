"""Child process of the dashboard's `POST /apply`: `python apply_child.py <kind> <id> <deadline>`.

Hermes' skill functions act on the HERMES_HOME of the process that calls them, and
`hermes -p <profile> argus apply` only exists in profiles that enable the plugin. So the dashboard
runs this file with the install's interpreter, HERMES_HOME=<the decision's profile home> and the
directory Hermes is imported from on PYTHONPATH (the gateway's interpreter finds hermes_cli/tools
only through its launcher's sys.path). It imports the plugin package from its directory without
running its __init__ (hooks, CLI registration), like plugin_api._mod, and prints the result of
apply.run as one JSON line.
"""

import contextlib
import importlib
import json
import os
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING

with contextlib.suppress(ImportError):
    import hermes_bootstrap  # noqa: F401  first import of every Hermes entry point

PKG = "argus_apply_child_pkg"

if TYPE_CHECKING:
    from . import apply
else:  # this package under a private name, after hermes_bootstrap
    _pkg = types.ModuleType(PKG)
    _pkg.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules[PKG] = _pkg
    apply = importlib.import_module(f"{PKG}.apply")


def main() -> int:
    kind, tid, deadline = sys.argv[1], sys.argv[2], float(sys.argv[3])
    r = apply.run(
        Path(os.environ["HERMES_HOME"]), kind, tid, applied_by="dashboard", deadline=deadline
    )
    print(json.dumps(r, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
