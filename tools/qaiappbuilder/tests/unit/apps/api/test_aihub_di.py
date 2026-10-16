# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

from __future__ import annotations

from apps.api import _aihub_di

import pytest

pytestmark = pytest.mark.unit


def test_config_is_app_owned_and_environment_token_wins(tmp_path, monkeypatch) -> None:
    manager = _aihub_di.AiHubConfigManager(data_root=tmp_path)
    manager.save({"api_token": "stored-secret"})

    assert manager._get_config_path() == tmp_path / "config" / "aihub_config.json"
    assert manager.get_api_token() == "stored-secret"
    assert manager._get_config_path().stat().st_mode & 0o777 == 0o600

    monkeypatch.setenv("AIHUB_API_TOKEN", "environment-secret")
    assert manager.get_api_token() == "environment-secret"

def test_sites_isolate_environment_config_and_jobs(tmp_path, monkeypatch) -> None:
    global_manager = _aihub_di.AiHubConfigManager(tmp_path, _aihub_di.AiHubSite.GLOBAL)
    china_manager = _aihub_di.AiHubConfigManager(tmp_path, _aihub_di.AiHubSite.CHINA)
    global_manager.save({"api_token": "global-stored"})
    china_manager.save({"api_token": "china-stored"})
    monkeypatch.setenv("AIHUB_API_TOKEN", "global-env")
    monkeypatch.setenv("AIHUB_CHINA_API_TOKEN", "china-env")

    assert global_manager.get_api_token() == "global-env"
    assert china_manager.get_api_token() == "china-env"
    assert global_manager._get_config_path().name == "aihub_config.json"
    assert china_manager._get_config_path().name == "aihub_config_china.json"

    container = type("Container", (), {"data_paths": type("Paths", (), {"root": tmp_path})()})()
    services = _aihub_di.build_aihub_services(container)
    assert services.for_site("global").job_registry is not services.for_site("china").job_registry
    assert services.for_site("china").metadata.api_url == "https://workbench.aihub.qualcomm.cn"
    assert services.for_site("china").metadata.public_url == "https://aihub.qualcomm.cn"


def test_registry_bounds_terminal_jobs_without_evicting_running_jobs() -> None:
    registry = _aihub_di.AiHubJobRegistry()
    registry.create("running")
    for index in range(_aihub_di._MAX_TERMINAL_JOBS + 5):
        job_id = f"done-{index}"
        registry.create(job_id)
        registry.update(job_id, {"status": "done"})

    assert registry.get("running") is not None
    terminal_count = sum(
        job["status"] in {"done", "error"} for job in registry._jobs.values()
    )
    assert terminal_count <= _aihub_di._MAX_TERMINAL_JOBS
