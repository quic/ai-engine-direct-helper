# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""System tests: workspace -> export_pack -> validate_pack (real adapters)."""

from __future__ import annotations

import json
import py_compile
import shutil
from pathlib import Path

import pytest

from qai.model_builder.domain import (
    ExportPackCommand,
    InvalidPrecisionError,
    MissingContextBinError,
    WorkspaceNotReadyError,
)
from qai.model_builder.adapters._pack_layout import probe_export_status

from .conftest import MIN_BIN, sha256

pytestmark = pytest.mark.system


def _cmd(wd: Path, **kw) -> ExportPackCommand:
    return ExportPackCommand(model_workdir=wd, **kw)


# --------------------------------------------------------------------- happy
async def test_single_precision_end_to_end(stack, make_workspace):
    wd = make_workspace("real_esrgan_x4plus", precisions=("fp16",))
    res = await stack.export_uc.execute(_cmd(wd))

    assert res.success, (res.failed_checks, res.errors)
    pack = wd / "app_pack"
    for rel in ("manifest.json", "runner.py", "requirements.txt",
                "_candidate.json", "weights", "provenance", "examples"):
        assert (pack / rel).exists(), rel

    manifest = json.loads((pack / "manifest.json").read_text("utf-8"))
    assert manifest["modelId"] == res.pack_id
    assert manifest["displayName"] == res.display_name
    py_compile.compile(str(pack / "runner.py"), doraise=True)

    # weights staged + checksum recorded matches the real file
    staged = pack / "weights" / "real_esrgan_x4plus_fp16.bin"
    assert staged.is_file()
    assert res.checks["context_binary_sha256"] == sha256(staged)

    ok, errs = await stack.validator.validate_dir(pack_dir=pack)
    assert ok, errs
    assert probe_export_status(wd) == "generated"
    assert not (pack / ".generating").exists()


async def test_multi_precision_variants_and_default(stack, make_workspace):
    wd = make_workspace("yolov8_det", precisions=("fp16", "w8a8", "w8a16"))
    res = await stack.export_uc.execute(
        _cmd(wd, precisions=("fp16", "int8", "w8a16"),
             default_precision="int8")
    )
    assert res.success, (res.failed_checks, res.errors)
    assert len(res.variants) == 3
    defaults = [v for v in res.variants if v.is_default]
    assert len(defaults) == 1 and defaults[0].precision.plan_key == "w8a8"
    # each variant has distinct weights staged
    names = {p.name for p in res.weights_dir.iterdir()}
    assert names == {
        "yolov8_det_fp16.bin", "yolov8_det_int8.bin", "yolov8_det_w8a16.bin"
    }
    manifest = json.loads(res.manifest_path.read_text("utf-8"))
    assert {v["id"] for v in manifest["variants"]} == {"fp16", "int8", "w8a16"}
    ok, errs = await stack.validator.validate_dir(pack_dir=res.pack_path)
    assert ok, errs


async def test_autodetect_precision_without_plan(stack, make_workspace):
    wd = make_workspace("whisper_tiny", precisions=("fp16",), plan=False)
    res = await stack.export_uc.execute(_cmd(wd))
    assert res.success
    assert [v.precision.plan_key for v in res.variants] == ["fp16"]


async def test_nested_per_precision_directories_are_discovered(
    stack, make_workspace
):
    wd = make_workspace("mobilenet_v3", precisions=("fp16", "w8a8"),
                        nested=True)
    res = await stack.export_uc.execute(
        _cmd(wd, precisions=("fp16", "w8a8"))
    )
    assert len(res.variants) == 2


async def test_overrides_are_honoured(stack, make_workspace):
    wd = make_workspace("some_model")
    res = await stack.export_uc.execute(
        _cmd(wd, pack_id_override="my-custom-id",
             display_name_override="My Custom",
             input_kind_override="audio", output_kind_override="text")
    )
    m = json.loads(res.manifest_path.read_text("utf-8"))
    assert m["modelId"] == "my-custom-id"
    assert m["displayName"] == "My Custom"


# --------------------------------------------------------------- idempotency
async def test_re_export_is_idempotent_and_keeps_user_assets(
    stack, make_workspace
):
    wd = make_workspace("real_esrgan_x4plus")
    r1 = await stack.export_uc.execute(_cmd(wd))
    marker = r1.examples_dir / "user_note.txt"
    marker.write_text("keep me", encoding="utf-8")

    r2 = await stack.export_uc.execute(_cmd(wd))
    assert r2.success and r2.pack_id == r1.pack_id
    assert marker.read_text("utf-8") == "keep me"
    ok, errs = await stack.validator.validate_dir(pack_dir=r2.pack_path)
    assert ok, errs


# ------------------------------------------------------------------ failures
async def test_missing_workspace(stack, wos_root):
    with pytest.raises(WorkspaceNotReadyError):
        await stack.export_uc.execute(_cmd(wos_root / "nope"))


async def test_missing_output_dir(stack, make_workspace):
    wd = make_workspace("m", with_output=False)
    with pytest.raises(WorkspaceNotReadyError):
        await stack.export_uc.execute(_cmd(wd))


@pytest.mark.parametrize("escape", ["..", "../..", "../outside"])
async def test_path_traversal_rejected(stack, wos_root, tmp_path, escape):
    outside = tmp_path / "outside"
    (outside / "output").mkdir(parents=True)
    with pytest.raises(WorkspaceNotReadyError):
        await stack.export_uc.execute(_cmd(wos_root / escape))


async def test_workdir_outside_root_rejected(stack, tmp_path):
    other = tmp_path / "elsewhere" / "m"
    (other / "output").mkdir(parents=True)
    with pytest.raises(WorkspaceNotReadyError):
        await stack.export_uc.execute(_cmd(other))


async def test_stub_bin_below_min_size_is_rejected(stack, make_workspace):
    wd = make_workspace("tiny", bin_size=MIN_BIN - 1)
    with pytest.raises(MissingContextBinError):
        await stack.export_uc.execute(_cmd(wd))
    # failed export must not leave the in-flight sentinel behind
    assert probe_export_status(wd) == "idle"


async def test_requested_precision_not_on_disk(stack, make_workspace):
    wd = make_workspace("m", precisions=("fp16",))
    with pytest.raises(MissingContextBinError):
        await stack.export_uc.execute(_cmd(wd, precisions=("w4a16",)))


async def test_default_precision_not_in_precisions(stack, make_workspace):
    wd = make_workspace("m", precisions=("fp16", "w8a8"))
    with pytest.raises(InvalidPrecisionError):
        await stack.export_uc.execute(
            _cmd(wd, precisions=("fp16",), default_precision="w8a8")
        )


async def test_partial_precisions_soft_error(stack, make_workspace):
    wd = make_workspace("m", precisions=("fp16",))
    res = await stack.export_uc.execute(
        _cmd(wd, precisions=("fp16", "w8a16"), default_precision="fp16")
    )
    assert any("missing precisions" in e for e in res.errors)
    assert [v.precision.plan_key for v in res.variants] == ["fp16"]


async def test_no_report_degrades_but_exports(stack, make_workspace):
    wd = make_workspace("m", report=None)
    res = await stack.export_uc.execute(_cmd(wd))
    assert res.success
    assert res.checks["report_available"] is False
    assert res.checks["cosine_values_found"] is False


# ----------------------------------------------------- validator tamper tests
@pytest.mark.parametrize(
    "mutate, expect",
    [
        (lambda p: (p / "manifest.json").unlink(), "manifest.json missing"),
        (lambda p: (p / "runner.py").write_text("def (:\n"), "does not compile"),
        (lambda p: (p / "requirements.txt").unlink(), "requirements.txt missing"),
        (lambda p: (p / "_candidate.json").unlink(), "_candidate.json missing"),
        (lambda p: (p / "manifest.json").write_text("{not json"), "unreadable"),
        (lambda p: next((p / "weights").iterdir()).write_bytes(b"x" * 10),
         "SHA-256 mismatch"),
        (lambda p: shutil.rmtree(p / "weights"), "weights file not found"),
    ],
    ids=["no-manifest", "bad-runner", "no-reqs", "no-candidate",
         "corrupt-manifest", "weights-tampered", "weights-removed"],
)
async def test_validator_detects_tampering(
    stack, make_workspace, mutate, expect
):
    wd = make_workspace("m")
    res = await stack.export_uc.execute(_cmd(wd))
    pack = res.pack_path
    # weights may be symlinks to the source bin: break the link first so the
    # tamper never mutates the user's original artefact
    for f in (pack / "weights").glob("*"):
        if f.is_symlink():
            data = f.read_bytes()
            f.unlink()
            f.write_bytes(data)
    mutate(pack)
    ok, errs = await stack.validator.validate_dir(pack_dir=pack)
    assert not ok
    assert any(expect in e for e in errs), errs


async def test_validate_nonexistent_dir(stack, tmp_path):
    ok, errs = await stack.validate_uc.execute(pack_dir=tmp_path / "nope")
    assert not ok and errs
