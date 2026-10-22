---
name: executorch-direct-runtime
description: Use when running a custom ExecuTorch `.pte` model directly with QAI AppBuilder on Linux ARM64 or Android ARM64, including XNNPACK and optional Qualcomm QNN provider reporting.
---

# ExecuTorch Direct Runtime

Use this Skill for direct execution of an ExecuTorch `.pte` artifact. It is not an AI Hub conversion path and must not be sent through the ONNX/QNN conversion pipeline.

## Support boundary

| Target | Support | Provider choices |
|---|---|---|
| Linux ARM64 | Supported by the opt-in native ExecuTorch build | `cpu`, `xnnpack`, or `qnn` |
| Android ARM64 | Supported by the Android NDK build and sample | `cpu`, `xnnpack`, or `qnn` |
| Windows or non-ARM64 Linux | Not covered by the merged runtime support | Stop and explain that this direct path requires a supported build |

Use a QAI AppBuilder wheel built with the matching ExecuTorch SDK and its shared runtime libraries. A QNN-lowered artifact can use the optional QNN provider only when it matches the target SoC's HTP architecture and deployed QAIRT runtime; otherwise use CPU/XNNPACK.

## Python usage

The Python API dispatches `.pte` through `QNNContext`:

```python
import numpy as np
from qai_appbuilder import QNNContext

context = QNNContext(model_path="model.pte")
outputs = context.Inference([np.zeros((1, 3, 224, 224), dtype=np.float32)])
print(context.getProviderMode())
```

Always report the exact value returned by `getProviderMode()`: `qnn`, `xnnpack`, or `cpu`. Do not describe `cpu` or `xnnpack` execution as QNN/NPU acceleration.

## Required checks

1. Confirm the file is a `.pte` artifact and the host is Linux ARM64 or Android ARM64.
2. Confirm the installed QAI AppBuilder package was built with ExecuTorch support and has its runtime shared libraries available.
3. Inspect the exported model’s expected input names, shapes, and dtypes before constructing NumPy inputs.
4. Run one inference and record `getProviderMode()` with the result.
5. If QNN is requested, confirm the artifact was lowered for QNN and matches the target SoC's HTP architecture and deployed QAIRT runtime; otherwise choose CPU/XNNPACK.

## QNN Llama artifacts

A QNN-lowered Llama `.pte` is not a self-contained generic tensor smoke test. Run it with the matching QNN Llama runner, QNN backend shared library, and compatible QAIRT runtime. It also needs the companion model and tokenizer files documented with the artifact (for example, `stories260K.pt`, `params.json`, `tokenizer.model`, and `tokenizer.bin` for the Stories 260K artifact).

When the runner must be built, use a clean, recursive ExecuTorch clone in an isolated build directory. Enable the QNN backend, LLM runner, and quantized kernels. Do not alter a pre-existing checkout to make this work: a checkout without the `extension/llm/tokenizers` submodule cannot configure the Llama runner.

Before describing this path as QNN execution, confirm the runner selected the QNN backend and that the deployed QAIRT libraries match each other and the target SoC. A portable `executor_runner` success on a small `.pte` proves portable ExecuTorch execution only; it does not validate a QNN Llama artifact.

## Common mistakes

| Mistake | Correct action |
|---|---|
| Passing `.pte` to the ONNX exporter or QNN conversion wrappers | Dispatch the existing `.pte` directly through `QNNContext`. |
| Assuming every `.pte` artifact runs on QNN | Use CPU/XNNPACK unless the artifact was lowered for QNN and matches the target SoC's HTP architecture and QAIRT runtime. |
| Reporting the intended provider rather than the actual one | Report `getProviderMode()` after inference. |
| Treating a QNN Llama `.pte` as self-contained | Supply its documented checkpoint, parameters, and tokenizer files to the QNN Llama runner. |
| Enabling the Llama runner in an incomplete checkout | Build from a clean, recursive ExecuTorch clone that includes `extension/llm/tokenizers`. |
| Calling a portable `executor_runner` smoke test QNN validation | Use the QNN Llama runner and report its selected backend and runtime logs. |
| Attempting the direct path on Windows | Stop; the merged support is Linux ARM64 and Android ARM64 only. |
