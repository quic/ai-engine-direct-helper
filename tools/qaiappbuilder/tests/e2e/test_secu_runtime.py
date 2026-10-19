# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - security runtime config, master switch, policy, templates, channels (SECU-04, part 1).

Endpoints under ``/api/security`` (``interfaces/http/routes/security/``): ``GET|PUT /runtime-config``,
``PUT /mode``, ``GET /health``, ``GET|PUT /policy``, ``GET /policy/version``, ``GET|POST /templates``,
``GET|PUT /channels[/{name}]``. Skills (``skill_policy`` / ``skills/*`` / ``skill-discovery``) are in
``test_secu_skills.py``.

NOT covered (deliberately):

* **Switching the FileGuard off.** ``PUT /runtime-config {"file_guard_enabled": false}``,
  ``PUT /mode {"mode": "disabled"}`` and ``PUT /policy {"enabled": false}`` would turn the guard off in
  the test backend. The suite does not weaken the backend's security settings (the same rule as the
  native-guard decision in ``test_service.py``). Consequently UNTESTED: the ``disabled`` mode,
  ``health.status == "test_mode"``, and the native hook being stopped in place. The one hot toggle that IS
  exercised is ``file_guard_enabled`` False->True on an already-on guard (see the runtime-config tests).
* ``dependency_approval_*`` / ``command_policy_enabled`` take effect on a LIVE broker; this suite checks
  that the value is stored, reported and not a reboot, not that a pip install is then intercepted.
* The **restart** tests start their own backends (``_owned_backend.py``); a reboot-class change makes the
  process exit with code 75 and nothing here respawns it (that is the supervisor's job).

Isolation (verified): runtime config / mode / policy live in the throw-away
``<data_dir>/config/forge_config.json`` and ``<data_dir>/db/qai.db``. A test that changes the policy,
the mode or a channel puts it back; applying a template and ``PUT /policy`` bump a version counter that is
never reset, so no test asserts an absolute version. Nothing touches the keyring.

Observed behaviour worth pinning (it differs from what one might assume):

* ``GET /health.mode`` and ``GET /policy.mode`` do NOT follow ``PUT /mode``: ``permissive`` shows
  ``enforce`` in both (only ``effective_run_mode`` and the actual decisions change).
* Every ``PUT /policy`` bumps the version, even with identical rules; ``needs_reboot`` is true only
  when the rule set changed, and applying a template never asks for a reboot.
* ``GET /channels`` is empty until a channel is saved, only the five names ``background cli feishu web
  wechat`` are accepted, and there is no way to delete one.
* ``PUT /runtime-config`` accepts unknown fields silently, and a hand-edited ``forge_config.json`` can put a
  value below the PUT minimum (``read_max_lines: 0``) that GET then reports.
* The persisted region is read back by value coercion: a string ``"false"`` becomes ``True``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest

from tests.e2e.conftest import Backend

pytestmark = pytest.mark.e2e

SEC = "/api/security"
CHANNELS = ("background", "cli", "feishu", "web", "wechat")
DEFAULT_NO_UI = ["wechat", "feishu"]


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


@pytest.fixture
def api(http: httpx.Client) -> httpx.Client:
    http.get("/api/system/uptime")
    http.headers["X-QAI-CSRF"] = http.cookies.get("qai_csrf")
    return http


def _uid() -> str:
    return "e2e-" + uuid.uuid4().hex[:8]


def _fields(r: httpx.Response) -> dict:
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "request.validation"
    return r.json()["details"]["field_errors"]


def _rc(api: httpx.Client) -> dict:
    r = api.get(f"{SEC}/runtime-config")
    assert r.status_code == 200, r.text
    return r.json()


def _health(api: httpx.Client) -> dict:
    r = api.get(f"{SEC}/health")
    assert r.status_code == 200, r.text
    return r.json()


def _policy(api: httpx.Client) -> dict:
    r = api.get(f"{SEC}/policy")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def restore_policy(api: httpx.Client) -> Iterator[None]:
    """Put rules / mode / dynamic authorization / no-UI channels back to what they were."""
    before = _policy(api)
    yield
    api.put(f"{SEC}/policy", json={
        "rules": before["rules"], "mode": before["mode"],
        "dynamic_authorization": before["dynamic_authorization"], "no_ui_channels": before["no_ui_channels"],
    })


@pytest.fixture
def restore_runtime_config(api: httpx.Client) -> Iterator[None]:
    """Put the editable runtime-config fields back so later tests see the defaults."""
    before = _rc(api)
    yield
    keep = {k: before[k] for k in (
        "ssl_verify", "project_skip_dirs", "global_proxy", "dependency_approval_enabled",
        "dependency_approval_deny_args", "dependency_approval_timeout_s", "command_policy_enabled",
        "file_guard_enabled",
    )}
    api.put(f"{SEC}/runtime-config", json=keep)


@pytest.fixture
def restore_mode(api: httpx.Client) -> Iterator[None]:
    yield
    api.put(f"{SEC}/mode", json={"mode": "enforcing"})


# --------------------------------------------------------------------------
# runtime-config: read
# --------------------------------------------------------------------------

ALL_FIELDS = {
    "file_broker_enabled", "file_broker_max_entries", "ssl_verify", "project_skip_dirs", "global_proxy",
    "file_guard_enabled", "allow_exec_tool", "native_file_guard_enabled", "dependency_approval_enabled",
    "dependency_approval_deny_args", "dependency_approval_timeout_s", "command_policy_enabled",
    "read_max_lines", "read_max_bytes", "read_max_line_length", "glob_max_results", "grep_max_matches",
    "grep_max_line_length", "grep_max_output_bytes",
}


def test_runtime_config_get_reports_every_switch_with_the_documented_defaults(api: httpx.Client) -> None:
    rc = _rc(api)
    assert set(rc) == ALL_FIELDS
    assert (rc["file_broker_enabled"], rc["file_broker_max_entries"]) == (True, 10000)
    assert (rc["file_guard_enabled"], rc["native_file_guard_enabled"], rc["allow_exec_tool"]) == (True, True, True)
    assert (rc["dependency_approval_enabled"], rc["dependency_approval_timeout_s"]) == (False, 120)
    assert rc["dependency_approval_deny_args"] == ["-e", "git+", "--extra-index-url", "--pre"]
    assert rc["command_policy_enabled"] is True and rc["global_proxy"] is None and rc["project_skip_dirs"] == []
    assert (rc["read_max_lines"], rc["read_max_bytes"], rc["read_max_line_length"]) == (2000, 51200, 2000)
    assert (rc["glob_max_results"], rc["grep_max_matches"]) == (60, 100)
    assert (rc["grep_max_line_length"], rc["grep_max_output_bytes"]) == (2000, 51200)


def test_runtime_config_get_is_stable(api: httpx.Client) -> None:
    assert _rc(api) == _rc(api)


# --------------------------------------------------------------------------
# runtime-config: write
# --------------------------------------------------------------------------


def test_put_returns_the_merged_view_and_what_was_persisted(api: httpx.Client, restore_runtime_config: None) -> None:
    r = api.put(f"{SEC}/runtime-config", json={"project_skip_dirs": ["a", "b"], "ssl_verify": False})
    assert r.status_code == 200
    body = r.json()
    assert body["project_skip_dirs"] == ["a", "b"] and body["ssl_verify"] is False
    assert body["needs_reboot"] is False and body["persisted"] == ["project_skip_dirs", "ssl_verify"]
    assert set(body) == ALL_FIELDS | {"needs_reboot", "persisted"}
    after = _rc(api)
    assert after["project_skip_dirs"] == ["a", "b"] and after["ssl_verify"] is False


def test_put_changes_only_the_submitted_fields(api: httpx.Client, restore_runtime_config: None) -> None:
    before = _rc(api)
    api.put(f"{SEC}/runtime-config", json={"project_skip_dirs": ["only-this"]})
    after = _rc(api)
    assert after["project_skip_dirs"] == ["only-this"]
    assert {k: v for k, v in after.items() if k != "project_skip_dirs"} == {k: v for k, v in before.items() if k != "project_skip_dirs"}


@pytest.mark.parametrize("body", [{}, {"nope": 1, "also_unknown": [1]}])
def test_put_with_nothing_to_change_persists_nothing(api: httpx.Client, body: dict) -> None:
    """An empty body and unknown fields are both accepted and silently ignored (no 4xx)."""
    r = api.put(f"{SEC}/runtime-config", json=body)
    assert r.status_code == 200
    assert r.json()["persisted"] == [] and r.json()["needs_reboot"] is False


def test_global_proxy_blank_means_none_and_null_clears(api: httpx.Client, restore_runtime_config: None) -> None:
    assert api.put(f"{SEC}/runtime-config", json={"global_proxy": "http://127.0.0.1:9"}).json()["global_proxy"] == "http://127.0.0.1:9"
    assert _rc(api)["global_proxy"] == "http://127.0.0.1:9"
    r = api.put(f"{SEC}/runtime-config", json={"global_proxy": "   "})
    assert r.json()["global_proxy"] is None and _rc(api)["global_proxy"] is None, "a blank string clears it"
    api.put(f"{SEC}/runtime-config", json={"global_proxy": "http://127.0.0.1:9"})
    assert api.put(f"{SEC}/runtime-config", json={"global_proxy": None}).json()["persisted"] == ["global_proxy"]
    assert _rc(api)["global_proxy"] is None, "an explicit null clears it (absent would leave it alone)"


def test_absent_field_is_not_the_same_as_null(api: httpx.Client, restore_runtime_config: None) -> None:
    api.put(f"{SEC}/runtime-config", json={"global_proxy": "http://127.0.0.1:9"})
    api.put(f"{SEC}/runtime-config", json={"ssl_verify": True})
    assert _rc(api)["global_proxy"] == "http://127.0.0.1:9"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("read_max_lines", 0), ("read_max_bytes", 1023), ("read_max_line_length", 79), ("glob_max_results", 0),
        ("grep_max_matches", 0), ("grep_max_line_length", 79), ("grep_max_output_bytes", 1023),
    ],
)
def test_tool_output_caps_have_lower_bounds(api: httpx.Client, field: str, value: int) -> None:
    assert f"body.{field}" in _fields(api.put(f"{SEC}/runtime-config", json={field: value}))
    assert _rc(api)[field] != value


@pytest.mark.parametrize("body", [{"ssl_verify": "maybe"}, {"project_skip_dirs": "a,b"}, {"dependency_approval_timeout_s": "soon"}, {"file_broker_enabled": []}])
def test_put_rejects_wrong_types_and_stores_nothing(api: httpx.Client, body: dict) -> None:
    before = _rc(api)
    r = api.put(f"{SEC}/runtime-config", json=body)
    assert r.status_code == 400 and r.json()["code"] == "request.validation"
    assert _rc(api) == before


def test_a_failed_put_does_not_half_apply(api: httpx.Client, restore_runtime_config: None) -> None:
    before = _rc(api)
    r = api.put(f"{SEC}/runtime-config", json={"project_skip_dirs": ["leak"], "read_max_lines": 0})
    assert r.status_code == 400
    assert _rc(api) == before, "the valid field in a rejected request must not be stored"


@pytest.mark.parametrize(
    "body",
    [
        {"ssl_verify": False}, {"project_skip_dirs": ["x"]}, {"global_proxy": "http://127.0.0.1:9"},
        {"dependency_approval_enabled": True}, {"dependency_approval_deny_args": ["--x"]},
        {"dependency_approval_timeout_s": 77}, {"command_policy_enabled": False}, {"file_guard_enabled": True},
    ],
)
def test_hot_applied_switches_never_ask_for_a_reboot(api: httpx.Client, restore_runtime_config: None, body: dict) -> None:
    r = api.put(f"{SEC}/runtime-config", json=body)
    assert r.status_code == 200 and r.json()["needs_reboot"] is False
    key = next(iter(body))
    assert _rc(api)[key] == body[key]


def test_unchanged_value_of_a_reboot_class_field_does_not_ask_for_a_reboot(api: httpx.Client) -> None:
    """``needs_reboot`` is computed from the DIFF against the current value, not from the field name."""
    cur = _rc(api)
    same = {k: cur[k] for k in ("allow_exec_tool", "file_broker_enabled", "file_broker_max_entries", "read_max_lines")}
    r = api.put(f"{SEC}/runtime-config", json=same)
    assert r.status_code == 200 and r.json()["needs_reboot"] is False
    assert r.json()["persisted"] == sorted(same), "operator intent is still recorded"
    assert _rc(api) == cur


def test_the_put_response_for_a_rejected_request_leaves_the_process_alive(api: httpx.Client) -> None:
    api.put(f"{SEC}/runtime-config", json={"read_max_lines": 0})
    assert api.get("/api/system/health").status_code == 200


def test_runtime_config_persists_into_forge_config(api: httpx.Client, backend: Backend, restore_runtime_config: None) -> None:
    api.put(f"{SEC}/runtime-config", json={"project_skip_dirs": ["disk-check"], "dependency_approval_timeout_s": 91})
    doc = json.loads((backend.data_dir / "config" / "forge_config.json").read_text(encoding="utf-8"))
    region = doc["security_runtime_config"]
    assert region["project_skip_dirs"] == ["disk-check"] and region["dependency_approval_timeout_s"] == 91


def test_the_persisted_region_wins_over_the_boot_defaults_but_not_over_live_brokers(
    api: httpx.Client, backend: Backend, restore_runtime_config: None
) -> None:
    """Hand-edit the file: ordinary switches follow the file, broker switches follow the LIVE broker."""
    f = backend.data_dir / "config" / "forge_config.json"
    api.put(f"{SEC}/runtime-config", json={"ssl_verify": True})
    doc = json.loads(f.read_text(encoding="utf-8"))
    doc["security_runtime_config"].update({"grep_max_matches": 55, "dependency_approval_enabled": True, "command_policy_enabled": False})
    f.write_text(json.dumps(doc), encoding="utf-8")
    try:
        rc = _rc(api)
        assert rc["grep_max_matches"] == 55
        assert rc["dependency_approval_enabled"] is False and rc["command_policy_enabled"] is True
    finally:
        doc["security_runtime_config"].pop("grep_max_matches", None)
        f.write_text(json.dumps(doc), encoding="utf-8")


@pytest.mark.parametrize(
    ("region", "expect"),
    [
        ({"read_max_lines": "5"}, {"read_max_lines": 5}),
        ({"read_max_lines": 7.9}, {"read_max_lines": 7}),
        ({"read_max_lines": "abc"}, {"read_max_lines": 2000}),
        ({"ssl_verify": 0}, {"ssl_verify": False}),
        ({"project_skip_dirs": "a,b"}, {"project_skip_dirs": []}),
        ({"project_skip_dirs": [1, 2]}, {"project_skip_dirs": ["1", "2"]}),
        ({"global_proxy": 123}, {"global_proxy": "123"}),
        ({"unknown_key": 1}, {}),
    ],
)
def test_hand_edited_values_are_coerced_to_the_declared_shape(
    api: httpx.Client, backend: Backend, restore_runtime_config: None, region: dict, expect: dict
) -> None:
    f = backend.data_dir / "config" / "forge_config.json"
    api.put(f"{SEC}/runtime-config", json={"ssl_verify": True})
    doc = json.loads(f.read_text(encoding="utf-8"))
    saved = dict(doc["security_runtime_config"])
    doc["security_runtime_config"] = {**saved, **region}
    f.write_text(json.dumps(doc), encoding="utf-8")
    try:
        rc = _rc(api)
        for k, v in expect.items():
            assert rc[k] == v, (k, rc[k])
        assert set(rc) == ALL_FIELDS
    finally:
        doc["security_runtime_config"] = saved
        f.write_text(json.dumps(doc), encoding="utf-8")


def test_a_string_false_in_the_file_is_read_as_true(api: httpx.Client, backend: Backend, restore_runtime_config: None) -> None:
    """KNOWN GAP (current behaviour, pinned): bool coercion is ``bool(value)``, so a hand-edited
    ``"ssl_verify": "false"`` turns verification ON. If coercion learns to parse strings this becomes False."""
    f = backend.data_dir / "config" / "forge_config.json"
    api.put(f"{SEC}/runtime-config", json={"ssl_verify": True})
    doc = json.loads(f.read_text(encoding="utf-8"))
    saved = dict(doc["security_runtime_config"])
    doc["security_runtime_config"] = {**saved, "ssl_verify": "false"}
    f.write_text(json.dumps(doc), encoding="utf-8")
    try:
        assert _rc(api)["ssl_verify"] is True
    finally:
        doc["security_runtime_config"] = saved
        f.write_text(json.dumps(doc), encoding="utf-8")


def test_a_value_below_the_put_minimum_can_still_be_read_back(api: httpx.Client, backend: Backend, restore_runtime_config: None) -> None:
    """KNOWN GAP (current behaviour, pinned): PUT forbids ``read_max_lines < 1`` but a hand-edited file can
    store 0, and GET reports it without complaint."""
    f = backend.data_dir / "config" / "forge_config.json"
    api.put(f"{SEC}/runtime-config", json={"ssl_verify": True})
    doc = json.loads(f.read_text(encoding="utf-8"))
    saved = dict(doc["security_runtime_config"])
    doc["security_runtime_config"] = {**saved, "read_max_lines": 0}
    f.write_text(json.dumps(doc), encoding="utf-8")
    try:
        assert _rc(api)["read_max_lines"] == 0
    finally:
        doc["security_runtime_config"] = saved
        f.write_text(json.dumps(doc), encoding="utf-8")


def test_an_unreadable_forge_config_falls_back_to_the_defaults_and_the_next_put_repairs_it(api: httpx.Client, backend: Backend) -> None:
    f = backend.data_dir / "config" / "forge_config.json"
    original = f.read_text(encoding="utf-8") if f.exists() else None
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{not json", encoding="utf-8")
    try:
        assert _rc(api)["read_max_lines"] == 2000
        r = api.put(f"{SEC}/runtime-config", json={"ssl_verify": True})
        assert r.status_code == 200 and r.json()["persisted"] == ["ssl_verify"]
        assert json.loads(f.read_text(encoding="utf-8"))["security_runtime_config"] == {"ssl_verify": True}
    finally:
        if original is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(original, encoding="utf-8")


# --------------------------------------------------------------------------
# PUT /mode  (master switch) and GET /health
# --------------------------------------------------------------------------


def test_health_reports_a_healthy_enforcing_guard(api: httpx.Client) -> None:
    h = _health(api)
    assert (h["status"], h["enabled"], h["mode"], h["test_mode"]) == ("ok", True, "enforce", False)
    assert h["native_enabled"] is True and h["native_active"] is True
    assert isinstance(h["native_diagnostics"], dict) and all(isinstance(v, int) for v in h["native_diagnostics"].values())


def test_health_is_read_only_and_stable(api: httpx.Client) -> None:
    a = _health(api)
    b = _health(api)
    assert {k: v for k, v in a.items() if k != "native_diagnostics"} == {k: v for k, v in b.items() if k != "native_diagnostics"}


def test_permissive_mode_relaxes_decisions_to_audit_only(api: httpx.Client, restore_mode: None) -> None:
    probe = rf"C:\e2e-nogrant-{uuid.uuid4().hex[:6]}\x\y.txt"

    def decision() -> str:
        return api.post(f"{SEC}/permission/check", json={
            "subject": {"kind": "user", "identifier": _uid()}, "resource": {"kind": "path", "identifier": probe},
            "requested_mask": {"read": True}}).json()["decision"]

    assert decision() == "deny"
    r = api.put(f"{SEC}/mode", json={"mode": "permissive"})
    assert r.status_code == 200 and r.json() == {"mode": "permissive", "enabled": True, "effective_run_mode": "audit_only"}
    assert decision() == "allow", "permissive logs but allows"
    r = api.put(f"{SEC}/mode", json={"mode": "enforcing"})
    assert r.json() == {"mode": "enforcing", "enabled": True, "effective_run_mode": "enforce"}
    assert decision() == "deny"


def test_mode_is_not_reflected_in_health_or_policy(api: httpx.Client, restore_mode: None) -> None:
    """KNOWN GAP (current behaviour, pinned): ``permissive`` shows ``enforce`` in both ``/health.mode`` and
    ``/policy.mode`` - the run-mode there is the *sub*-switch, orthogonal to the master switch."""
    api.put(f"{SEC}/mode", json={"mode": "permissive"})
    assert _health(api)["mode"] == "enforce" and _policy(api)["mode"] == "enforce"
    assert _health(api)["status"] == "ok"


def test_mode_is_persisted_and_leaves_the_guard_switches_on(api: httpx.Client, backend: Backend, restore_mode: None) -> None:
    api.put(f"{SEC}/mode", json={"mode": "permissive"})
    doc = json.loads((backend.data_dir / "config" / "forge_config.json").read_text(encoding="utf-8"))
    assert doc["security_runtime_state"]["mode"] == "permissive"
    assert doc["security_runtime_config"]["file_guard_enabled"] is True
    assert doc["security_runtime_config"]["native_file_guard_enabled"] is True
    rc = _rc(api)
    assert rc["file_guard_enabled"] is True and rc["native_file_guard_enabled"] is True


@pytest.mark.parametrize("body", [{"mode": "bogus"}, {"mode": "ENFORCING"}, {"mode": ""}, {}, {"mode": None}])
def test_mode_rejects_anything_else_and_changes_nothing(api: httpx.Client, body: dict) -> None:
    before = _health(api)["status"]
    assert "body.mode" in _fields(api.put(f"{SEC}/mode", json=body))
    assert _health(api)["status"] == before


# --------------------------------------------------------------------------
# policy (toggles, rules, version)
# --------------------------------------------------------------------------

RULE = {"rule_id": "e2e-r1", "scope": "user", "pattern": r"C:\e2e-pol\*", "action": "deny", "description": "d"}


def test_policy_defaults(api: httpx.Client) -> None:
    p = _policy(api)
    assert (p["enabled"], p["mode"], p["dynamic_authorization"], p["needs_reboot"]) == (True, "enforce", True, False)
    assert p["no_ui_channels"] == DEFAULT_NO_UI and p["rules"] == []
    assert isinstance(p["version"], int) and p["updated_at"]
    assert api.get(f"{SEC}/policy/version").json() == {"version": p["version"]}


def test_policy_toggles_are_stored_and_reflected(api: httpx.Client, restore_policy: None) -> None:
    r = api.put(f"{SEC}/policy", json={"rules": [], "mode": "audit_only", "dynamic_authorization": False, "no_ui_channels": ["cli"]})
    assert r.status_code == 200
    p = r.json()
    assert (p["mode"], p["dynamic_authorization"], p["no_ui_channels"]) == ("audit_only", False, ["cli"])
    assert _policy(api)["mode"] == "audit_only"
    assert _health(api)["mode"] == "audit_only", "the policy run-mode IS what /health reports"


def test_policy_toggles_not_submitted_keep_their_value(api: httpx.Client, restore_policy: None) -> None:
    api.put(f"{SEC}/policy", json={"rules": [], "no_ui_channels": ["cli"]})
    api.put(f"{SEC}/policy", json={"rules": [], "mode": "audit_only"})
    p = _policy(api)
    assert p["no_ui_channels"] == ["cli"] and p["mode"] == "audit_only" and p["dynamic_authorization"] is True


def test_every_policy_put_bumps_the_version_but_only_a_rule_change_needs_a_reboot(api: httpx.Client, restore_policy: None) -> None:
    v0 = _policy(api)["version"]
    first = api.put(f"{SEC}/policy", json={"rules": [RULE]}).json()
    assert first["version"] == v0 + 1 and first["needs_reboot"] is True
    assert [r["rule_id"] for r in first["rules"]] == ["e2e-r1"]
    again = api.put(f"{SEC}/policy", json={"rules": [RULE]}).json()
    assert again["version"] == v0 + 2, "identical rules still bump the version"
    gone = api.put(f"{SEC}/policy", json={"rules": []}).json()
    assert gone["version"] == v0 + 3 and gone["rules"] == [] and gone["needs_reboot"] is True
    toggle = api.put(f"{SEC}/policy", json={"rules": [], "mode": "audit_only"}).json()
    assert toggle["needs_reboot"] is False
    assert api.get(f"{SEC}/policy/version").json() == {"version": toggle["version"]}


def test_policy_rule_wire_shape(api: httpx.Client, restore_policy: None) -> None:
    r = api.put(f"{SEC}/policy", json={"rules": [RULE]}).json()["rules"][0]
    assert r == {"rule_id": "e2e-r1", "scope": "user", "pattern": r"C:\e2e-pol\*", "case_sensitive": False,
                 "action": "deny", "description": "d", "op": "any"}


def test_a_deny_rule_in_the_policy_overrides_the_default(api: httpx.Client, restore_policy: None) -> None:
    zone = rf"C:\e2e-pol-{uuid.uuid4().hex[:6]}"
    body = {"subject": {"kind": "user", "identifier": _uid()}, "resource": {"kind": "path", "identifier": zone + r"\f.txt"}, "requested_mask": {"read": True}}
    api.put(f"{SEC}/policy", json={"rules": [{**RULE, "rule_id": "e2e-allow", "pattern": zone + "\\*", "action": "allow", "op": "read"}]})
    assert api.post(f"{SEC}/permission/check", json=body).json()["decision"] == "allow"
    api.put(f"{SEC}/policy", json={"rules": [{**RULE, "rule_id": "e2e-deny", "pattern": zone + "\\*", "action": "deny", "op": "read"}]})
    d = api.post(f"{SEC}/permission/check", json=body).json()
    assert d["decision"] == "deny" and d["matched_rule_id"] == "e2e-deny"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"mode": "bogus"}, "body.mode"),
        ({"rules": [{**RULE, "scope": "zzz"}]}, "body.rules.0.scope"),
        ({"rules": [{**RULE, "action": "ask"}]}, "body.rules.0.action"),
        ({"rules": [{**RULE, "pattern": ""}]}, "body.rules.0.pattern"),
        ({"rules": [{**RULE, "rule_id": "x" * 129}]}, "body.rules.0.rule_id"),
        ({"rules": [{**RULE, "op": "chmod"}]}, "body.rules.0.op"),
    ],
)
def test_policy_put_validation_leaves_the_policy_alone(api: httpx.Client, body: dict, field: str) -> None:
    before = _policy(api)
    assert field in _fields(api.put(f"{SEC}/policy", json={"rules": [], **body}))
    assert _policy(api) == before


def test_duplicate_rule_ids_are_422_and_nothing_is_stored(api: httpx.Client) -> None:
    before = _policy(api)
    r = api.put(f"{SEC}/policy", json={"rules": [RULE, RULE]})
    assert r.status_code == 422 and r.json()["code"] == "security.policy.invalid" and "e2e-r1" in r.json()["message"]
    assert _policy(api) == before


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------


def test_template_catalogue(api: httpx.Client) -> None:
    j = api.get(f"{SEC}/templates").json()
    assert j["total"] == 3 == len(j["templates"])
    by = {t["id"]: t for t in j["templates"]}
    assert set(by) == {"demo", "development", "strict"}
    assert {k: by[k]["rules_count"] for k in by} == {"demo": 1, "development": 2, "strict": 0}
    for t in by.values():
        assert t["name"] == t["id"] and t["description"]


def test_applying_the_rule_free_template_replaces_the_rules(api: httpx.Client, restore_policy: None) -> None:
    """Only ``strict`` (0 rules) is applied on the shared backend: applying ``demo`` or ``development`` makes the
    backend EXIT with code 75 about 8 s later (see ``test_secu_templates_restart.py``)."""
    v0 = _policy(api)["version"]
    r = api.post(f"{SEC}/templates", json={"template": "strict"})
    assert r.status_code == 200 and r.json()["template"] == "strict"
    pol = r.json()["policy"]
    assert pol["rules"] == [] and pol["version"] == v0 + 1 and pol["needs_reboot"] is False
    assert _policy(api)["rules"] == [] and _health(api)["status"] == "ok"
    assert api.get("/api/system/health").status_code == 200

def test_applying_a_template_keeps_the_toggles(api: httpx.Client, restore_policy: None) -> None:
    api.put(f"{SEC}/policy", json={"rules": [], "mode": "audit_only", "no_ui_channels": ["cli"]})
    pol = api.post(f"{SEC}/templates", json={"template": "strict"}).json()["policy"]
    assert pol["mode"] == "audit_only" and pol["no_ui_channels"] == ["cli"] and pol["enabled"] is True


def test_unknown_or_malformed_template_ids(api: httpx.Client) -> None:
    before = _policy(api)
    r = api.post(f"{SEC}/templates", json={"template": "nope"})
    assert r.status_code == 404 and r.json()["code"] == "security.template.not_found"
    assert "body.template" in _fields(api.post(f"{SEC}/templates", json={"template": ""}))
    assert "body.template" in _fields(api.post(f"{SEC}/templates", json={"template": "x" * 65}))
    assert "body.template" in _fields(api.post(f"{SEC}/templates", json={}))
    assert _policy(api) == before


# --------------------------------------------------------------------------
# channels
# --------------------------------------------------------------------------


@pytest.fixture
def restore_channels(api: httpx.Client) -> Iterator[None]:
    """There is no DELETE for a channel, so put each back to what it was (or leave it unsaved-looking)."""
    before = {c["name"]: c for c in api.get(f"{SEC}/channels").json()["channels"]}
    yield
    for name in CHANNELS:
        if name in before:
            c = before[name]
            api.put(f"{SEC}/channels/{name}", json={"requires_ui": c["requires_ui"], "description": c["description"], "quota": c["quota"]})


def _channel(api: httpx.Client, name: str) -> dict | None:
    return next((c for c in api.get(f"{SEC}/channels").json()["channels"] if c["name"] == name), None)


def test_channels_start_empty_and_save_makes_them_appear(api: httpx.Client, restore_channels: None, backend: Backend) -> None:
    import sqlite3

    r = api.put(f"{SEC}/channels/web", json={"requires_ui": False, "description": "e2e", "quota": {"window_seconds": 60, "max_asks": 3}})
    assert r.status_code == 200
    assert r.json() == {"name": "web", "requires_ui": False, "description": "e2e", "quota": {"window_seconds": 60, "max_asks": 3}}
    assert _channel(api, "web") == r.json()
    con = sqlite3.connect(str(backend.data_dir / "db" / "qai.db"))
    try:
        assert con.execute("SELECT * FROM security_channel_policy WHERE name = 'web'").fetchall() == [("web", 0, 60, 3, "e2e")]
    finally:
        con.close()


@pytest.mark.parametrize("name", CHANNELS)
def test_every_built_in_channel_name_is_accepted(api: httpx.Client, restore_channels: None, name: str) -> None:
    r = api.put(f"{SEC}/channels/{name}", json={"requires_ui": True, "description": "e2e"})
    assert r.status_code == 200 and r.json()["name"] == name and r.json()["quota"] is None


def test_saving_a_channel_without_a_quota_clears_the_quota(api: httpx.Client, restore_channels: None) -> None:
    api.put(f"{SEC}/channels/cli", json={"quota": {"window_seconds": 60, "max_asks": 3}})
    r = api.put(f"{SEC}/channels/cli", json={"requires_ui": True})
    assert r.json() == {"name": "cli", "requires_ui": True, "description": "", "quota": None}
    assert _channel(api, "cli") == r.json(), "PUT is a full replace"


def test_channel_defaults_when_the_body_is_empty(api: httpx.Client, restore_channels: None) -> None:
    assert api.put(f"{SEC}/channels/feishu", json={}).json() == {"name": "feishu", "requires_ui": True, "description": "", "quota": None}


@pytest.mark.parametrize(
    ("name", "message"),
    [("e2e-chan", "must be one of"), ("bad%20name", "must be one of"), ("UPPER", "lower-case"), ("n" * 200, "must be one of"), ("%E9%80%9A%E9%81%93", "must be one of")],
)
def test_only_the_five_known_channel_names_are_accepted(api: httpx.Client, restore_channels: None, name: str, message: str) -> None:
    before = api.get(f"{SEC}/channels").json()
    r = api.put(f"{SEC}/channels/{name}", json={})
    assert r.status_code == 400 and r.json()["code"] == "security.channel.invalid_name" and message in r.json()["message"]
    assert api.get(f"{SEC}/channels").json() == before


@pytest.mark.parametrize("quota", [{"window_seconds": 0, "max_asks": 1}, {"window_seconds": 5, "max_asks": -1}, {"window_seconds": "x", "max_asks": 1}, {"max_asks": 1}])
def test_channel_quota_must_be_positive_integers(api: httpx.Client, restore_channels: None, quota: dict) -> None:
    r = api.put(f"{SEC}/channels/web", json={"quota": quota})
    assert r.status_code == 400 and any(k.startswith("body.quota") for k in r.json()["details"]["field_errors"])


def test_a_channel_cannot_be_deleted_over_http(api: httpx.Client, restore_channels: None) -> None:
    api.put(f"{SEC}/channels/web", json={})
    assert api.delete(f"{SEC}/channels/web").status_code == 405
    assert _channel(api, "web") is not None


# --------------------------------------------------------------------------
# gaps found by mutation testing
# --------------------------------------------------------------------------


def test_native_diagnostics_are_reported_as_integer_counters(api: httpx.Client) -> None:
    diag = _health(api)["native_diagnostics"]
    assert {"allow_count", "deny_count", "callback_count", "is_inited"} <= set(diag), "the DLL snapshot is passed through"
    assert diag["is_inited"] == 1 and all(type(v) is int for v in diag.values())


def test_replacing_a_rule_with_a_different_one_of_the_same_count_needs_a_reboot(api: httpx.Client, restore_policy: None) -> None:
    api.put(f"{SEC}/policy", json={"rules": [RULE]})
    other = {**RULE, "rule_id": "e2e-r2", "pattern": r"C:\e2e-other\*"}
    r = api.put(f"{SEC}/policy", json={"rules": [other]}).json()
    assert r["needs_reboot"] is True and [x["rule_id"] for x in r["rules"]] == ["e2e-r2"]
    same = api.put(f"{SEC}/policy", json={"rules": [other]}).json()
    assert same["needs_reboot"] is False, "an identical rule set is not a change"
    edited = api.put(f"{SEC}/policy", json={"rules": [{**other, "action": "allow"}]}).json()
    assert edited["needs_reboot"] is True, "editing one field of a rule is a change"


def test_channels_are_listed_in_name_order(api: httpx.Client, restore_channels: None) -> None:
    for name in ("wechat", "cli", "web", "background"):
        api.put(f"{SEC}/channels/{name}", json={})
    names = [c["name"] for c in api.get(f"{SEC}/channels").json()["channels"]]
    assert names == sorted(names) and {"background", "cli", "web", "wechat"} <= set(names)
