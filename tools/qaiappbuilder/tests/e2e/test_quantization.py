# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L3-lite - quantization through the model-builder pipeline, DLC only, no NPU (MB-05).

``factory/chat_features/model-builder/scripts/run_pipeline.py`` converts an ONNX model to a DLC (``qairt-converter``) and, for a
quantized precision, quantizes it (``qairt-quantizer``). With ``--skip_contextbin`` it stops at the DLC, which runs on the CPU
under the x64 Python 3.10 conversion venv - no HTP / NPU is touched, so this is far cheaper than MB-04.

The model is a 2-layer conv net (1359 bytes) built IN CODE into a temp folder together with 8 random calibration samples; nothing
binary lives in the repo. One quantizer run takes ~15 s (almost all SDK start-up), so the slow scenarios run once, in parallel,
in a module fixture and the tests only read the results (whole module ~1 min).

NOT covered (deliberately):

* **Context binary generation** (the NPU half of MB-04) - ``--skip_contextbin`` is always passed.
* **Numerical accuracy** of the quantized model (cosine vs the ONNX baseline): only "a quantized DLC is produced and differs from
  the FP one" is checked, not that it is good. A 2-layer random net says nothing about real models.
* **Real models / other operators** (MatMul, attention, ...), ``--per_row``, ``--io_config``, ``--quant_overrides``,
  ``--input_dim``, ``--htp_version``, ``--soc_optimized``, 4-bit precisions, ``qai_convert_int.py`` / ``run_pipeline_legacy.py``.
* **"Sensitivity analysis"**: FEATURE-INVENTORY MB-05 once listed it, but the product has no such feature - only a reference
  document (``references/quantization-sensitivity.md``). Nothing to test.

Isolation (verified): every file is written under a ``tempfile`` folder removed in teardown; the real SDK, the repo's
``data/config/qairt_env.json`` and the repo's ``models/`` folder are hashed / listed before and after the module and must not change.
The SDK and the x64 venv are located from ``data/config/qairt_env.json`` (READ only); if either is missing the module is skipped.

Observed behaviour worth pinning:

* ``--act_bw`` / ``--weight_bw`` must come as a pair; a lone one exits 1 BEFORE any SDK tool starts (0.3 s).
* A quantized precision without ``--calib_list`` exits 1 up front; a missing model file or missing list file also exits 1 up front.
  argparse rejects an unknown ``--precision`` / ``--calib_method`` with exit 2.
* An EMPTY calibration list and a list of NON-EXISTENT samples are not caught up front: the quantizer itself fails (exit 1, ~15 s)
  and only the FP ``tiny.dlc`` is left behind. For the empty list the SDK does name the file (``Input file ... was empty``).
* A sample file of the wrong size (4 bytes for a 3072-byte input) and the legacy ``input:=path`` list format are both ACCEPTED and
  produce a quantized DLC (the latter with a ``[WARN]``). A wrong-size sample quantizing silently is worth a look by the owner;
  it is pinned here as observed, not asserted to be right.

Timing (measured on this X Elite, 12 logical CPUs): the 15 slow scenarios take 258 s one after another and ~120 s in the parallel
fixture; a single run is ~15 s alone but 30-55 s when several run at once, so no test asserts a per-run time.
* ``--calib_method percentile`` without ``--percentile_value`` falls back to 99.99; an out-of-range value (50) is rejected by the
  quantizer (exit 1), not by the script.
* ``--bias_bw 7`` is rejected only by the quantizer (exit 1, ~15 s), not up front.
* W8A16 adds ``--restrict_quantization_steps '-0x8000 0x7FFF'``; ``--cle`` adds ``--algorithms cle``; ``--per_channel`` adds
  ``--use_per_channel_quantization``; bias stays FP32 (``--float_bias_bitwidth 32``) unless ``--bias_bw`` is given.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.e2e.conftest import REPO_ROOT

pytestmark = pytest.mark.e2e

SCRIPT = Path(os.environ["QAI_E2E_QUANT_SCRIPT"]) if os.environ.get("QAI_E2E_QUANT_SCRIPT") else (
    REPO_ROOT / "factory" / "chat_features" / "model-builder" / "scripts" / "run_pipeline.py"
)  # the override exists only so mutation checks can point at a modified COPY of the script
ENV_JSON = REPO_ROOT / "data" / "config" / "qairt_env.json"
MODELS_DIR = REPO_ROOT / "models"
SLOW_TIMEOUT_S = 180.0
FAST_TIMEOUT_S = 60.0


@dataclass
class Run:
    code: int
    out: str
    dir: Path
    seconds: float
    dlcs: dict[str, int] = field(default_factory=dict)
    quantizer_cmd: str = ""
    converter_cmd: str = ""


def _sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def _tree(p: Path) -> list[str] | None:
    return sorted(str(x.relative_to(p)) for x in p.rglob("*")) if p.is_dir() else None


def _read_env() -> dict:
    try:
        v = json.loads(ENV_JSON.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _x64_python() -> Path | None:
    v = _read_env().get("python_x64_venv")
    p = Path(v) / "Scripts" / "python.exe" if v else None
    return p if p and p.is_file() else None


def _sdk_root() -> Path | None:
    cfg = _read_env()
    v = cfg.get("qairt_sdk_root") or cfg.get("qairt_root")
    p = Path(v) if v else None
    return p if p and p.is_dir() else None


def _build_model(work: Path, py: Path) -> None:
    """Build tiny.onnx + calibration files with the x64 venv's own ``onnx`` (the test interpreter may not have it)."""
    code = r'''
import os, sys, numpy as np, onnx
from onnx import helper, TensorProto, numpy_helper
work = sys.argv[1]
rng = np.random.default_rng(0)
t = lambda n, s, k=0.2: numpy_helper.from_array((rng.standard_normal(s) * k).astype(np.float32), n)
x = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, 16, 16])
y = helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 4, 16, 16])
nodes = [helper.make_node("Conv", ["input", "w1", "b1"], ["c1"], pads=[1, 1, 1, 1], kernel_shape=[3, 3]),
         helper.make_node("Relu", ["c1"], ["r1"]),
         helper.make_node("Conv", ["r1", "w2", "b2"], ["c2"], pads=[1, 1, 1, 1], kernel_shape=[3, 3]),
         helper.make_node("Relu", ["c2"], ["output"])]
inits = [t("w1", (4, 3, 3, 3)), numpy_helper.from_array(np.zeros(4, np.float32), "b1"), t("w2", (4, 4, 3, 3)), numpy_helper.from_array(np.zeros(4, np.float32), "b2")]
m = helper.make_model(helper.make_graph(nodes, "tiny", [x], [y], inits), opset_imports=[helper.make_opsetid("", 13)])
m.ir_version = 8
onnx.checker.check_model(m)
onnx.save(m, os.path.join(work, "tiny.onnx"))
os.makedirs(os.path.join(work, "calib"), exist_ok=True)
paths = []
for i in range(8):
    p = os.path.join(work, "calib", "s%d.raw" % i)
    rng.standard_normal((1, 3, 16, 16)).astype(np.float32).tofile(p)
    paths.append(p)
open(os.path.join(work, "calib.txt"), "w").write("\n".join(paths) + "\n")
open(os.path.join(work, "empty.txt"), "w").write("")
open(os.path.join(work, "nonexistent.txt"), "w").write("\n".join(os.path.join(work, "calib", "nope%d.raw" % i) for i in range(2)) + "\n")
open(os.path.join(work, "prefixed.txt"), "w").write("\n".join("input:=" + p for p in paths[:2]) + "\n")
open(os.path.join(work, "calib", "short.raw"), "wb").write(bytes([1, 2, 3, 4]))
open(os.path.join(work, "short.txt"), "w").write(os.path.join(work, "calib", "short.raw") + "\n")
'''
    p = subprocess.run([str(py), "-c", code, str(work)], capture_output=True, text=True, timeout=FAST_TIMEOUT_S)
    if p.returncode != 0:
        pytest.skip(f"cannot build the test model with the x64 venv's onnx: {p.stderr.strip().splitlines()[-1:]}")


def _cmd_of(log: str, tool: str) -> str:
    m = re.search(r"\[CMD\][^\n]*" + tool + r"'? ([^\n]*)", log)
    return m.group(1) if m else ""


def _run(py: Path, work: Path, name: str, args: list[str], *, timeout: float) -> Run:
    out_dir = work / f"out_{name}"
    t0 = time.monotonic()
    p = subprocess.run([str(py), str(SCRIPT), "--model", str(work / "tiny.onnx"), "--output", str(out_dir), "--skip_contextbin", *args],
                       cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    log = p.stdout + p.stderr
    dlcs = {f.name: f.stat().st_size for f in out_dir.glob("*.dlc")} if out_dir.is_dir() else {}
    return Run(p.returncode, log, out_dir, time.monotonic() - t0, dlcs, _cmd_of(log, "qairt-quantizer"), _cmd_of(log, "qairt-converter"))


@pytest.fixture(scope="module", autouse=True)
def product_untouched() -> Iterator[None]:
    before = (_sha(ENV_JSON), _tree(MODELS_DIR))
    sdk = _sdk_root()
    sdk_before = sorted(x.name for x in sdk.iterdir()) if sdk else None
    yield
    assert (_sha(ENV_JSON), _tree(MODELS_DIR)) == before, "data/config/qairt_env.json or models/ changed during this module"
    assert (sorted(x.name for x in sdk.iterdir()) if sdk else None) == sdk_before, "the QAIRT SDK folder changed"


@pytest.fixture(scope="module")
def work() -> Iterator[Path]:
    py, sdk = _x64_python(), _sdk_root()
    if py is None:
        pytest.skip("the x64 Python 3.10 conversion venv (python_x64_venv in data/config/qairt_env.json) is not installed")
    if sdk is None:
        pytest.skip("the QAIRT SDK (qairt_sdk_root in data/config/qairt_env.json) is not installed")
    if not SCRIPT.is_file():
        pytest.skip(f"{SCRIPT} not found")
    d = Path(tempfile.mkdtemp(prefix="qai-quant-"))
    try:
        _build_model(d, py)
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def py(work: Path) -> Path:
    p = _x64_python()
    assert p is not None
    return p


SLOW = {
    "w8a8":        ["--precision", "w8a8", "--calib_list", "{calib}"],
    "w8a16":       ["--precision", "w8a16", "--calib_list", "{calib}"],
    "fp16":        ["--precision", "fp16"],
    "bf16":        ["--precision", "bf16"],
    "cle":         ["--precision", "w8a8", "--calib_list", "{calib}", "--cle"],
    "per_channel": ["--precision", "w8a8", "--calib_list", "{calib}", "--per_channel"],
    "percentile":  ["--precision", "w8a8", "--calib_list", "{calib}", "--calib_method", "percentile", "--percentile_value", "99.9"],
    "custom":      ["--act_bw", "8", "--weight_bw", "8", "--bias_bw", "32", "--calib_list", "{calib}"],
    "dump":        ["--precision", "w8a8", "--calib_list", "{calib}", "--dump_encoding"],
    "empty_list":  ["--precision", "w8a8", "--calib_list", "{empty}"],
    "no_samples":  ["--precision", "w8a8", "--calib_list", "{nonexistent}"],
    "prefixed":    ["--precision", "w8a8", "--calib_list", "{prefixed}"],
    "short_sample": ["--precision", "w8a8", "--calib_list", "{short}"],
    "bad_bias":    ["--act_bw", "8", "--weight_bw", "8", "--bias_bw", "7", "--calib_list", "{calib}"],
    "pct_50":      ["--precision", "w8a8", "--calib_list", "{calib}", "--calib_method", "percentile", "--percentile_value", "50"],
}


@pytest.fixture(scope="module")
def slow(work: Path, py: Path) -> dict[str, Run]:
    """Every ~15 s scenario, run in parallel once (12 CPUs here; each run is one SDK process pair)."""
    subst = {"{calib}": str(work / "calib.txt"), "{empty}": str(work / "empty.txt"), "{nonexistent}": str(work / "nonexistent.txt"),
             "{prefixed}": str(work / "prefixed.txt"), "{short}": str(work / "short.txt")}

    def go(item: tuple[str, list[str]]) -> tuple[str, Run]:
        name, args = item
        return name, _run(py, work, name, [subst.get(a, a) for a in args], timeout=SLOW_TIMEOUT_S)

    with ThreadPoolExecutor(max_workers=6) as ex:
        return dict(ex.map(go, SLOW.items()))


def fast(py: Path, work: Path, name: str, *args: str) -> Run:
    return _run(py, work, name, list(args), timeout=FAST_TIMEOUT_S)


# --------------------------------------------------------------------------
# the happy path: a quantized DLC comes out
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name,suffix", [("w8a8", "tiny_w8a8.dlc"), ("w8a16", "tiny_w8a16.dlc")])
def test_quantized_precision_produces_a_quantized_dlc_next_to_the_fp_one(slow: dict[str, Run], name: str, suffix: str) -> None:
    r = slow[name]
    assert r.code == 0, r.out[-800:]
    assert set(r.dlcs) == {"tiny.dlc", suffix}
    assert r.dlcs[suffix] > 0 and r.dlcs[suffix] != r.dlcs["tiny.dlc"], "the quantized DLC is no different from the FP one"
    assert "Binary   : (skipped, DLC-only)" in r.out


@pytest.mark.parametrize("name", ["fp16", "bf16"])
def test_float_precisions_produce_only_the_converted_dlc(slow: dict[str, Run], name: str) -> None:
    r = slow[name]
    assert r.code == 0, r.out[-800:]
    assert set(r.dlcs) == {"tiny.dlc"} and r.quantizer_cmd == "", "a float precision must not run the quantizer"


def test_float_precisions_pass_their_width_to_the_converter(slow: dict[str, Run]) -> None:
    assert slow["fp16"].converter_cmd.endswith("--float_bitwidth 16"), slow["fp16"].converter_cmd
    assert slow["bf16"].converter_cmd.endswith("--float_bitwidth bf16"), slow["bf16"].converter_cmd


def test_custom_bitwidths_name_the_output_after_all_three_widths(slow: dict[str, Run]) -> None:
    r = slow["custom"]
    assert r.code == 0, r.out[-800:]
    assert set(r.dlcs) == {"tiny.dlc", "tiny_w8a8b32.dlc"}


def test_dump_encoding_also_writes_an_encoding_json(slow: dict[str, Run]) -> None:
    r = slow["dump"]
    assert r.code == 0, r.out[-800:]
    enc = r.dir / "tiny_w8a8_encoding.json"
    assert enc.is_file() and enc.stat().st_size > 0
    assert isinstance(json.loads(enc.read_text(encoding="utf-8")), (dict, list))


# --------------------------------------------------------------------------
# what each option really passes to the SDK tools
# --------------------------------------------------------------------------


def test_default_quantization_keeps_bias_in_fp32(slow: dict[str, Run]) -> None:
    c = slow["w8a8"].quantizer_cmd
    assert "--act_bitwidth 8" in c and "--weights_bitwidth 8" in c and "--float_bias_bitwidth 32" in c
    assert "--algorithms" not in c and "--use_per_channel_quantization" not in c


def test_w8a16_activations_are_16_bit_with_restricted_steps(slow: dict[str, Run]) -> None:
    c = slow["w8a16"].quantizer_cmd
    assert "--act_bitwidth 16" in c and "--weights_bitwidth 8" in c
    assert "--restrict_quantization_steps" in c and "0x7FFF" in c


def test_cle_is_forwarded_as_an_algorithm(slow: dict[str, Run]) -> None:
    r = slow["cle"]
    assert r.code == 0, r.out[-800:]
    assert "--algorithms cle" in r.quantizer_cmd


def test_per_channel_is_forwarded(slow: dict[str, Run]) -> None:
    r = slow["per_channel"]
    assert r.code == 0, r.out[-800:]
    assert "--use_per_channel_quantization" in r.quantizer_cmd


def test_percentile_calibration_with_a_value_is_forwarded(slow: dict[str, Run]) -> None:
    r = slow["percentile"]
    assert r.code == 0, r.out[-800:]
    c = r.quantizer_cmd
    assert "--act_quantizer_calibration percentile" in c and "--param_quantizer_calibration percentile" in c
    assert "--percentile_calibration_value 99.9" in c


def test_explicit_bias_width_replaces_the_fp32_default(slow: dict[str, Run]) -> None:
    c = slow["custom"].quantizer_cmd
    assert "--bias_bitwidth 32" in c and "--float_bias_bitwidth" not in c


# --------------------------------------------------------------------------
# rejected up front (no SDK tool starts)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("args", [["--act_bw", "8"], ["--weight_bw", "8"]])
def test_a_lone_custom_bitwidth_is_rejected_up_front(work: Path, py: Path, args: list[str]) -> None:
    r = fast(py, work, "lone", *args, "--calib_list", str(work / "calib.txt"))
    assert r.code == 1 and "--act_bw and --weight_bw must both be specified" in r.out
    assert r.dlcs == {} and r.seconds < 30


def test_a_quantized_precision_needs_a_calibration_list(work: Path, py: Path) -> None:
    r = fast(py, work, "nocal", "--precision", "w8a8")
    assert r.code == 1 and "--calib_list required for quantized precision (w8a8)" in r.out
    assert r.dlcs == {}


def test_a_missing_calibration_list_file_is_rejected_up_front(work: Path, py: Path) -> None:
    r = fast(py, work, "nolist", "--precision", "w8a8", "--calib_list", str(work / "does-not-exist.txt"))
    assert r.code == 1 and "Calibration list not found" in r.out and r.dlcs == {}


def test_a_missing_model_file_is_rejected_up_front(work: Path, py: Path) -> None:
    out = work / "out_nomodel"
    p = subprocess.run([str(py), str(SCRIPT), "--model", str(work / "nope.onnx"), "--output", str(out), "--precision", "fp16", "--skip_contextbin"],
                       cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=FAST_TIMEOUT_S)
    assert p.returncode == 1 and "Model not found" in p.stdout + p.stderr
    assert not out.exists() or not list(out.glob("*.dlc"))


@pytest.mark.parametrize("flag,value", [("--precision", "w9a9"), ("--calib_method", "zzz")])
def test_unknown_choices_are_rejected_by_argparse_with_exit_2(work: Path, py: Path, flag: str, value: str) -> None:
    r = fast(py, work, "badchoice", "--precision", "w8a8", "--calib_list", str(work / "calib.txt"), flag, value)
    assert r.code == 2 and "invalid choice" in r.out and r.dlcs == {}


# --------------------------------------------------------------------------
# rejected only by the quantizer (slow failure, FP DLC left behind)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["empty_list", "no_samples", "bad_bias", "pct_50"])
def test_quantizer_side_failures_exit_1_and_leave_only_the_fp_dlc(slow: dict[str, Run], name: str) -> None:
    r = slow[name]
    assert r.code == 1, f"{name}: expected exit 1, got {r.code}\n{r.out[-600:]}"
    assert "qairt-quantizer failed" in r.out or "Error" in r.out
    assert set(r.dlcs) == {"tiny.dlc"}, f"{name}: a quantized DLC must not exist after a failed quantization: {r.dlcs}"


def test_bad_bias_width_error_names_the_valid_widths(slow: dict[str, Run]) -> None:
    assert "Invalid bias bitwidth: 7" in slow["bad_bias"].out and "8 or 32" in slow["bad_bias"].out


def test_empty_list_failure_names_the_empty_file(slow: dict[str, Run]) -> None:
    """The script does not check the list itself; the SDK's message is what tells the user which file was empty."""
    assert "empty.txt was empty" in slow["empty_list"].out


# --------------------------------------------------------------------------
# accepted although questionable (pinned as observed)
# --------------------------------------------------------------------------


def test_legacy_prefixed_list_is_accepted_with_a_warning(slow: dict[str, Run]) -> None:
    r = slow["prefixed"]
    assert r.code == 0, r.out[-800:]
    assert "[WARN]" in r.out and "input_name:=path" in r.out
    assert "tiny_w8a8.dlc" in r.dlcs


def test_a_sample_of_the_wrong_size_is_accepted(slow: dict[str, Run]) -> None:
    r = slow["short_sample"]
    assert r.code == 0, r.out[-800:]
    assert "tiny_w8a8.dlc" in r.dlcs

