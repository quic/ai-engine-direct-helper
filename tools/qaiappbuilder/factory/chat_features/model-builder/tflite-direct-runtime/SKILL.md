---
name: tflite-direct-runtime
description: Use when running a custom TensorFlow Lite `.tflite` model directly with QAI AppBuilder on Linux ARM64, Windows, or Android, including QNN LiteRT delegate and CPU-provider selection.
---

# TFLite Direct Runtime

Use this Skill for direct execution of a `.tflite` model. It is a runtime path, not an ONNX-to-QNN conversion workflow: do not send `.tflite` files through `run_pipeline.py` or create a QNN context binary first.

## Choose the supported target

| Target | Build gate | Backend / provider | Notes |
|---|---|---|---|
| Linux ARM64 | `APPBUILDER_ENABLE_TFLITE=ON` | TFLite QNN delegate / `qnn` | Requires `TFLITE_ROOT` and a matching QAIRT `QNN_SDK_ROOT`; attaches the QNN HTP delegate. |
| Windows | `APPBUILDER_ENABLE_TFLITE_CPU=ON` | TFLite CPU / `cpu` | CPU-only; do not claim NPU execution. |
| Android ARM64 | Android TFLite sample | QNN LiteRT delegate, then `gpu` or `cpu` fallback | Report the provider returned by the sample; a fallback is not NPU execution. |

The Linux and Windows build gates are mutually exclusive. Enable them only when the required TFLite artifacts are licensed for redistribution.

## Build prerequisites

Linux ARM64 requires:

- `TFLITE_ROOT/include/tensorflow/lite/c/c_api.h` and a TFLite C API shared library under `TFLITE_ROOT/lib`.
- `QNN_SDK_ROOT/include/QNN/TFLiteDelegate/QnnTFLiteDelegate.h` and the matching `libQnnTFLiteDelegate.so`.

Windows requires the same C API header plus the matching TFLite C API import library and DLL. It does not package or use the QNN TFLite delegate.

## Python usage

```python
import numpy as np
from qai_appbuilder import QNNContext

context = QNNContext(model_name="classifier", model_path="model.tflite")
assert context.getProviderMode() == "qnn"
outputs = context.Inference([np.zeros((1, 224, 224, 3), dtype=np.float32)])
```

`QNNContext` is the public Python API for this path. It detects the `.tflite` suffix and dispatches to the enabled native TFLite binding: QNN delegate on Linux ARM64 or CPU on Windows. On the Windows CPU build, use the same `QNNContext` call; `getProviderMode()` must report `cpu`.

## Verify HTP offload

Report `getProviderMode()` exactly, but do not treat `qnn` as sufficient proof of NPU execution. In the current implementation it means the QNN delegate was attached; TensorFlow Lite can still run unsupported delegated operators on CPU.

For a Linux ARM64 HTP claim, also check the runtime logs:

1. Use one matched QAIRT release for `libQnnTFLiteDelegate.so`, `libQnnHtp.so`, `libQnnHtpPrepare.so`, `libQnnSystem.so`, and the HTP skeleton. Do not mix SDK directories or releases.
2. Confirm that QNN does not report delegate/prepare-library version mismatches or per-operator `validateOpConfig` failures.
3. Confirm that the model's precision is supported by the target SoC. For example, an FP16 model on hardware without FP16 HTP support falls back even though `getProviderMode()` is `qnn`.

If these checks fail, report the run as QNN-delegate attachment with CPU fallback, not as HTP acceleration. Use a compatible quantized model or a target SoC with the required precision support before benchmarking NPU performance.

## Runtime contract

- Direct TFLite contexts contain one interpreter graph. Omit `graphIndex` or pass `graphIndex=0`; any non-zero graph index is invalid.
- `getProfilingEvent()` returns `0`. It is not a timing or hardware-profiling measurement.
- Use the model’s reported input shapes and dtypes. Inputs must be contiguous NumPy arrays with the exact tensor byte size.
- On Android, use the maintained Java sample and surface its actual provider (`qnn-npu`, `gpu`, or `cpu`).

## Common mistakes

| Mistake | Correct action |
|---|---|
| Treating `.tflite` as a QNN context binary | Pass it to `QNNContext`; `QNNContext` auto-dispatches to the enabled native TFLite binding. |
| Calling the Windows path NPU accelerated | Report `cpu`; only Linux ARM64 uses the direct QNN delegate. |
| Calling `getProviderMode() == "qnn"` proof of HTP execution | Inspect QNN logs for a matched runtime and successful operator validation; delegate attachment alone does not prove HTP offload. |
| Combining a delegate from one QAIRT release with HTP libraries from another | Use one matched QAIRT release for every delegate, backend, prepare, system, and skeleton library. |
| Selecting graph 1 or reading profiling event 0 as a measurement | Keep `graphIndex=0` and use an explicit benchmark for timing. |
