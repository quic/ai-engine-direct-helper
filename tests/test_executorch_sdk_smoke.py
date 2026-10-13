# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Smoke test for a wheel built with a real ExecuTorch SDK."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest


_DTYPES = {
    "bool": np.bool_,
    "float16": np.float16,
    "float32": np.float32,
    "float64": np.float64,
    "int8": np.int8,
    "uint8": np.uint8,
    "int16": np.int16,
    "uint16": np.uint16,
    "int32": np.int32,
    "uint32": np.uint32,
    "int64": np.int64,
    "uint64": np.uint64,
}


def test_executorch_sdk_model_loads_and_runs() -> None:
    model_path = os.environ.get("EXECUTORCH_SMOKE_MODEL")
    if not model_path:
        pytest.skip("EXECUTORCH_SMOKE_MODEL is not configured")
    assert Path(model_path).is_file(), model_path

    from qai_appbuilder.qnncontext import QNNContext

    backend_lib_path = os.environ.get("EXECUTORCH_BACKEND_LIB", "None")

    def run_once() -> None:
        context = QNNContext(model_path=model_path, backend_lib_path=backend_lib_path)
        try:
            assert context.isExecuTorchModel()
            input_shapes = context.getInputShapes()
            input_types = context.getInputDataType()
            assert len(input_shapes) == len(input_types)
            inputs = [
                np.zeros(tuple(shape), dtype=_DTYPES[dtype])
                for shape, dtype in zip(input_shapes, input_types)
            ]
            outputs = context.Inference(inputs)
            assert len(outputs) == len(context.getOutputShapes())
        finally:
            context.release()

    run_once()

    if os.environ.get("EXECUTORCH_ASSERT_BACKEND_RETAINED") == "1":
        backend_path = Path(backend_lib_path).resolve()
        assert backend_path.is_file(), backend_path
        maps_path = Path("/proc/self/maps")
        assert maps_path.is_file(), "backend-retention check requires Linux /proc"
        assert str(backend_path) in maps_path.read_text(encoding="utf-8")

    # A second context must remain usable after the first one is released.
    # With an explicit plugin, this also exercises process-lifetime backend
    # ownership rather than unloading and registering the plugin again.
    run_once()
