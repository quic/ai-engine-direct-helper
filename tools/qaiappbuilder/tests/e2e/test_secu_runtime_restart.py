# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - security runtime config: persistence across a restart and the reboot signal (SECU-04, part 2).

Kept in its own module on purpose: these tests OWN their backends (``_owned_backend.py``) so they can read the
exit code and restart on the same data directory. A module that also requests the shared ``backend`` fixture
would hold port 8989 and this one could never start (the suite refuses to kill a process it did not start).

A reboot-class field (``allow_exec_tool``, ``file_broker_*``, the tool-output caps) makes the backend exit with
code 75 so the supervisor (``Start.bat`` / ``apps.cli.serve``) respawns it. Nothing here respawns it; the test
starts the second backend itself.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e._owned_backend import owned_backend

pytestmark = pytest.mark.e2e

SEC = "/api/security"


def test_runtime_config_survives_a_restart_and_a_reboot_class_change_exits_with_75() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        r = cl.put(f"{SEC}/runtime-config", json={
            "ssl_verify": False, "project_skip_dirs": ["keep-me"], "global_proxy": "http://127.0.0.1:9",
            "dependency_approval_deny_args": ["--zzz"], "dependency_approval_timeout_s": 33, "grep_max_matches": 42,
        })
        assert r.status_code == 200 and r.json()["needs_reboot"] is True, "grep_max_matches is reboot-class"
        assert ob.wait_exit(90) == 75, ob.log_text()[-1500:]
        events = [json.loads(line).get("event") for line in ob.log_text().splitlines() if line.startswith("{") and "reboot" in line]
        assert "reboot.scheduled" in events
        ob.stop()

        cl = ob.start()
        rc = cl.get(f"{SEC}/runtime-config").json()
        assert rc["ssl_verify"] is False and rc["project_skip_dirs"] == ["keep-me"]
        assert rc["global_proxy"] == "http://127.0.0.1:9"
        assert rc["dependency_approval_deny_args"] == ["--zzz"] and rc["dependency_approval_timeout_s"] == 33
        assert rc["grep_max_matches"] == 42
        again = cl.put(f"{SEC}/runtime-config", json={"grep_max_matches": 42})
        assert again.json()["needs_reboot"] is False, "after the reboot the value is the live one, so no second reboot"
        assert ob.proc is not None and ob.proc.poll() is None


def test_only_hot_switches_keep_the_process_running() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        for body in ({"ssl_verify": False}, {"project_skip_dirs": ["x"]}, {"global_proxy": "http://127.0.0.1:9"},
                     {"dependency_approval_enabled": True}, {"dependency_approval_deny_args": ["--x"]},
                     {"dependency_approval_timeout_s": 77}, {"command_policy_enabled": False}):
            r = cl.put(f"{SEC}/runtime-config", json=body)
            assert r.status_code == 200 and r.json()["needs_reboot"] is False
            assert ob.proc is not None and ob.proc.poll() is None, body
        assert ob.wait_exit(3) is None, "still alive after a quiet period"


def test_the_exit_code_is_75_for_every_reboot_class_field() -> None:
    """One backend per field would be slow; the first change schedules the exit, so this checks that a
    reboot-class change in ANY of the groups is flagged, and that the flagged one is what exits."""
    with owned_backend() as ob:
        cl = ob.start()
        cur = cl.get(f"{SEC}/runtime-config").json()
        flagged = {}
        for field in ("file_broker_max_entries", "read_max_bytes", "glob_max_results", "grep_max_line_length"):
            r = cl.put(f"{SEC}/runtime-config", json={field: cur[field] + 1}).json()
            flagged[field] = r["needs_reboot"]
        assert flagged == dict.fromkeys(flagged, True)
        assert ob.wait_exit(90) == 75


def test_every_kind_of_change_is_flagged_correctly_before_the_exit() -> None:
    """A REAL change to a reboot-class field must be flagged ``needs_reboot`` (the shared-backend tests can only
    assert the negative, because a flagged change makes the process exit)."""
    with owned_backend() as ob:
        cl = ob.start()
        cur = cl.get(f"{SEC}/runtime-config").json()
        assert cl.put(f"{SEC}/runtime-config", json={"ssl_verify": not cur["ssl_verify"]}).json()["needs_reboot"] is False
        r = cl.put(f"{SEC}/runtime-config", json={"allow_exec_tool": not cur["allow_exec_tool"]})
        assert r.status_code == 200 and r.json()["needs_reboot"] is True
        assert r.json()["allow_exec_tool"] is (not cur["allow_exec_tool"]), "the response already shows the new value"
        assert cl.get(f"{SEC}/runtime-config").json()["allow_exec_tool"] is (not cur["allow_exec_tool"]), "and so does GET, before the restart"
        assert ob.wait_exit(90) == 75
