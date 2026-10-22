# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Contract tests for the direct-runtime documentation skills."""

import ast
from pathlib import Path
import re


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _python_examples(markdown: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", markdown, flags=re.DOTALL)


def test_tflite_direct_runtime_skill_documents_merged_pr_contract():
    skill = (
        REPOSITORY_ROOT
        / "tools/qaiappbuilder/factory/chat_features/model-builder/tflite-direct-runtime/SKILL.md"
    )

    content = skill.read_text(encoding="utf-8")

    assert "name: tflite-direct-runtime" in content
    assert "description: Use when" in content
    assert "APPBUILDER_ENABLE_TFLITE" in content
    assert "APPBUILDER_ENABLE_TFLITE_CPU" in content
    assert "Linux ARM64" in content
    assert "Windows" in content
    assert "graphIndex=0" in content
    assert "qnn" in content
    assert "cpu" in content
    assert "delegate attachment alone does not prove HTP offload" in content
    assert "FP16" in content
    assert "matched QAIRT release" in content

    examples = _python_examples(content)
    assert len(examples) == 1
    example = ast.parse(examples[0])
    imports = [
        alias.name
        for node in ast.walk(example)
        if isinstance(node, ast.ImportFrom) and node.module == "qai_appbuilder"
        for alias in node.names
    ]
    assert imports == ["QNNContext"]
    assert "TFLiteQnnContext" not in imports
    assert "TFLiteCpuContext" not in imports
    assert 'QNNContext(model_name="classifier", model_path="model.tflite")' in examples[0]
    assert "auto-dispatches to the enabled native TFLite binding" in content


def test_executorch_direct_runtime_skill_documents_merged_pr_contract():
    skill = (
        REPOSITORY_ROOT
        / "tools/qaiappbuilder/factory/chat_features/model-builder/executorch-direct-runtime/SKILL.md"
    )

    content = skill.read_text(encoding="utf-8")

    assert "name: executorch-direct-runtime" in content
    assert "description: Use when" in content
    assert ".pte" in content
    assert "Linux ARM64" in content
    assert "Android ARM64" in content
    assert "XNNPACK" in content
    assert "QNN" in content
    assert "`qnn`" in content
    assert "`xnnpack`" in content
    assert "`cpu`" in content
    assert "V81" not in content
    assert "target SoC's HTP architecture" in content
    assert "QNNContext" in content
    assert "getProviderMode()" in content
    assert "clean, recursive ExecuTorch clone" in content
    assert "tokenizers" in content
    assert "QNN Llama runner" in content


def test_model_builder_routes_direct_runtime_formats_to_runtime_skills():
    skill = (
        REPOSITORY_ROOT
        / "tools/qaiappbuilder/factory/chat_features/model-builder/SKILL.md"
    )

    content = skill.read_text(encoding="utf-8")

    assert ".tflite" in content
    assert "tflite-direct-runtime/SKILL.md" in content
    assert ".pte" in content
    assert "executorch-direct-runtime/SKILL.md" in content


def test_readme_describes_tflite_as_direct_runtime_not_conversion():
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    assert "TensorFlow Lite models use the public `QNNContext`\nentry point" in readme
    assert "TensorFlow Lite (`.tflite`) models use the direct runtime Skill" in readme
    assert "`QNNContext` auto-dispatches the model" in readme
    assert "Direct TFLite support is disabled by default" in readme
    assert "[BUILD.md](BUILD.md)" in readme
    assert "Convert PyTorch / ONNX / TensorFlow Lite models" not in readme
