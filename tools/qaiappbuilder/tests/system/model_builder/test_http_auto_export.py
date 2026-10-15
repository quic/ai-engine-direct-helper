# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""System tests for ``POST /api/app-builder/import/auto-export[/status]``.

Mounts the REAL route module + REAL bridge + REAL model_builder use case on a
minimal FastAPI app (no DB / full container), so it is fast enough for the
default run, unlike ``tests/integration/http``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import APIRouter, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from apps.api._app_builder_model_builder_bridge import (  # noqa: E402
    AppBuilderModelBuilderBridge,
)
from interfaces.http.error_handlers import register_error_handlers  # noqa: E402
from interfaces.http.routes.app_builder import _import  # noqa: E402

pytestmark = pytest.mark.system

URL = "/api/app-builder/import/auto-export"
STATUS = URL + "/status"


@pytest.fixture
def client(stack) -> TestClient:
    container = SimpleNamespace(
        auto_export_bridge=AppBuilderModelBuilderBridge(
            export_pack_use_case=stack.export_uc
        ),
        app_builder=SimpleNamespace(),  # only touched on the 409 diag path
    )
    router = APIRouter(prefix="/api/app-builder")
    _import.register(router, container=container)
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_auto_export_success_and_status(client, make_workspace):
    wd = make_workspace("real_esrgan_x4plus")
    assert client.post(STATUS, json={"source_path": str(wd)}).json() == {
        "status": "idle"
    }

    r = client.post(URL, json={"source_path": str(wd)})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["success"] is True and body["note"] == "export complete"
    assert body["pack_id"] and body["source_workdir"].endswith("app_pack")

    assert client.post(STATUS, json={"source_path": str(wd)}).json() == {
        "status": "generated"
    }


def test_auto_export_multi_precision(client, make_workspace):
    wd = make_workspace("yolov8_det", precisions=("fp16", "w8a8"))
    r = client.post(URL, json={
        "source_path": str(wd),
        "precisions": ["fp16", "int8"],
        "default_precision": "int8",
    })
    assert r.status_code == 202 and r.json()["success"] is True


@pytest.mark.parametrize("payload", [
    {},                                   # missing source_path
    {"source_path": ""},                  # min_length
    {"source_path": "x" * 5000},          # max_length
    {"source_path": "p", "precisions": ["a"] * 17},
])
def test_auto_export_request_validation_400(client, payload):
    r = client.post(URL, json=payload)
    assert r.status_code == 400, r.text
    assert r.json()["type"]


def test_auto_export_outside_root_is_400(client, tmp_path):
    (tmp_path / "evil" / "output").mkdir(parents=True)
    r = client.post(URL, json={"source_path": str(tmp_path / "evil")})
    assert r.status_code == 400
    assert r.json()["code"] == "app_builder.auto_export.invalid_request"


def test_auto_export_missing_workspace_is_400(client, wos_root):
    r = client.post(URL, json={"source_path": str(wos_root / "ghost")})
    assert r.status_code == 400


def test_auto_export_invalid_default_precision_is_400(client, make_workspace):
    wd = make_workspace("m", precisions=("fp16",))
    r = client.post(URL, json={
        "source_path": str(wd), "precisions": ["fp16"],
        "default_precision": "w8a16",
    })
    assert r.status_code == 400


def test_auto_export_missing_bin_is_409(client, make_workspace):
    wd = make_workspace("m", bin_size=10)
    r = client.post(URL, json={"source_path": str(wd)})
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "app_builder.auto_export.missing_context_bin"


def test_bridge_not_wired_is_503(stack):
    router = APIRouter(prefix="/api/app-builder")
    _import.register(router, container=SimpleNamespace(app_builder=None))
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router)
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post(URL, json={"source_path": "x"}).status_code == 503
    assert c.post(STATUS, json={"source_path": "x"}).status_code == 503
