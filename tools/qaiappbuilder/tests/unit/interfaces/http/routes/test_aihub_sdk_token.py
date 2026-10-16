# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

from __future__ import annotations

from types import SimpleNamespace

import pytest

from interfaces.http.routes import aihub

pytestmark = pytest.mark.unit


def test_sdk_client_uses_explicit_site_config(monkeypatch) -> None:
    captured = {}

    class ClientConfig:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class Client:
        def __init__(self, *, config):
            self.config = config

    monkeypatch.setitem(__import__("sys").modules, "qai_hub.client", SimpleNamespace(Client=Client, ClientConfig=ClientConfig))
    context = SimpleNamespace(
        metadata=SimpleNamespace(api_url="https://workbench.aihub.qualcomm.cn", web_url="https://workbench.aihub.qualcomm.cn"),
        config_manager=SimpleNamespace(get_api_token=lambda: "stored-token"),
    )

    assert isinstance(aihub._hub_sdk(context), Client)
    assert captured == {
        "api_url": "https://workbench.aihub.qualcomm.cn",
        "web_url": "https://workbench.aihub.qualcomm.cn",
        "api_token": "stored-token",
        "verbose": False,
    }
