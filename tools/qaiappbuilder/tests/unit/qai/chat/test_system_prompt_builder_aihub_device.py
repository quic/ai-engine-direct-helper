# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""AI Hub Device skill discovery and intent-routing contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.api._chat_feature_skill_provider import FeatureSkillProvider, TOOL_MODE_DIR_MAP
from qai.chat.adapters.system_prompt_builder import (
    RichSystemPromptBuilder,
    _auto_detect_tool_mode,
)
from qai.chat.application.ports import SystemPromptRequest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "message",
    [
        "compile my model on AI Hub",
        "profile my model via AI Hub",
        "compile an AI Hub model",
        "profile this AI Hub model",
        "upload my model to Qualcomm AI Hub",
        "show me the AI Hub device list",
        "compile for Snapdragon 8 Gen 3",
        "run cloud profiling of MobileNet without physical hardware",
        "请在 AI Hub 云端编译这个模型",
    ],
)
def test_cloud_device_operations_route_to_aihub_device(message: str) -> None:
    assert _auto_detect_tool_mode(message) == "aihub-device"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("download ResNet50 from AI Hub", "model-hub"),
        ("从 Qualcomm AI-Hub 下载 inception", "model-hub"),
        ("run the melotts voice_ai package", "model-hub"),
        ("I have the device, compile locally", "model_build"),
        ("I own the hardware; compile my model on AI Hub", "model_build"),
        ("compile an AI Hub model locally", "model_build"),
        ("compile my ONNX model. What is AI Hub?", None),
        ("convert my ONNX model to QNN", "model_build"),
        ("AI Hub 的 yolo 模型怎么量化", None),
    ],
)
def test_neighboring_workflows_keep_their_routes(message: str, expected: str | None) -> None:
    assert _auto_detect_tool_mode(message) == expected


def test_explicit_app_builder_mode_is_not_overridden_by_aihub_words() -> None:
    builder = RichSystemPromptBuilder(feature_skill_provider=lambda _: "# App Builder")
    result = builder.build(
        SystemPromptRequest(
            tool_mode="app-builder",
            extra={"latest_user_message": "build an app that uses an AI Hub model"},
        )
    )

    assert result.effective_tool_mode == "app-builder"


def test_cloud_profile_precedes_overlapping_model_hub_download() -> None:
    message = "download resnet50 from AI Hub and profile it on AI Hub"
    assert _auto_detect_tool_mode(message) == "aihub-device"


def test_auto_detected_aihub_device_uses_canonical_feature_skill() -> None:
    skill = "AI HUB DEVICE SKILL: submit cloud compilation and profiling jobs."
    requested_modes: list[str | None] = []

    def provider(tool_mode: str | None) -> str | None:
        requested_modes.append(tool_mode)
        return skill if tool_mode == "aihub-device" else None

    result = RichSystemPromptBuilder(feature_skill_provider=provider).build(
        SystemPromptRequest(
            extra={"latest_user_message": "Profile my model on AI Hub"},
        )
    )

    assert result.effective_tool_mode == "aihub-device"
    assert requested_modes == ["aihub-device"]
    assert result.skill_body == skill


def test_provider_discovers_hyphen_and_underscore_aliases(tmp_path: Path) -> None:
    skill_dir = tmp_path / "aihub-device"
    skill_dir.mkdir()
    skill_dir.joinpath("SKILL.md").write_text("# AI Hub Device\n", encoding="utf-8")
    provider = FeatureSkillProvider(features_dir=tmp_path)

    assert TOOL_MODE_DIR_MAP["aihub-device"] == "aihub-device"
    assert provider("aihub-device") == "# AI Hub Device\n"
    assert provider("aihub_device") == "# AI Hub Device\n"


@pytest.mark.parametrize(
    ("locale", "display_name"),
    [
        ("en", "AI Hub Device Compilation"),
        ("zh-CN", "AI Hub 设备编译"),
        ("zh-TW", "AI Hub 裝置編譯"),
    ],
)
def test_feature_prompt_uses_localized_display_name(locale: str, display_name: str) -> None:
    builder = RichSystemPromptBuilder(feature_skill_provider=lambda _: "# AI Hub Device")
    result = builder.build(
        SystemPromptRequest(tool_mode="aihub-device", extra={"locale": locale})
    )

    assert display_name in result.prompt
    assert result.skill_body == "# AI Hub Device"
