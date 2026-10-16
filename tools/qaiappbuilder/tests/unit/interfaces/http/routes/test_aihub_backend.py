# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api._aihub_di import AiHubSite, build_aihub_services
from interfaces.http.routes import aihub

pytestmark = pytest.mark.unit


def _container(data_root: Path, *, jobs: dict[str, dict] | None = None) -> SimpleNamespace:
    container = SimpleNamespace(data_paths=SimpleNamespace(root=data_root))
    container.aihub = build_aihub_services(container)
    if jobs is not None:
        container.aihub.for_site(AiHubSite.GLOBAL).job_registry._jobs = jobs
    return container


def _client(container: SimpleNamespace) -> TestClient:
    app = FastAPI()
    app.include_router(aihub.build_router(container=container))
    return TestClient(app)


def test_config_response_returns_presence_without_token_derived_data(tmp_path) -> None:
    container = _container(tmp_path)
    with _client(container) as client:
        assert client.post("/api/aihub/configure", json={"api_token": "secret-token"}).json() == {"ok": True}
        response = client.get("/api/aihub/config")

    assert response.json() == {"has_token": True}
    assert "secret" not in response.text
    saved = json.loads((tmp_path / "config" / "aihub_config.json").read_text())
    assert saved["api_token"] == "secret-token"

def test_config_rejects_blank_token(tmp_path) -> None:
    with _client(_container(tmp_path)) as client:
        response = client.post("/api/aihub/configure", json={"api_token": "   "})

    assert response.status_code == 422
    assert not (tmp_path / "config" / "aihub_config.json").exists()

def test_job_cannot_be_read_or_streamed_from_other_site(tmp_path) -> None:
    container = _container(tmp_path)
    container.aihub.for_site(AiHubSite.GLOBAL).job_registry.create("global-job")
    with _client(container) as client:
        assert client.get("/api/aihub/jobs/global-job", params={"site": "china"}).status_code == 404
        assert client.get("/api/aihub/jobs/global-job/logs", params={"site": "china"}).status_code == 404




def test_upload_rejects_path_outside_data_root_without_invoking_cli(tmp_path, monkeypatch) -> None:
    outside = tmp_path.parent / "private.onnx"
    outside.write_bytes(b"model")
    invoked = False

    def fake_run(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        return 0, "", ""

    monkeypatch.setattr(aihub, "_run_hub_sync", fake_run)
    with _client(_container(tmp_path)) as client:
        response = client.post("/api/aihub/upload", json={"local_path": str(outside)})

    assert response.status_code == 400
    assert not invoked


def test_upload_rejects_external_data_symlink_escape_before_external_load_or_cli(
    tmp_path, monkeypatch
) -> None:
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper

    data_root = tmp_path / "data"
    data_root.mkdir()
    model_path = data_root / "model.onnx"
    tensor = TensorProto(name="weights", data_type=TensorProto.FLOAT, dims=[1])
    tensor.data_location = TensorProto.EXTERNAL
    tensor.external_data.add(key="location", value="weights.bin")
    graph = helper.make_graph([], "g", [], [], initializer=[tensor])
    onnx.save_model(helper.make_model(graph), model_path)
    outside_data = tmp_path / "outside.bin"
    outside_data.write_bytes(b"weights")
    (data_root / "weights.bin").symlink_to(outside_data)

    real_load = onnx.load
    calls = {"metadata": 0, "external": 0, "runner": 0}

    def tracking_load(*args, **kwargs):
        if kwargs.get("load_external_data") is False:
            calls["metadata"] += 1
        else:
            calls["external"] += 1
        return real_load(*args, **kwargs)

    def fake_run(*_args, **_kwargs):
        calls["runner"] += 1
        return 0, "", ""

    monkeypatch.setattr(onnx, "load", tracking_load)
    monkeypatch.setattr(aihub, "_run_hub_sync", fake_run)
    with _client(_container(data_root)) as client:
        response = client.post("/api/aihub/upload", json={"local_path": str(model_path)})

    assert response.status_code == 400
    assert calls == {"metadata": 1, "external": 0, "runner": 0}


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ("/api/aihub/jobs/profile", aihub._run_profile_job_sdk),
        ("/api/aihub/jobs/compile-and-profile", aihub._run_compile_and_profile_job_sdk),
    ],
)
def test_sdk_worker_thread_receives_container_first(
    tmp_path, monkeypatch, path, target
) -> None:
    container = _container(tmp_path)
    captured: dict = {}

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            captured.update(target=target, args=args, daemon=daemon)

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(aihub.threading, "Thread", FakeThread)
    with _client(container) as client:
        response = client.post(path, json={"model_name": "model-id", "device": "device-name"})

    job_id = response.json()["job_id"]
    assert captured == {
        "target": target,
        "args": (container.aihub.for_site(), job_id, "model-id", "device-name"),
        "daemon": True,
        "started": True,
    }


def test_hub_cli_receives_token_only_through_temporary_private_ini(tmp_path, monkeypatch) -> None:
    context = _container(tmp_path).aihub.for_site(AiHubSite.CHINA)
    context.config_manager.save({"api_token": "token-value"})
    captured: dict = {}

    def fake_run(command, **kwargs):
        ini = Path(kwargs["env"]["QAIHUB_CLIENT_INI"])
        captured.update(
            command=command,
            env=kwargs["env"],
            ini=ini,
            content=ini.read_text(),
            mode=ini.stat().st_mode & 0o777,
        )
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(aihub.subprocess, "run", fake_run)
    assert aihub._run_hub_sync(context, ["list-devices"])[0] == 0
    assert "token-value" not in captured["command"]
    assert "token-value" not in captured["env"].values()
    assert "token-value" in captured["content"]
    assert captured["mode"] == 0o600
    assert not captured["ini"].exists()


def test_hub_cli_removes_temporary_ini_when_subprocess_fails(tmp_path, monkeypatch) -> None:
    context = _container(tmp_path).aihub.for_site()
    context.config_manager.save({"api_token": "token-value"})
    captured: dict = {}

    def fake_run(_command, **kwargs):
        captured["ini"] = Path(kwargs["env"]["QAIHUB_CLIENT_INI"])
        raise RuntimeError("failed")

    monkeypatch.setattr(aihub.subprocess, "run", fake_run)
    assert aihub._run_hub_sync(context, ["list-devices"])[0] == 1
    assert not captured["ini"].exists()


def test_hub_cli_temporary_ini_paths_are_unique(tmp_path) -> None:
    context = _container(tmp_path).aihub.for_site()
    context.config_manager.save({"api_token": "token-value"})
    with aihub._hub_env(context) as first_env, aihub._hub_env(context) as second_env:
        first = Path(first_env["QAIHUB_CLIENT_INI"])
        second = Path(second_env["QAIHUB_CLIENT_INI"])
        assert first != second
        assert first.exists() and second.exists()
    assert not first.exists() and not second.exists()



@pytest.mark.parametrize(
    ("stderr", "expected_status", "expected_detail"),
    [
        (
            "qai_hub.client.UserError: Failed to authenticate. Bad or expired API token: sensitive-value",
            401,
            "AI Hub authentication failed. Check the API token for the selected site.",
        ),
        (
            "request failed using /tmp/qai-hub-china-secret.ini with sensitive-value",
            502,
            "Could not load devices from AI Hub. Please try again.",
        ),
    ],
)
def test_devices_cli_failure_is_non_success_and_sanitized(
    tmp_path, monkeypatch, stderr, expected_status, expected_detail
) -> None:
    monkeypatch.setattr(aihub, "_run_hub_sync", lambda *_args: (1, "", stderr))

    with _client(_container(tmp_path)) as client:
        response = client.get("/api/aihub/devices", params={"site": "china"})

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert "sensitive-value" not in response.text
    assert "qai-hub-china-secret.ini" not in response.text


def test_devices_successful_empty_inventory_remains_success(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(aihub, "_run_hub_sync", lambda *_args: (0, "", ""))

    with _client(_container(tmp_path)) as client:
        response = client.get("/api/aihub/devices", params={"site": "china"})

    assert response.status_code == 200
    assert response.json() == {"devices": []}

def test_device_table_parser_extracts_frontend_schema() -> None:
    text = """+---+\n| Device | OS | Vendor | Type | Chipset | CLI Invocation |\n| Galaxy S24 | Android 14 | Samsung | Phone | Snapdragon 8 Gen 3 | --device x |\n+---+"""
    assert aihub._parse_devices_text(text) == [
        {"name": "Galaxy S24", "os": "Android 14", "chipset": "Snapdragon 8 Gen 3"}
    ]


@pytest.mark.asyncio
async def test_sse_frames_multiline_output_and_terminal_event(tmp_path) -> None:
    container = _container(
        tmp_path,
        jobs={"job": {"status": "done", "output": ["first\nsecond"]}},
    )
    chunks = [chunk async for chunk in aihub._sse_generator(container.aihub.for_site(), "job")]
    assert chunks == [
        "data: first\ndata: second\n\n",
        "event: done\ndata: done\n\n",
    ]


@pytest.mark.asyncio
async def test_sse_unknown_job_uses_error_event(tmp_path) -> None:
    container = _container(tmp_path)
    chunks = [chunk async for chunk in aihub._sse_generator(container.aihub.for_site(), "missing")]
    assert chunks == ["event: error_event\ndata: Job not found\n\n"]


@pytest.mark.asyncio
async def test_sse_waits_past_old_deadline_until_terminal_state(tmp_path, monkeypatch) -> None:
    context = _container(tmp_path).aihub.for_site()
    context.job_registry.create("slow")
    sleeps = 0

    async def controlled_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            context.job_registry.update("slow", {"status": "done"})

    monkeypatch.setattr(aihub.asyncio, "sleep", controlled_sleep)
    chunks = [chunk async for chunk in aihub._sse_generator(context, "slow")]
    assert sleeps == 2
    assert chunks == ["event: done\ndata: done\n\n"]
