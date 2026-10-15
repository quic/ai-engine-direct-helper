# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared fixtures for the Model Builder system tests.

Strategy
--------
The system tests drive the REAL adapters (workspace reader, taxonomy
classifier, pack exporter, pack validator, workspace initializer) against
synthetic ``<WoS_AI>/<model>/`` workspaces built under ``tmp_path`` - no
mocks inside the ``qai.model_builder`` boundary. Only the NPU smoke test is
switched off (``skip_smoke_test=True``) so the suite runs on any host; the
real-NPU case lives in ``test_npu_smoke.py`` behind the ``npu`` marker.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO, _REPO / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from qai.model_builder.adapters import (  # noqa: E402
    FileSystemWorkspaceInitializer,
    QaiPackExporter,
    QaiPackValidator,
    RuleAndShapeTaxonomyClassifier,
    WosAiWorkspaceReader,
)
from qai.model_builder.application.use_cases.export_pack import (  # noqa: E402
    ExportPackUseCase,
)
from qai.model_builder.application.use_cases.init_workspace import (  # noqa: E402
    InitWorkspaceUseCase,
)
from qai.model_builder.application.use_cases.validate_pack import (  # noqa: E402
    ValidatePackUseCase,
)

MIN_BIN = 1 * 1024 * 1024  # == MIN_CONTEXT_BIN_SIZE


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "system: model_builder system tests")
    config.addinivalue_line("markers", "npu: needs a real NPU + qai_appbuilder")


@dataclass
class Stack:
    root: Path
    reader: WosAiWorkspaceReader
    exporter: QaiPackExporter
    validator: QaiPackValidator
    export_uc: ExportPackUseCase
    validate_uc: ValidatePackUseCase
    init_uc: InitWorkspaceUseCase


@pytest.fixture
def wos_root(tmp_path: Path) -> Path:
    root = tmp_path / "WoS_AI"
    root.mkdir()
    return root


@pytest.fixture
def stack(wos_root: Path) -> Stack:
    classifier = RuleAndShapeTaxonomyClassifier()
    reader = WosAiWorkspaceReader(wos_ai_root=wos_root)
    exporter = QaiPackExporter(
        classifier=classifier,
        skip_smoke_test=True,
        require_qai_appbuilder=False,
    )
    validator = QaiPackValidator()
    return Stack(
        root=wos_root,
        reader=reader,
        exporter=exporter,
        validator=validator,
        export_uc=ExportPackUseCase(reader, exporter, validator),
        validate_uc=ValidatePackUseCase(pack_validator=validator),
        init_uc=InitWorkspaceUseCase(
            workspace_initializer=FileSystemWorkspaceInitializer(
                wos_ai_root=wos_root
            )
        ),
    )


PLAN = """# {name} plan
## Config
```
MODEL_NAME  = {name}
PRECISION   = {precision}   # default precision
```
"""

REPORT_GOOD = """# REPORT
| FP16 | 0.9991 | ok |
Cosine Similarity: 0.9993
Latency: 12.5 ms
"""


@pytest.fixture
def make_workspace(wos_root: Path) -> Callable[..., Path]:
    """Factory: build ``<wos_root>/<name>/`` with the requested artefacts."""

    def _make(
        name: str = "real_esrgan_x4plus",
        *,
        precisions: tuple[str, ...] = ("fp16",),
        bin_size: int = MIN_BIN + 16,
        plan: bool = True,
        plan_precision: str | None = None,
        report: str | None = REPORT_GOOD,
        infer_manifest: dict | None = None,
        with_output: bool = True,
        nested: bool = False,
    ) -> Path:
        wd = wos_root / name
        wd.mkdir(parents=True, exist_ok=True)
        if plan:
            (wd / "plan.md").write_text(
                PLAN.format(
                    name=name, precision=plan_precision or precisions[0]
                ),
                encoding="utf-8",
            )
        if report is not None:
            (wd / "REPORT.md").write_text(report, encoding="utf-8")
        if infer_manifest is not None:
            (wd / "inference_manifest.json").write_text(
                json.dumps(infer_manifest), encoding="utf-8"
            )
        if with_output:
            out = wd / "output"
            out.mkdir(exist_ok=True)
            for p in precisions:
                label = {"w8a8": "int8"}.get(p, p)
                target_dir = out / p if nested else out
                target_dir.mkdir(exist_ok=True)
                f = target_dir / f"{name}_{label}.bin"
                f.write_bytes(
                    (p.encode() * 64).ljust(bin_size, b"\0")  # unique per prec
                )
        return wd

    return _make


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
