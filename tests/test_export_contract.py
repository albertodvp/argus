"""The export contract is written twice: exporttypes.py for mypy and the `Argus` namespace of
dashboard/app/types.d.ts for tsc. Each checker checks its side; this test checks that the two sides
name the same fields, and that no interface is left out."""

from pathlib import Path

import pytest

from argus import apply, exporttypes
from argus.signals import BlameLine

DTS = Path(__file__).resolve().parents[1] / "argus" / "dashboard" / "app" / "types.d.ts"

# TypedDict -> the interface of the same name in `declare namespace Argus`
CONTRACT: dict[str, frozenset[str]] = {
    t.__name__: t.__required_keys__ | t.__optional_keys__
    for t in (
        exporttypes.Turn,
        exporttypes.Child,
        exporttypes.RunOutcome,
        exporttypes.Judgement,
        exporttypes.Run,
        exporttypes.Intent,
        exporttypes.ApplyAttempt,
        exporttypes.Review,
        exporttypes.Learning,
        BlameLine,
        exporttypes.Session,
        exporttypes.Trend,
        exporttypes.Proposal,
        exporttypes.SkillAbsence,
        exporttypes.Harness,
        exporttypes.ExportMeta,
        exporttypes.Export,
        exporttypes.ProfileCounts,
        exporttypes.LastDerive,
        exporttypes.RunError,
        exporttypes.Status,
    )
}
# /apply answers apply.ApplyResult plus the profile the plugin API adds
CONTRACT["ApplyResult"] = apply.ApplyResult.__required_keys__ | {
    *apply.ApplyResult.__optional_keys__,
    "profile",
}
# the /decision body app.js sends; plugin_api._decide checks it field by field
REQUEST_ONLY: frozenset[str] = frozenset({"DecisionBody"})


def interfaces() -> dict[str, set[str]]:
    """Field names of the top-level interfaces of `declare namespace Argus` (not Argus.App)."""
    out: dict[str, set[str]] = {}
    current: set[str] | None = None
    for line in DTS.read_text(encoding="utf-8").splitlines():
        if line.startswith("  namespace App"):
            break
        if line.startswith("  interface "):
            current = out.setdefault(line.split()[1], set())
        elif line == "  }":
            current = None
        elif current is not None and line.startswith("    ") and not line.startswith("     "):
            name, sep, _rest = line.strip().partition(":")
            if sep and name.rstrip("?").isidentifier():
                current.add(name.rstrip("?"))
    return out


@pytest.mark.parametrize("name", sorted(CONTRACT))
def test_typescript_mirrors_the_python_contract(name: str) -> None:
    ts = interfaces()
    assert name in ts, f"types.d.ts has no interface {name}"
    assert ts[name] == set(CONTRACT[name])


def test_every_interface_has_a_python_side() -> None:
    assert set(interfaces()) - REQUEST_ONLY == set(CONTRACT)
