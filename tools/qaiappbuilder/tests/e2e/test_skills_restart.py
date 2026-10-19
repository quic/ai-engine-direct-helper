# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - skill overrides survive a restart (SKILL-01, part 2).

Own module with its own backends: ``test_skills.py`` keeps a module-scoped backend (and a client) alive, and stopping it to test a
restart would close that client for every later test. See that module for scope, isolation and what is not covered.
"""

from __future__ import annotations

import pytest

from tests.e2e._owned_backend import owned_backend

pytestmark = pytest.mark.e2e


def _mode(cl, sid):
    return next(s for s in cl.get("/api/skills").json()["skills"] if s["skill_id"] == sid)["mode"]


def test_overrides_the_global_mode_and_the_reload_stamp_survive_a_restart() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        cl.post("/api/skills/toggle", json={"skill_name": "weather", "enabled": False})
        cl.post("/api/skills/ppt-gen/set_mode", json={"mode": "cloud"})
        cl.post("/api/skills/set_mode", json={"mode": "manual"})
        cl.post("/api/skills/reload")
        doc = cl.get("/api/skills/policy").json()
        assert doc["mode"] == "manual" and doc["last_reload"] and set(doc["overrides"]) == {"weather", "ppt-gen"}
        ob.stop()
        cl2 = ob.start()
        assert cl2.get("/api/skills/policy").json() == doc
        assert _mode(cl2, "weather") == "off" and _mode(cl2, "ppt-gen") == "cloud" and _mode(cl2, "read-arxiv-paper") == "cloud"


def test_a_fresh_data_dir_has_no_overrides() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        assert cl.get("/api/skills/policy").json() == {"mode": "auto", "overrides": {}, "last_reload": None}
        assert _mode(cl, "weather") == "cloud" and _mode(cl, "ppt-gen") == "off"