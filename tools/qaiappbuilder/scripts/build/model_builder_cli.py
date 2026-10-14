# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""CLI entry points for the ``model_builder`` bounded context (U-025).

V2 equivalent of the three legacy ModelBuilder scripts that the
``factory/chat_features/model-builder/SKILL.md`` workflow drives:

================================  =====================================  ============================
legacy script                     V2 ``qai`` subcommand                  use case
================================  =====================================  ============================
``qai_workspace_init.py``         ``qai pack workspace-init``            :class:`InitWorkspaceUseCase`
``qai_pack_export.py``            ``qai pack export``                    :class:`ExportPackUseCase`
``qai_pack_validate.py``          ``qai pack validate``                  :class:`ValidatePackUseCase`
================================  =====================================  ============================

The ModelBuilder use cases are framework-free and their adapters touch
only the filesystem, so this module is a thin composition root (like
``apps/api/_model_builder_di.py`` but without the HTTP container): it
wires the adapters, parses ``argv``, runs the async use case via
:func:`asyncio.run`, prints a human-readable result, and returns a
process exit code. No new business logic lives here.

CLI argument parity is preserved with the legacy scripts so the SKILL
workflow keeps working with the new command names (``--workdir`` /
``--model-name`` / ``--precision`` / ``--precisions`` /
``--default-precision`` for export; ``<model_name>`` positional for
init; ``<pack_dir>`` positional for validate).

CLI D3 (Desktop App Plan §2.4, 2026-06-10): the standalone
``qai-workspace-init`` / ``qai-pack-export`` / ``qai-pack-validate``
console-scripts were retired; the three ``*_main`` callables below are
now invoked exclusively via the unified ``qai`` dispatcher (see
``apps/cli/commands/pack.py``). The ``prog=`` strings have been updated
accordingly so ``--help`` prints the new command name.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Sequence

from qai.model_builder.adapters import (
    FileSystemWorkspaceInitializer,
    QaiPackExporter,
    QaiPackValidator,
    RuleAndShapeTaxonomyClassifier,
    WosAiWorkspaceReader,
)
from qai.model_builder.application.use_cases.export_pack import (
    ExportPackUseCase,
)
from qai.model_builder.application.use_cases.init_workspace import (
    InitWorkspaceUseCase,
)
from qai.model_builder.application.use_cases.validate_pack import (
    ValidatePackUseCase,
)
from qai.model_builder.domain import ExportPackCommand

#: Default workspace root. On Windows: C:/WoS_AI (V1 parity).
#: On Linux/macOS: IQ_AI relative to CWD, matching _DEFAULT_WORKSPACE_ROOT
#: in system_prompt_builder.py.
_DEFAULT_WOS_AI_ROOT = (
    Path("C:/WoS_AI")
    if sys.platform == "win32"
    else Path(os.path.abspath("IQ_AI"))
)


# ---------------------------------------------------------------------------
# qai pack workspace-init
# ---------------------------------------------------------------------------
def workspace_init_main(argv: Sequence[str] | None = None) -> int:
    """``qai pack workspace-init <model_name>`` — bootstrap a fresh workspace.

    Replaces ``qai_workspace_init.py``: creates the canonical
    ``<wos_ai_root>/<model_name>/`` skeleton (output/ + plan.md
    placeholder) so the conversion pipeline can populate it.
    """
    parser = argparse.ArgumentParser(
        prog="qai_workspace_init.py",
        description="Initialize a clean Model Builder workspace.",
    )
    parser.add_argument(
        "model_name",
        help="Model name; the workspace is created at <wos-ai-root>/<model_name>/.",
    )
    parser.add_argument(
        "--precision",
        action="append",
        default=[],
        dest="precisions",
        help="Precision label to seed (repeatable). Optional.",
    )
    parser.add_argument(
        "--wos-ai-root",
        type=Path,
        default=_DEFAULT_WOS_AI_ROOT,
        help=f"Workspace root (default: {_DEFAULT_WOS_AI_ROOT}).",
    )
    args = parser.parse_args(argv)

    model_name = args.model_name.strip()
    if not model_name:
        parser.error("model_name must not be empty")

    workdir = args.wos_ai_root / model_name
    use_case = InitWorkspaceUseCase(
        workspace_initializer=FileSystemWorkspaceInitializer(
            wos_ai_root=args.wos_ai_root
        ),
    )
    asyncio.run(
        use_case.execute(
            workdir=workdir,
            model_name=model_name,
            precisions=tuple(args.precisions),
        )
    )
    print(f"[OK] Workspace initialized: {workdir}")
    return 0


# ---------------------------------------------------------------------------
# qai pack export
# ---------------------------------------------------------------------------
def pack_export_main(argv: Sequence[str] | None = None) -> int:
    """``qai pack export --workdir <dir> ...`` — emit an App Builder Pack.

    Replaces ``qai_pack_export.py``: reads a completed ModelBuilder
    workspace and writes the ``app_pack/`` candidate. Exit code mirrors
    the legacy contract (0 = pack ready, 1 = validation/export errors).
    """
    parser = argparse.ArgumentParser(
        prog="qai_pack_export.py",
        description="Export a ModelBuilder workspace to an App Builder Pack.",
    )
    parser.add_argument(
        "--workdir",
        required=True,
        type=Path,
        help="ModelBuilder workspace directory (e.g. C:/WoS_AI/<model>).",
    )
    parser.add_argument("--model-name", default=None)
    parser.add_argument("--pack-id", default=None, dest="pack_id_override")
    parser.add_argument("--category", default=None, dest="category_override")
    parser.add_argument(
        "--display-name", default=None, dest="display_name_override"
    )
    parser.add_argument("--input-kind", default=None, dest="input_kind_override")
    parser.add_argument(
        "--output-kind", default=None, dest="output_kind_override"
    )
    parser.add_argument(
        "--precision",
        default=None,
        help="Single-precision (legacy) export. Mutually combinable with --precisions.",
    )
    parser.add_argument(
        "--precisions",
        nargs="+",
        default=None,
        help="Multi-variant precision list (e.g. fp16 int8).",
    )
    parser.add_argument("--default-precision", default=None)
    parser.add_argument(
        "--wos-ai-root",
        type=Path,
        default=_DEFAULT_WOS_AI_ROOT,
        help=f"Workspace root for the traversal guard (default: {_DEFAULT_WOS_AI_ROOT}).",
    )
    args = parser.parse_args(argv)

    # Merge --precision (single) + --precisions (multi) into one tuple,
    # mirroring the legacy script's acceptance of either flag.
    precisions: list[str] = []
    if args.precisions:
        precisions.extend(args.precisions)
    if args.precision and args.precision not in precisions:
        precisions.append(args.precision)

    classifier = RuleAndShapeTaxonomyClassifier()
    exporter = QaiPackExporter(
        classifier=classifier,
        require_qai_appbuilder=False,
    )
    use_case = ExportPackUseCase(
        workspace_reader=WosAiWorkspaceReader(wos_ai_root=args.wos_ai_root),
        pack_exporter=exporter,
        pack_validator=QaiPackValidator(),
    )
    command = ExportPackCommand(
        model_workdir=args.workdir,
        model_name=args.model_name,
        precisions=tuple(precisions),
        default_precision=args.default_precision,
        pack_id_override=args.pack_id_override,
        category_override=args.category_override,
        display_name_override=args.display_name_override,
        input_kind_override=args.input_kind_override,
        output_kind_override=args.output_kind_override,
    )
    result = asyncio.run(use_case.execute(command))

    for line in result.log_lines:
        print(line)
    if result.success:
        print(f"[OK] Pack ready: {result.pack_path}")
        return 0
    print("[FAIL] Pack export reported errors:")
    for err in result.errors:
        print(f"  - {err}")
    return 1


# ---------------------------------------------------------------------------
# qai pack validate
# ---------------------------------------------------------------------------
def pack_validate_main(argv: Sequence[str] | None = None) -> int:
    """``qai pack validate <pack_dir>`` — structural Pack validation.

    Replaces ``qai_pack_validate.py``: prints PASS/FAIL and returns 0
    when the candidate Pack is structurally valid, 1 otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="qai_pack_validate.py",
        description="Validate a candidate App Builder Pack directory.",
    )
    parser.add_argument(
        "pack_dir",
        type=Path,
        help="Candidate Pack directory (e.g. C:/WoS_AI/<model>/app_pack).",
    )
    args = parser.parse_args(argv)

    use_case = ValidatePackUseCase(pack_validator=QaiPackValidator())
    ok, errors = asyncio.run(use_case.execute(pack_dir=args.pack_dir))
    if ok:
        print(f"[PASS] {args.pack_dir}")
        return 0
    print(f"[FAIL] {args.pack_dir}")
    for err in errors:
        print(f"  - {err}")
    return 1


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(pack_validate_main(sys.argv[1:]))
