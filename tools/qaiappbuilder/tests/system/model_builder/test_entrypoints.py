# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Entry-point integrity for Model Builder (CLI + chat-skill scripts).

Records a real defect found while designing the system tests:

    ``scripts/build/model_builder_cli.py`` is imported by
    ``apps/cli/commands/pack.py`` and the model-builder skill scripts
    (``qai_pack_export.py`` / ``qai_pack_validate.py`` /
    ``qai_workspace_init.py``) but does not exist in the source tree. The
    repo-root ``.gitignore`` rule ``build/`` (unanchored) silently excluded
    every ``scripts/build/`` directory from commits, so the file never
    reached the remote. On a clean checkout ``qai pack export|validate|init``
    and the agent's export step crash with ``ModuleNotFoundError``.

Fix: anchor the rule as ``/build/`` and commit ``scripts/build/``. Both are
done; these tests now guard against a regression.
"""

from __future__ import annotations

import ast
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.system

_REPO = Path(__file__).resolve().parents[3]
_SCAN_DIRS = ("apps", "factory/chat_features/model-builder/scripts", "interfaces", "src")


def _scripts_build_imports() -> dict[str, list[str]]:
    """Return ``{module: [referencing files]}`` for every ``scripts.build.*`` import."""
    found: dict[str, list[str]] = {}
    for rel in _SCAN_DIRS:
        base = _REPO / rel
        if not base.is_dir():
            continue
        for py in base.rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                mods: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                elif isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                for m in mods:
                    if m == "scripts.build" or m.startswith("scripts.build."):
                        found.setdefault(m, []).append(
                            py.relative_to(_REPO).as_posix()
                        )
    return found


def test_scripts_build_is_referenced_by_entrypoints() -> None:
    """Sanity: the scan finds the known consumers (guards the test itself)."""
    refs = _scripts_build_imports()
    assert "scripts.build.model_builder_cli" in refs
    users = set(refs["scripts.build.model_builder_cli"])
    assert "apps/cli/commands/pack.py" in users
    assert any(u.endswith("qai_pack_export.py") for u in users)


def test_scripts_build_not_gitignored() -> None:
    """``scripts/build/`` must not be swallowed by a too-broad ``build/`` rule."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    probe = "tools/qaiappbuilder/scripts/build/model_builder_cli.py"
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=_REPO, capture_output=True, text=True, check=False,
    )
    if top.returncode != 0:
        pytest.skip("not inside a git work tree")
    root = Path(top.stdout.strip())
    rel = (_REPO / "scripts/build/model_builder_cli.py").relative_to(root).as_posix()
    r = subprocess.run(
        ["git", "check-ignore", "-v", "--no-index", rel],
        cwd=root, capture_output=True, text=True, check=False,
    )
    # exit 0 => ignored (bad), 1 => not ignored (good), else git error.
    assert r.returncode in (0, 1), r.stderr
    assert r.returncode == 1, (
        f"{rel} is git-ignored by rule: {r.stdout.strip()!r}. "
        f"Anchor the rule (e.g. '/build/'). probe={probe}"
    )


def test_scripts_build_imports_resolve() -> None:
    """Every ``scripts.build.*`` module imported by the code must exist."""
    missing: list[str] = []
    for mod, users in sorted(_scripts_build_imports().items()):
        rel = Path(*mod.split("."))
        exists = (_REPO / rel).with_suffix(".py").is_file() or (
            _REPO / rel / "__init__.py"
        ).is_file() or (_REPO / rel).is_dir()
        if not exists:
            missing.append(f"{mod}  <- {', '.join(sorted(set(users))[:3])}")
    assert not missing, "unresolvable imports:\n  " + "\n  ".join(missing)


@pytest.mark.parametrize(
    "script",
    ["qai_pack_export.py", "qai_pack_validate.py", "qai_workspace_init.py"],
)
def test_skill_script_imports_cleanly(script: str) -> None:
    """The skill scripts must import their CLI entry without error."""
    path = _REPO / "factory/chat_features/model-builder/scripts" / script
    assert path.is_file(), path
    spec = importlib.util.spec_from_file_location(f"_mb_skill_{path.stem}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
