"""The Phase 5 dependencies are pinned to minor versions (spec 11.1 item 4; audit F-11, SS's
DS-6): the minors the audit resolved and tested, with a compatible-release pin (~=X.Y.Z means
>=X.Y.Z and <X.(Y+1))."""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

RUNTIME = {"reportlab~=4.5.1", "cryptography~=50.0.2"}
DEV = {"jsonschema~=4.26.0", "pypdf~=6.21.4"}


def _names(requirements: list[str]) -> dict[str, str]:
    return {
        req.split("~")[0].split(">")[0].split("=")[0].split("<")[0]: req for req in requirements
    }


def test_the_phase5_dependencies_are_pinned_to_the_tested_minors() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    runtime = _names(project["dependencies"])
    dev = _names(project["optional-dependencies"]["dev"])
    assert {runtime[name.split("~")[0]] for name in RUNTIME} == RUNTIME
    assert {dev[name.split("~")[0]] for name in DEV} == DEV
