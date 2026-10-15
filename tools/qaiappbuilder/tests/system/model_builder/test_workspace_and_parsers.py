# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""System tests: workspace init, plan/REPORT parsing, precision handling,
taxonomy classification and the export-status probe."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from qai.model_builder.adapters._plan_parser import parse_plan, parse_report
from qai.model_builder.adapters import _pack_layout as layout
from qai.model_builder.domain import Precision

pytestmark = pytest.mark.system


# ------------------------------------------------------------------ init
async def test_init_creates_skeleton_and_is_idempotent(stack, wos_root):
    wd = wos_root / "new_model"
    await stack.init_uc.execute(workdir=wd, model_name="new_model",
                                precisions=("W8A8",))
    assert (wd / "output").is_dir()
    plan = wd / "plan.md"
    assert parse_plan(plan)["MODEL_NAME"] == "new_model"
    assert parse_plan(plan)["PRECISION"] == "w8a8"

    plan.write_text("MODEL_NAME = edited\n", encoding="utf-8")
    await stack.init_uc.execute(workdir=wd, model_name="new_model")
    assert parse_plan(plan)["MODEL_NAME"] == "edited"  # never overwritten


async def test_init_rejects_workdir_outside_root(stack, tmp_path):
    with pytest.raises(ValueError):
        await stack.init_uc.execute(workdir=tmp_path / "evil", model_name="x")


async def test_init_then_read_requires_bin_but_reads_ok(stack, wos_root):
    wd = wos_root / "m2"
    await stack.init_uc.execute(workdir=wd, model_name="m2")
    ws = await stack.reader.read(workdir=wd)
    assert ws.model_name == "m2" and ws.report_path is None
    assert ws.validation_passed is False


# ---------------------------------------------------------------- parsers
@pytest.mark.parametrize("text, expected", [
    ("Cosine Similarity: 0.9987", [0.9987]),
    ("cosine_sim = 0.9912\ncosine_sim = 0.9955", [0.9912, 0.9955]),
    ("ONNX vs W8A8 : 0.99123", [0.99123]),
    ("| W8A16 | **0.9876** | pass |", [0.9876]),
])
def test_parse_report_cosine_formats(tmp_path, text, expected):
    p = tmp_path / "REPORT.md"
    p.write_text(text, encoding="utf-8")
    r = parse_report(p)
    assert r["cosine_similarities"] == expected
    assert r["validation_passed"] is True


@pytest.mark.parametrize("text, expected", [
    ("Latency: 12.5 ms", [12.5]),
    ("p50=5.3 ms p95=7.1 ms", [5.3, 7.1]),
    ("latency_ms = 15.2", [15.2]),
    ("Latency (ms): 9", [9.0]),
    ("Latency_ms: 3.5\nlatency_ms = 4.5", [3.5, 4.5]),
])
def test_parse_report_latency_formats(tmp_path, text, expected):
    p = tmp_path / "REPORT.md"
    p.write_text(text, encoding="utf-8")
    assert parse_report(p)["latencies"] == expected


def test_parse_report_garbage_is_not_a_pass(tmp_path):
    p = tmp_path / "REPORT.md"
    p.write_text("nothing useful here", encoding="utf-8")
    r = parse_report(p)
    assert r["validation_passed"] is False and r["cosine_similarities"] == []


def test_parse_plan_quotes_and_comments(tmp_path):
    p = tmp_path / "plan.md"
    p.write_text('A = "x y"  # c\nB = \'z\'\nlower = no\n', encoding="utf-8")
    assert parse_plan(p) == {"A": "x y", "B": "z"}


# -------------------------------------------------------------- precision
@pytest.mark.parametrize("tok, key, label, fp", [
    ("int8", "w8a8", "int8", False),
    ("W8A8", "w8a8", "int8", False),
    ("fp16", "fp16", "fp16", True),
    ("int4", "w4a8", "int4", False),
    (" FP32 ", "fp32", "fp32", True),
])
def test_precision_normalisation(tok, key, label, fp):
    p = Precision.from_token(tok)
    assert (p.plan_key, p.label, p.is_floating_point) == (key, label, fp)
    assert p.cosine_threshold == (0.99 if fp else 0.95)


@pytest.mark.parametrize("bad", ["", "   ", None, 8])
def test_precision_empty_token_rejected(bad):
    with pytest.raises(ValueError):
        Precision.from_token(bad)


def test_precision_unknown_token_preserved():
    assert Precision.from_token("w4a4").plan_key == "w4a4"
    assert not Precision.is_supported_token("w4a4")


# --------------------------------------------------------------- taxonomy
@pytest.mark.parametrize("name, task", [
    ("real_esrgan_x4plus", "super-resolution"),
    ("yolo-v8", "object-detection"),
    ("yolox", "object-detection"),
    ("whisper_tiny", "speech-recognition"),
    ("mobilenet_v3", "image-classification"),
    ("deeplabv3", "semantic-segmentation"),
])
def test_classifier_rule_hits(stack, name, task):
    r = stack.exporter.classifier.classify(model_name=name,
                                           infer_manifest=None)
    assert (r.task, r.source) == (task, "rule")


@pytest.mark.parametrize(
    "name", ["yolov8_det", "yolov8", "yolov5s", "yolo11n", "YOLOv8n-seg"]
)
def test_classifier_yolo_versions_detected(stack, name):
    r = stack.exporter.classifier.classify(model_name=name,
                                           infer_manifest=None)
    assert r.task == "object-detection"


@pytest.mark.parametrize(
    "name", ["totally_unknown_zzz", "", "x" * 500, "yoloish", "myyolov8"]
)
def test_classifier_unknown_falls_back_without_raising(stack, name):
    r = stack.exporter.classifier.classify(model_name=name,
                                           infer_manifest=None)
    assert (r.task, r.source) == (None, "fallback")


# ------------------------------------------------------------ status probe
def test_status_probe_state_machine(tmp_path):
    wd = tmp_path / "w"
    pack = wd / "app_pack"
    assert layout.probe_export_status(wd) == "idle"

    layout.mark_generating(pack)
    assert layout.probe_export_status(wd) == "generating"

    (pack / layout.CANDIDATE_FILE).write_text("{}", encoding="utf-8")
    assert layout.probe_export_status(wd) == "generated"  # candidate wins

    # starting a new run drops the stale candidate
    layout.mark_generating(pack)
    assert layout.probe_export_status(wd) == "generating"

    layout.clear_generating(pack)
    layout.clear_generating(pack)  # idempotent
    assert layout.probe_export_status(wd) == "idle"


def test_status_probe_stale_sentinel_is_idle(tmp_path):
    wd = tmp_path / "w"
    layout.mark_generating(wd / "app_pack")
    s = wd / "app_pack" / layout.GENERATING_SENTINEL
    old = time.time() - layout.GENERATING_STALE_SECONDS - 60
    import os
    os.utime(s, (old, old))
    assert layout.probe_export_status(wd) == "idle"
