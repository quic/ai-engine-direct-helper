# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Convert -> export, end to end, on a model built in code (opt-in: needs QAIRT + a Hexagon NPU).

The chain this covers, with nothing hand-made in the middle::

    ONNX (built here) --run_pipeline.py--> DLC --qai_dev_gen_contextbin.py--> real .bin
        --QaiPackExporter (smoke test ON)--> app_pack/ with manifest.io_contract
        --QaiPackValidator--> the pack validates

The other system tests use a synthetic placeholder ``.bin`` and switch the smoke test off, so "a genuine context
binary yields the right ``manifest.io_contract``" was only checked by ``test_npu_smoke.py``, which needs a model
the developer already converted. This test converts one itself, so it needs nothing outside the repo, and it asserts
the EXACT shapes of the network it built, not merely "non-empty".

Why the network is this size: the exporter rejects any context binary under ``MIN_CONTEXT_BIN_SIZE`` (1 MiB) as a
stub. A 2-layer toy converts to a ~33 KB ``.bin`` and is refused, so this builds 5 conv layers of 160 channels
(~0.7 M parameters, ~1.5 MiB ``.bin``). The size gate itself is NOT bypassed; the test asserts the ``.bin`` is above it.

Skips (never fails) when the environment cannot do the job: no ``qairt_env.json``, no QAIRT SDK, no x64 conversion
venv, no ``onnx`` in that venv, a host without a Hexagon NPU, or no ``qai_appbuilder``. Once all of that is present,
a problem is a failure.

NOT covered (deliberately):

* **Quantized precisions.** ``w8a8`` converts and produces a ``.bin``, but the exporter's zero-tensor probe cannot
  synthesize inputs for a quantized dtype (``unknown dtype 'ufp8'``), so export fails. That is a product limitation
  to fix, not something to pin here.
* **Numerical accuracy** of the model, and **real-world operators**; the net is conv + relu with random weights.
* **Running inference on the NPU.** Only loading the ``.bin`` and reading its tensor shapes (the smoke test).

Isolation (verified by the module fixture): every file is written under ``tmp_path`` (removed by pytest);
``data/config/qairt_env.json``, the repo's ``models/`` folder and the QAIRT SDK folder are hashed / listed before and
after and must not change. The product's ``C:\\WoS_AI`` is never touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from qai.model_builder.adapters import (
    QaiPackExporter,
    QaiPackValidator,
    RuleAndShapeTaxonomyClassifier,
    WosAiWorkspaceReader,
)
from qai.model_builder.application.use_cases.export_pack import ExportPackUseCase
from qai.model_builder.domain import ExportPackCommand
from qai.model_builder.domain.value_objects import MIN_CONTEXT_BIN_SIZE

pytestmark = [pytest.mark.system, pytest.mark.npu, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[3]
ENV_JSON = REPO / "data" / "config" / "qairt_env.json"
MODELS_DIR = REPO / "models"
SCRIPT = Path(os.environ["MB_PIPELINE_SCRIPT"]) if os.environ.get("MB_PIPELINE_SCRIPT") else (
    REPO / "factory" / "chat_features" / "model-builder" / "scripts" / "run_pipeline.py"
)  # the override exists only so a mutation check can point at a modified COPY of the script
SHARED_DIR = REPO / "factory" / "chat_features" / "app-builder" / "shared"
CONVERT_TIMEOUT_S = 300.0

# The network built below. These numbers are the expected tensor contract.
CHANNELS = 160
IN_SHAPE = [1, 3, 16, 16]
OUT_SHAPE = [1, 4, 16, 16]
MODEL_NAME = "tiny"  # the ONNX graph is named after the file; the .bin / graph_name follow it

_BUILD_MODEL = r'''
import os, sys, numpy as np, onnx
from onnx import helper, TensorProto, numpy_helper
out, C = sys.argv[1], int(sys.argv[2])
rng = np.random.default_rng(0)
w = lambda n, s: numpy_helper.from_array((rng.standard_normal(s) * 0.05).astype(np.float32), n)
z = lambda n, k: numpy_helper.from_array(np.zeros(k, np.float32), n)
chans = [3, C, C, C, C, 4]
nodes, inits, prev = [], [], "input"
for i in range(5):
    cin, cout = chans[i], chans[i + 1]
    o = "output" if i == 4 else "r%d" % i
    nodes.append(helper.make_node("Conv", [prev, "w%d" % i, "b%d" % i], ["c%d" % i], pads=[1, 1, 1, 1], kernel_shape=[3, 3]))
    nodes.append(helper.make_node("Relu", ["c%d" % i], [o]))
    inits += [w("w%d" % i, (cout, cin, 3, 3)), z("b%d" % i, cout)]
    prev = o
x = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, 16, 16])
y = helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 4, 16, 16])
m = helper.make_model(helper.make_graph(nodes, "tiny", [x], [y], inits), opset_imports=[helper.make_opsetid("", 13)])
m.ir_version = 8
onnx.checker.check_model(m)
onnx.save(m, out)
'''


def _sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def _tree(p: Path) -> list[str] | None:
    return sorted(str(x.relative_to(p)) for x in p.rglob("*")) if p.is_dir() else None


def _env() -> dict:
    try:
        v = json.loads(ENV_JSON.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _tail(text: str, lines: int = 25) -> str:
    return "\n".join(text.splitlines()[-lines:])


@pytest.fixture(scope="module")
def toolchain() -> tuple[Path, Path]:
    """(x64 conversion python, QAIRT SDK root). Skips when the machine cannot convert + load a model."""
    if platform.system() != "Windows" or platform.machine().lower() not in ("arm64", "aarch64"):
        pytest.skip("needs a Windows-on-Snapdragon (arm64) host: the .bin is generated for the local Hexagon NPU")
    cfg = _env()
    venv = cfg.get("python_x64_venv")
    py = Path(venv) / "Scripts" / "python.exe" if venv else None
    if py is None or not py.is_file():
        pytest.skip("the x64 conversion venv (python_x64_venv in data/config/qairt_env.json) is not installed")
    sdk_s = cfg.get("qairt_sdk_root") or cfg.get("qairt_root")
    sdk = Path(sdk_s) if sdk_s else None
    if sdk is None or not sdk.is_dir():
        pytest.skip("the QAIRT SDK (qairt_sdk_root in data/config/qairt_env.json) is not installed")
    if not SCRIPT.is_file():
        pytest.skip(f"the conversion script is missing: {SCRIPT}")
    if not SHARED_DIR.is_dir():
        pytest.skip(f"the App Builder shared modules are missing: {SHARED_DIR}")
    try:
        import qai_appbuilder  # noqa: F401
    except ImportError:
        pytest.skip("qai_appbuilder is not importable in the test interpreter (the exporter's smoke test needs it)")
    return py, sdk


@pytest.fixture(scope="module", autouse=True)
def child_uses_repo_source() -> Iterator[None]:
    """Make the exporter's smoke-test child import THIS checkout's ``qai`` package.

    The child is a separate interpreter that inherits only ``PYTHONPATH`` (not the ``sys.path`` edits conftest makes
    for pytest itself). Without this it imports whatever ``qai`` is installed in the interpreter's site-packages -
    which on a developer machine can be an older build - and the test would validate that instead of the code under test.
    """
    mp = pytest.MonkeyPatch()
    existing = os.environ.get("PYTHONPATH", "")
    mp.setenv("PYTHONPATH", os.pathsep.join(p for p in (str(REPO / "src"), str(REPO), existing) if p))
    yield
    mp.undo()


@pytest.fixture(scope="module", autouse=True)
def product_untouched() -> Iterator[None]:
    sdk = _env().get("qairt_sdk_root") or _env().get("qairt_root")
    sdk_p = Path(sdk) if sdk else None
    before = (_sha(ENV_JSON), _tree(MODELS_DIR), sorted(x.name for x in sdk_p.iterdir()) if sdk_p and sdk_p.is_dir() else None)
    yield
    after = (_sha(ENV_JSON), _tree(MODELS_DIR), sorted(x.name for x in sdk_p.iterdir()) if sdk_p and sdk_p.is_dir() else None)
    assert after[0] == before[0], "data/config/qairt_env.json changed during this module"
    assert after[1] == before[1], "the repo's models/ folder changed during this module"
    assert after[2] == before[2], "the QAIRT SDK folder changed during this module"


@pytest.fixture(scope="module")
def converted(tmp_path_factory: pytest.TempPathFactory, toolchain: tuple[Path, Path]) -> dict:
    """Build the ONNX and convert it to a real FP16 context binary ONCE (~25 s); the tests only read the result."""
    py, _sdk = toolchain
    work = tmp_path_factory.mktemp("convert")
    onnx_path = work / f"{MODEL_NAME}.onnx"
    p = subprocess.run([str(py), "-c", _BUILD_MODEL, str(onnx_path), str(CHANNELS)],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        pytest.skip(f"cannot build the test model with the x64 venv's onnx: {_tail(p.stderr, 3)}")
    out = work / "out"
    t0 = time.monotonic()
    r = subprocess.run([str(py), str(SCRIPT), "--model", str(onnx_path), "--output", str(out)],
                       cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=CONVERT_TIMEOUT_S)
    log = r.stdout + r.stderr
    bins = sorted(out.glob("*.bin")) if out.is_dir() else []
    return {"code": r.returncode, "log": log, "out": out, "bins": bins, "dlcs": sorted(out.glob("*.dlc")) if out.is_dir() else [],
            "seconds": time.monotonic() - t0, "work": work}


def test_conversion_produces_a_context_binary_above_the_stub_threshold(converted: dict) -> None:
    assert converted["code"] == 0, f"run_pipeline.py exited {converted['code']}:\n{_tail(converted['log'])}"
    assert "[STEP 3] DLC -> context binary" in converted["log"], "the context-binary step did not run"
    assert [b.name for b in converted["bins"]] == [f"{MODEL_NAME}_fp16.bin"], converted["bins"]
    size = converted["bins"][0].stat().st_size
    assert size >= MIN_CONTEXT_BIN_SIZE, (
        f"the generated .bin is {size:,} bytes, under the {MIN_CONTEXT_BIN_SIZE:,}-byte stub threshold; "
        f"the test network is too small (CHANNELS={CHANNELS})"
    )
    assert converted["dlcs"], "no DLC was left behind"


async def test_exported_pack_records_the_exact_tensor_contract_of_the_network(
    converted: dict, toolchain: tuple[Path, Path], tmp_path: Path
) -> None:
    assert converted["bins"], f"conversion produced no .bin:\n{_tail(converted['log'])}"
    wd = tmp_path / "WoS_AI" / MODEL_NAME
    (wd / "output").mkdir(parents=True)
    for f in converted["out"].iterdir():
        if f.is_file():
            shutil.copy2(f, wd / "output" / f.name)

    exporter = QaiPackExporter(
        classifier=RuleAndShapeTaxonomyClassifier(),
        skip_smoke_test=False,            # the point of this test: really load the .bin
        require_qai_appbuilder=True,
        qai_appbuilder_shared_dir=SHARED_DIR,
    )
    uc = ExportPackUseCase(WosAiWorkspaceReader(wos_ai_root=wd.parent), exporter, QaiPackValidator())
    res = await uc.execute(ExportPackCommand(model_workdir=wd))
    assert res.success, res.errors
    assert res.manifest_path is not None

    manifest = json.loads(res.manifest_path.read_text("utf-8"))
    io = manifest["io_contract"]
    assert io is not None, "io_contract is null: the smoke test did not run"
    assert io["validated_at_export"] is True
    assert io["graph_name"] == MODEL_NAME
    assert [(t["name"], t["dtype"], t["shape"]) for t in io["inputs"]] == [("input", "float16", IN_SHAPE)]
    assert [(t["name"], t["dtype"], t["shape"]) for t in io["outputs"]] == [("output", "float16", OUT_SHAPE)]
    # byte_size must agree with shape x dtype (2 bytes per float16 element), not just be present.
    assert io["inputs"][0]["byte_size"] == 2 * IN_SHAPE[0] * IN_SHAPE[1] * IN_SHAPE[2] * IN_SHAPE[3]
    assert io["outputs"][0]["byte_size"] == 2 * OUT_SHAPE[0] * OUT_SHAPE[1] * OUT_SHAPE[2] * OUT_SHAPE[3]

    ok, errs = await QaiPackValidator().validate_dir(pack_dir=res.pack_path)
    assert ok, errs
    assert (res.pack_path / "weights" / converted["bins"][0].name).is_file(), "the converted .bin was not staged into the pack"
