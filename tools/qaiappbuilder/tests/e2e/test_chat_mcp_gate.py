# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - chat MCP: the execution gate and persistence across a restart (CHAT-08, part 2).

The gate is a persisted "global enabled" switch (``PATCH /api/chat/mcp/enabled``) whose initial value comes from the ``chat_mcp_enabled``
SETTING (``QAI_CHAT__CHAT_MCP_ENABLED``, default off). A server is started only while the switch is on. Each test uses its own backend (one restart
is part of what is tested), so this module is slow (about 15 s per backend).

Every server is a command that does not exist (``C:\\e2e-nope\\never-run.exe``): turning the gate on makes the product TRY to spawn it
and the attempt fails with ``spawn_failed`` - which is exactly how a start attempt is observed without running anything.

NOT covered (deliberately): a server that starts and answers (needs a real MCP server); ``headers`` / secret env (keyring - see
``test_chat_mcp.py``).

Observed behaviour worth pinning:

* The persisted global switch, not the setting, is what ``list.enabled`` reports; ``PATCH /enabled`` flips it and it SURVIVES a restart.
* With the switch on, the next listing/test attempts to start every per-server-enabled server; a failure is reported in the row
  (``connected: false``, ``error: spawn_failed: [WinError 2] ...``), never as an HTTP error.
* Servers and their per-server switches survive a restart; an entry written by hand is loaded.
"""

from __future__ import annotations

import json
import time

import pytest

from tests.e2e._owned_backend import owned_backend
from tests.e2e.test_chat_stream import _set_env

pytestmark = pytest.mark.e2e

M = "/api/chat/mcp"
NOPE = "C:\\e2e-nope\\never-run.exe"
GATE_OFF = "mcp disabled (chat_mcp_enabled=false)"
SPAWN_FAILED = "spawn_failed: [WinError 2] The system cannot find the file specified"


def _row(cl, name):
    return next(s for s in cl.get(f"{M}/servers").json()["servers"] if s["name"] == name)


def _wait_error(cl, name, want, seconds=15.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if _row(cl, name)["error"] == want:
            return
        time.sleep(0.3)
    raise AssertionError(f"{name}: error stayed {_row(cl, name)['error']!r}, wanted {want!r}")


def test_turning_the_global_switch_on_attempts_to_start_each_enabled_server_and_off_stops_trying() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        for n in ("e2e-on", "e2e-off"):
            assert cl.post(f"{M}/servers", json={"name": n, "command": NOPE, "args": ["--e2e"]}).status_code == 200
        cl.patch(f"{M}/servers/e2e-off", json={"enabled": False})
        assert _row(cl, "e2e-on")["error"] == GATE_OFF and "spawn_failed" not in ob.log_text()

        r = cl.patch(f"{M}/enabled", json={"enabled": True})
        assert r.status_code == 200 and r.json()["enabled"] is True and {s["name"] for s in r.json()["servers"]} == {"e2e-on", "e2e-off"}
        _wait_error(cl, "e2e-on", SPAWN_FAILED)
        assert _row(cl, "e2e-on")["connected"] is False and _row(cl, "e2e-on")["tool_count"] == 0
        log = ob.log_text()
        assert "chat.mcp.spawn_failed name=e2e-on" in log, "the product really tried to start the enabled server"
        assert "chat.mcp.spawn_failed name=e2e-off" not in log, "and did NOT try the one whose own switch is off"
        assert _row(cl, "e2e-off")["enabled"] is False

        r = cl.patch(f"{M}/enabled", json={"enabled": False})
        assert r.json()["enabled"] is False and _row(cl, "e2e-on")["enabled"] is True, "the per-server switch is independent of the global one"
        assert cl.post(f"{M}/servers/e2e-on/test").json()["error"] == GATE_OFF


def test_enabling_a_server_while_the_global_switch_is_on_tries_to_start_it() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        cl.patch(f"{M}/enabled", json={"enabled": True})
        cl.post(f"{M}/servers", json={"name": "e2e-late", "command": NOPE})
        cl.patch(f"{M}/servers/e2e-late", json={"enabled": False})
        n_before = ob.log_text().count("chat.mcp.spawn_failed name=e2e-late")
        row = cl.patch(f"{M}/servers/e2e-late", json={"enabled": True}).json()
        assert row["enabled"] is True
        _wait_error(cl, "e2e-late", SPAWN_FAILED)
        assert ob.log_text().count("chat.mcp.spawn_failed name=e2e-late") > n_before


def test_a_failed_start_is_reported_in_the_row_with_http_200() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        cl.post(f"{M}/servers", json={"name": "e2e-bad", "command": NOPE})
        cl.patch(f"{M}/enabled", json={"enabled": True})
        r = cl.post(f"{M}/servers/e2e-bad/test")
        assert r.status_code == 200 and (r.json()["connected"], r.json()["error"]) == (False, SPAWN_FAILED)
        assert cl.get("/api/system/health").status_code == 200


def test_servers_their_switches_and_the_global_switch_survive_a_restart() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        cl.post(f"{M}/servers", json={"name": "e2e-keep", "command": NOPE, "args": ["a", "b"], "env": {"K": "v"}, "timeout_s": 7.5})
        cl.post(f"{M}/servers", json={"name": "e2e-muted", "command": NOPE})
        cl.patch(f"{M}/servers/e2e-muted", json={"enabled": False})
        cl.patch(f"{M}/enabled", json={"enabled": True})
        cl.delete(f"{M}/servers/e2e-gone") if False else None
        ob.stop()
        cl = ob.start()
        body = cl.get(f"{M}/servers").json()
        assert body["enabled"] is True, "the persisted global switch is restored"
        by = {s["name"]: s for s in body["servers"]}
        assert set(by) == {"e2e-keep", "e2e-muted"}
        assert (by["e2e-keep"]["args"], by["e2e-keep"]["env"], by["e2e-keep"]["timeout_s"], by["e2e-keep"]["enabled"]) == (["a", "b"], {"K": "v"}, 7.5, True)
        assert by["e2e-muted"]["enabled"] is False
        _wait_error(cl, "e2e-keep", SPAWN_FAILED)
        assert "chat.mcp.spawn_failed name=e2e-keep" in ob.log_text(), "on boot the product connects what is enabled"


def test_a_removed_server_stays_removed_after_a_restart() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        for n in ("e2e-a", "e2e-b"):
            cl.post(f"{M}/servers", json={"name": n, "command": NOPE})
        cl.delete(f"{M}/servers/e2e-a")
        ob.stop()
        cl = ob.start()
        assert [s["name"] for s in cl.get(f"{M}/servers").json()["servers"]] == ["e2e-b"]


def test_a_hand_written_config_file_is_loaded() -> None:
    with owned_backend() as ob:
        cfg = ob.data_dir / "config"
        cfg.mkdir()
        (cfg / "mcp_servers.json").write_text(json.dumps({
            "global_enabled": False,
            "servers": [{"name": "e2e-hand", "transport": "stdio", "command": NOPE, "args": ["z"], "env": {}, "timeout_s": 9.0}],
            "custom_registry_sources": [],
        }), encoding="utf-8")
        cl = ob.start()
        row = _row(cl, "e2e-hand")
        assert (row["command"], row["args"], row["timeout_s"], row["enabled"], row["transport"]) == (NOPE, ["z"], 9.0, True, "stdio")


@pytest.mark.parametrize("content", ["", "{not json", "[]", '"x"', "null"], ids=["empty", "corrupt", "list", "string", "null"])
def test_an_unreadable_config_file_does_not_stop_the_backend(content: str) -> None:
    with owned_backend() as ob:
        cfg = ob.data_dir / "config"
        cfg.mkdir()
        (cfg / "mcp_servers.json").write_text(content, encoding="utf-8")
        cl = ob.start()
        assert cl.get(f"{M}/servers").json() == {"servers": [], "enabled": False}
        assert cl.post(f"{M}/servers", json={"name": "e2e-after", "command": NOPE}).status_code == 200


def test_the_setting_only_seeds_the_switch_and_the_api_can_override_it_in_both_directions() -> None:
    """KNOWN GAP / DESIGN QUESTION (current behaviour, pinned) - CHAT-ISS-09: the setting's own description calls
    ``chat_mcp_enabled`` the operator's opt-in ("operators must explicitly opt in") because an MCP stdio server is an arbitrary local
    subprocess. At runtime it is only the INITIAL value of the persisted switch: with the setting OFF (the default) any caller of
    ``PATCH /api/chat/mcp/enabled`` turns it on and the product then tries to start every configured server; with the setting ON the
    same call turns it off. The operator's choice does not hold."""
    old = _set_env({"QAI_CHAT__CHAT_MCP_ENABLED": "true"})
    try:
        with owned_backend() as ob:
            cl = ob.start()
            cl.post(f"{M}/servers", json={"name": "e2e-set", "command": NOPE})
            _wait_error(cl, "e2e-set", SPAWN_FAILED)
            assert cl.get(f"{M}/servers").json()["enabled"] is True, "the setting seeded the switch ON"
            assert cl.patch(f"{M}/enabled", json={"enabled": False}).json()["enabled"] is False
            assert cl.post(f"{M}/servers/e2e-set/test").json()["error"] == GATE_OFF
    finally:
        _set_env(old)


def test_with_the_setting_off_the_api_can_turn_the_gate_on() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        cl.post(f"{M}/servers", json={"name": "e2e-api", "command": NOPE})
        assert cl.get(f"{M}/servers").json()["enabled"] is False and cl.post(f"{M}/servers/e2e-api/test").json()["error"] == GATE_OFF
        assert cl.patch(f"{M}/enabled", json={"enabled": True}).json()["enabled"] is True
        assert cl.post(f"{M}/servers/e2e-api/test").json()["error"] == SPAWN_FAILED, "the product now tries to start the process"
        assert json.loads((ob.data_dir / "config" / "mcp_servers.json").read_text(encoding="utf-8"))["global_enabled"] is True
