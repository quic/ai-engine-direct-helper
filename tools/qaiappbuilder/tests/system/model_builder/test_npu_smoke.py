# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Hardware-in-the-loop system test (opt-in).

Run on a Snapdragon X / QAIRT host with a REAL context binary::

    $env:MB_REAL_WORKDIR = "C:\\WoS_AI\\real_esrgan_x4plus"
    pytest tests/system/model_builder -m npu --no-header -p no:cacheprovider

Verifies the exporter's smoke-test child records a non-empty live
``io_contract`` (the thing App Builder later checks against
``getInputShapes()``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from qai.model_builder.adapters import QaiPackExporter
from qai.model_builder.application.use_cases.export_pack import ExportPackUseCase
from qai.model_builder.adapters import (
    QaiPackValidator, RuleAndShapeTaxonomyClassifier, WosAiWorkspaceReader,
)
from qai.model_builder.domain import ExportPackCommand

pytestmark = [pytest.mark.system, pytest.mark.npu, pytest.mark.slow]

WORKDIR = os.environ.get("MB_REAL_WORKDIR")
_REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _child_uses_repo_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the exporter's smoke-test child import THIS checkout's ``qai`` package.

    The child is a separate interpreter that inherits only ``PYTHONPATH`` (not the ``sys.path`` edits conftest makes
    for pytest itself). Without this it imports whatever ``qai`` is installed in site-packages - on a developer
    machine that can be an older build - so this test would pass or fail on code that is not the code under test.
    """
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in (str(_REPO / "src"), str(_REPO), existing) if p))


@pytest.mark.skipif(not WORKDIR, reason="MB_REAL_WORKDIR not set")
async def test_real_bin_records_live_io_contract():
    wd = Path(WORKDIR)
    exporter = QaiPackExporter(
        classifier=RuleAndShapeTaxonomyClassifier(),
        skip_smoke_test=False,
        require_qai_appbuilder=True,
        qai_appbuilder_shared_dir=Path(
            os.environ.get("MB_SHARED_DIR", "")
        ) or None,
    )
    uc = ExportPackUseCase(
        WosAiWorkspaceReader(wos_ai_root=wd.parent), exporter,
        QaiPackValidator(),
    )
    res = await uc.execute(ExportPackCommand(model_workdir=wd))
    assert res.success, res.errors
    io = json.loads(res.manifest_path.read_text("utf-8"))["io_contract"]
    assert io and io["inputs"] and io["outputs"]
