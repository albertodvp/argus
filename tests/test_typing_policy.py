"""The pinned type checker runs with the no-Any policy: two representative probes (the policy itself
is pyproject.toml, applied to the whole code by `just typecheck`)."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("source", "diagnostic"),
    [
        ("from typing import Any\nvalue: Any = 1\n", "[explicit-any]"),
        (
            'import json\nvalue = json.loads("1")\n',
            'Expression has type "Any"',
        ),
    ],
)
def test_unsafe_code_is_rejected(tmp_path: Path, source: str, diagnostic: str) -> None:
    result = run_mypy(tmp_path, source)
    assert result.returncode == 1, result.stdout + result.stderr
    assert diagnostic in result.stdout, result.stdout + result.stderr


def test_fully_typed_code_is_accepted(tmp_path: Path) -> None:
    result = run_mypy(tmp_path, "def identity(value: int) -> int:\n    return value\n")
    assert result.returncode == 0, result.stdout + result.stderr


def test_hermes_stubs_are_found(tmp_path: Path) -> None:
    source = (
        "from pathlib import Path\n"
        "from hermes_constants import get_hermes_home\n"
        "home: Path = get_hermes_home()\n"
    )
    result = run_mypy(tmp_path, source)
    assert result.returncode == 0, result.stdout + result.stderr


def run_mypy(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    fixture = tmp_path / "typing_probe.py"
    fixture.write_text(source, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(ROOT / "pyproject.toml"),
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / "cache"),
            str(fixture),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
