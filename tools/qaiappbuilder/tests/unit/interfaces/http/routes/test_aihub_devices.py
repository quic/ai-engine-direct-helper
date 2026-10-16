# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Safe error responses for AI Hub device listing."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from interfaces.http.routes import aihub

pytestmark = pytest.mark.unit


def test_list_devices_preserves_empty_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aihub, "_run_hub_sync", lambda *_: (0, "", ""))
    response = aihub._list_devices_response(SimpleNamespace())  # noqa: SLF001

    assert response.status_code == 200
    assert json.loads(response.body) == {"devices": []}


def test_list_devices_hides_cli_failure_details(monkeypatch: pytest.MonkeyPatch) -> None:
    private_detail = "NameResolutionError token=do-not-return config=/tmp/private.ini"
    monkeypatch.setattr(aihub, "_run_hub_sync", lambda *_: (2, "", private_detail))
    response = aihub._list_devices_response(SimpleNamespace())  # noqa: SLF001
    body = json.loads(response.body)

    assert response.status_code == 502
    assert body == {"detail": "Could not load devices from AI Hub. Please try again."}
    assert private_detail not in response.body.decode()
    assert "devices" not in body


def test_list_devices_maps_authentication_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    stderr = "Bad or expired API token: synthetic-private-value"
    monkeypatch.setattr(aihub, "_run_hub_sync", lambda *_: (1, "", stderr))
    response = aihub._list_devices_response(SimpleNamespace())  # noqa: SLF001

    assert response.status_code == 401
    assert json.loads(response.body) == {
        "detail": "AI Hub authentication failed. Check the API token for the selected site."
    }
    assert stderr not in response.body.decode()
