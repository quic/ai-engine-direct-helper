# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Safe error responses for AI Hub model listing."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from interfaces.http.routes import aihub

pytestmark = pytest.mark.unit


def test_list_models_hides_sdk_failure_details(monkeypatch: pytest.MonkeyPatch) -> None:
    private_detail = "token=synthetic-secret config=/home/private/client.ini"

    class FailingClient:
        def get_models(self) -> list[object]:
            raise RuntimeError(private_detail)

    monkeypatch.setattr(aihub, "_hub_sdk", lambda _context: FailingClient())
    response = aihub._list_models_response(SimpleNamespace())  # noqa: SLF001
    body = json.loads(response.body)

    assert response.status_code == 502
    assert body == {
        "detail": "AI Hub model request failed: upstream service unavailable."
    }
    assert private_detail not in response.body.decode()
    assert "models" not in body


def test_list_models_maps_authentication_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingClient:
        def get_models(self) -> list[object]:
            raise RuntimeError("HTTP 401 Unauthorized token=do-not-return")

    monkeypatch.setattr(aihub, "_hub_sdk", lambda _context: FailingClient())
    response = aihub._list_models_response(SimpleNamespace())  # noqa: SLF001

    assert response.status_code == 401
    assert json.loads(response.body) == {
        "detail": "AI Hub model request failed: authentication required."
    }
