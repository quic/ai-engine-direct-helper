# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - applying a security policy template restarts the backend (SECU-04, part 3).

Own module, own backends (``_owned_backend.py``): the behaviour pinned here makes the process EXIT, so it
cannot share the module-scoped ``backend`` fixture with the other security tests.

KNOWN GAP (current behaviour, pinned): ``POST /api/security/templates`` for a template that carries rules
(``demo``, ``development``) makes the backend exit with code 75 (the supervisor's "respawn me" code) about 8 s
after the response - yet the response says ``needs_reboot: false`` and the Security page only shows a success
toast. ``PUT /api/security/policy`` with the same kind of rule change does the opposite: it reports
``needs_reboot: true`` and does NOT restart. Cause: ``ApplySecurityTemplateUseCase`` calls
``UpdatePolicyUseCase.execute`` with its default ``auto_reboot=True``, while the manual route passes
``auto_reboot=False``. A user who applies a template in a window that is not under the supervisor
(``Start.bat`` / ``apps.cli.serve``) loses the backend. If the template route is changed to
``auto_reboot=False`` (and to report ``needs_reboot``), the "exits" assertions below fail - update them then.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e._owned_backend import owned_backend
from tests.e2e.conftest import REPO_ROOT

pytestmark = pytest.mark.e2e

SEC = "/api/security"


def _reboot_reasons(log: str) -> list[str]:
    return [json.loads(line).get("reason") for line in log.splitlines() if line.startswith("{") and "reboot.scheduled" in line]


@pytest.mark.parametrize(("tid", "rules"), [("demo", 1), ("development", 2)])
def test_applying_a_template_with_rules_exits_the_backend_with_75_although_the_response_says_no_reboot(tid: str, rules: int) -> None:
    with owned_backend() as ob:
        cl = ob.start()
        r = cl.post(f"{SEC}/templates", json={"template": tid})
        assert r.status_code == 200
        pol = r.json()["policy"]
        assert len(pol["rules"]) == rules
        assert pol["needs_reboot"] is False, "the response does not warn about the restart"
        assert ob.wait_exit(60) == 75, "but the process exits by itself"
        assert _reboot_reasons(ob.log_text()) == [f"applied template {tid}"]


def test_applying_the_rule_free_template_does_not_restart() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        assert cl.post(f"{SEC}/templates", json={"template": "strict"}).status_code == 200
        assert ob.wait_exit(15) is None, "no rule changed, so no restart"
        assert _reboot_reasons(ob.log_text()) == []


def test_a_manual_rule_change_reports_a_reboot_but_does_not_restart() -> None:
    """The contrast that makes the template behaviour a bug: same effect on the rule set, opposite handling."""
    rule = {"rule_id": "e2e-r1", "scope": "user", "pattern": r"C:\e2e-pol\*", "action": "deny"}
    with owned_backend() as ob:
        cl = ob.start()
        r = cl.put(f"{SEC}/policy", json={"rules": [rule]})
        assert r.status_code == 200 and r.json()["needs_reboot"] is True
        assert ob.wait_exit(20) is None
        assert _reboot_reasons(ob.log_text()) == []


def test_the_template_rules_are_live_before_the_restart_and_survive_it() -> None:
    """The ``development`` template allows the project tree to be read; the rule is enforced right away and is
    still there after the restart (it is persisted, not only in memory)."""
    inside = rf"{REPO_ROOT}\README.md"
    body = {"subject": {"kind": "user", "identifier": "e2e-tpl"}, "resource": {"kind": "path", "identifier": inside}, "requested_mask": {"read": True}}
    with owned_backend() as ob:
        cl = ob.start()
        dev = cl.post(f"{SEC}/templates", json={"template": "development"}).json()["policy"]["rules"]
        assert [(r["rule_id"], r["action"], r["op"]) for r in dev] == [("tpl-dev-allow-project", "allow", "write"), ("tpl-dev-allow-temp", "allow", "write")]
        assert all("${" in r["pattern"] for r in dev), "patterns keep their ${VAR} placeholders"
        d = cl.post(f"{SEC}/permission/check", json=body).json()
        assert d["decision"] == "allow" and d["matched_rule_id"] == "tpl-dev-allow-project"
        assert ob.wait_exit(60) == 75
        ob.stop()

        cl = ob.start()
        assert [r["rule_id"] for r in cl.get(f"{SEC}/policy").json()["rules"]] == ["tpl-dev-allow-project", "tpl-dev-allow-temp"]
        d = cl.post(f"{SEC}/permission/check", json=body).json()
        assert d["decision"] == "allow" and d["matched_rule_id"] == "tpl-dev-allow-project"
        assert ob.wait_exit(15) is None, "the restarted backend does not restart again"
