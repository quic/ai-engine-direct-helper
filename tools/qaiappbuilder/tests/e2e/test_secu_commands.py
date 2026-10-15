# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 — dangerous-command policy and auto-approve (SECU-02).

Endpoints under ``/api/security``:

* ``dangerous-command-patterns`` (GET/PUT) — an IMMUTABLE built-in floor of 9
  destructive-command regexes plus operator-supplied ``extra`` patterns that can
  only ADD to it (``routes/security/_dangerous_commands.py``).
* ``auto_approve/config`` (GET/PUT) — legacy ``enabled`` + ``trusted_paths``.
* ``auto_approve`` (GET/PUT) — per-tool toggles plus a command whitelist and a
  command blacklist (default ON with 14 built-in prefixes).

Scope note: these tests cover the CONFIGURATION surface. They do not execute
shell commands to prove the guard blocks ``rm -rf`` at run time - that needs a
live agent turn and belongs to a later suite. Also the extra patterns are baked
into the guard at build time, so a PUT reports ``needs_reboot=true`` and is
deliberately not hot-applied; we assert that contract rather than a restart.

Isolation (verified): writes go to ``<temp data_dir>/config/forge_config.json``;
the user's real file was byte-identical before/after. None of these endpoints has
a DELETE, so each test restores the documented default in teardown.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest

pytestmark = pytest.mark.e2e

SEC = "/api/security"
DANGEROUS = f"{SEC}/dangerous-command-patterns"
AA_CONFIG = f"{SEC}/auto_approve/config"
AA_FULL = f"{SEC}/auto_approve"

#: Documented factory state of the full auto-approve document.
AA_FULL_DEFAULT_TOGGLES = {"read": False, "write": False, "exec": False, "glob": False, "grep": False}


@pytest.fixture
def api(http: httpx.Client) -> httpx.Client:
    http.get("/api/system/uptime")
    http.headers["X-QAI-CSRF"] = http.cookies.get("qai_csrf")
    return http


@pytest.fixture
def restore_dangerous(api: httpx.Client) -> Iterator[None]:
    yield
    api.put(DANGEROUS, json={"extra": []})


@pytest.fixture
def restore_aa_config(api: httpx.Client) -> Iterator[None]:
    yield
    api.put(AA_CONFIG, json={"enabled": False, "trusted_paths": []})


@pytest.fixture
def restore_aa_full(api: httpx.Client) -> Iterator[dict]:
    """Capture the current document and put it back (there is no DELETE)."""
    original = api.get(AA_FULL).json()
    yield original
    api.put(AA_FULL, json=original)


# ---------------------------------------------------------------------------
# dangerous-command-patterns: immutable floor + union-only extras
# ---------------------------------------------------------------------------


def test_secu02_builtin_floor_is_present_and_every_entry_is_a_valid_regex(api):
    body = api.get(DANGEROUS).json()
    assert len(body["builtin"]) == 9
    for src in body["builtin"]:
        re.compile(src)  # raises if the shipped floor were malformed


@pytest.mark.parametrize(
    ("command", "expected_hit"),
    [
        ("rm -rf /", True),
        ("rm -fr /tmp/x", True),
        ("mkfs.ext4 /dev/sda1", True),
        ("dd if=/dev/zero of=/dev/sda", True),
        ("format C:", True),
        ("rmdir /s C:\\x", True),
        ("Remove-Item C:\\x -Recurse -Force", True),
        (":(){ :|: & };:", True),
        ("echo hello", False),
        ("git status", False),
        ("ls -la", False),
    ],
)
def test_secu02_builtin_floor_recognises_destructive_commands(api, command, expected_hit):
    """The advertised floor really matches the commands it is meant to catch.

    Matching is done with the regex SOURCES the API publishes (case-insensitive,
    as the guard compiles them), so a silently weakened floor fails here.
    """
    floor = [re.compile(s, re.IGNORECASE) for s in api.get(DANGEROUS).json()["builtin"]]
    assert any(p.search(command) for p in floor) is expected_hit


def test_secu02_extra_defaults_to_empty(api, restore_dangerous):
    assert api.get(DANGEROUS).json()["extra"] == []


def test_secu02_extra_patterns_round_trip_and_flag_reboot(api, restore_dangerous):
    r = api.put(DANGEROUS, json={"extra": [r"\bshutdown\b", "e2e-forbidden"]})
    assert r.status_code == 200
    body = r.json()
    assert body["extra"] == [r"\bshutdown\b", "e2e-forbidden"]
    assert body["invalid"] == []
    # Baked into the guard at build time, so NOT hot-applied: the UI must prompt.
    assert body["needs_reboot"] is True
    assert api.get(DANGEROUS).json()["extra"] == [r"\bshutdown\b", "e2e-forbidden"]


def test_secu02_get_does_not_claim_a_reboot_is_needed(api, restore_dangerous):
    assert "needs_reboot" not in api.get(DANGEROUS).json()


def test_secu02_uncompilable_patterns_are_dropped_and_reported(api, restore_dangerous):
    r = api.put(DANGEROUS, json={"extra": ["(unclosed", "[bad", "good-one"]})
    body = r.json()
    assert r.status_code == 200
    assert body["extra"] == ["good-one"]
    assert body["invalid"] == ["(unclosed", "[bad"]
    # A bad operator regex must never be persisted.
    assert api.get(DANGEROUS).json()["extra"] == ["good-one"]


def test_secu02_duplicates_and_blank_entries_are_removed(api, restore_dangerous):
    r = api.put(DANGEROUS, json={"extra": ["dup", "dup", "   ", "", "other"]})
    assert r.json()["extra"] == ["dup", "other"]


def test_secu02_put_replaces_the_whole_extra_list(api, restore_dangerous):
    api.put(DANGEROUS, json={"extra": ["first"]})
    api.put(DANGEROUS, json={"extra": ["second"]})
    assert api.get(DANGEROUS).json()["extra"] == ["second"]


def test_secu02_the_builtin_floor_can_never_be_changed_by_a_put(api, restore_dangerous):
    """Red line: no API path removes or replaces the built-in protections."""
    before = api.get(DANGEROUS).json()["builtin"]
    # Try the obvious tricks: empty extras, injecting a 'builtin' field, overwriting it.
    for payload in (
        {"extra": []},
        {"extra": [], "builtin": []},
        {"builtin": ["only-this"], "extra": ["x"]},
    ):
        r = api.put(DANGEROUS, json=payload)
        assert r.status_code == 200
        assert r.json()["builtin"] == before
    assert api.get(DANGEROUS).json()["builtin"] == before


def test_secu02_extra_cannot_be_used_to_smuggle_a_removal_of_the_floor(api, restore_dangerous):
    before = api.get(DANGEROUS).json()["builtin"]
    # A pattern that merely *looks* like an exemption is just another extra entry.
    api.put(DANGEROUS, json={"extra": ["!rm -rf", "-rm -rf", r"(?!rm)"]})
    assert api.get(DANGEROUS).json()["builtin"] == before


@pytest.mark.parametrize("payload", [{"extra": "not-a-list"}, {"extra": [1, 2]}, {"extra": {"a": 1}}])
def test_secu02_dangerous_put_rejects_wrong_types(api, restore_dangerous, payload):
    before = api.get(DANGEROUS).json()["extra"]
    r = api.put(DANGEROUS, json=payload)
    assert r.status_code == 400
    assert api.get(DANGEROUS).json()["extra"] == before


# ---------------------------------------------------------------------------
# auto_approve/config (legacy): enabled + trusted_paths
# ---------------------------------------------------------------------------


def test_secu02_auto_approve_config_default_is_off(api, restore_aa_config):
    assert api.get(AA_CONFIG).json() == {"enabled": False, "trusted_paths": []}


def test_secu02_auto_approve_config_round_trip(api, restore_aa_config):
    r = api.put(AA_CONFIG, json={"enabled": True, "trusted_paths": ["C:/e2e-trusted"]})
    assert r.status_code == 200
    assert r.json() == {"enabled": True, "trusted_paths": ["C:/e2e-trusted"]}
    assert api.get(AA_CONFIG).json() == {"enabled": True, "trusted_paths": ["C:/e2e-trusted"]}


def test_secu02_auto_approve_config_requires_enabled(api, restore_aa_config):
    r = api.put(AA_CONFIG, json={"trusted_paths": ["C:/x"]})
    assert r.status_code == 400
    assert "enabled" in str(r.json()["details"]["field_errors"])
    assert api.get(AA_CONFIG).json() == {"enabled": False, "trusted_paths": []}


def test_secu02_auto_approve_config_rejects_wrong_types(api, restore_aa_config):
    assert api.put(AA_CONFIG, json={"enabled": "yes-please", "trusted_paths": []}).status_code == 400
    assert api.put(AA_CONFIG, json={"enabled": True, "trusted_paths": "C:/x"}).status_code == 400


# ---------------------------------------------------------------------------
# auto_approve (full): tool toggles + whitelist + blacklist
# ---------------------------------------------------------------------------


def test_secu02_auto_approve_default_requires_approval_for_everything(api, restore_aa_full):
    body = restore_aa_full
    assert body["auto_approve"] == AA_FULL_DEFAULT_TOGGLES
    assert body["command_whitelist"] == {"enabled": False, "prefixes": []}


def test_secu02_default_blacklist_is_enabled_and_shows_real_protection(api, restore_aa_full):
    """A fresh install must HONESTLY show the default protection (not an empty list)."""
    bl = restore_aa_full["command_blacklist"]
    assert bl["enabled"] is True
    assert len(bl["prefixes"]) == 14
    for must_have in ("rm -rf", "rmdir /s", "del /s", "mkfs.", "diskpart", "reg delete", "curl|sh", "wget|sh"):
        assert must_have in bl["prefixes"], f"default blacklist lost {must_have!r}"


def test_secu02_auto_approve_full_round_trip(api, restore_aa_full):
    payload = {
        "auto_approve": {"read": True, "write": False, "exec": True, "glob": False, "grep": True},
        "command_whitelist": {"enabled": True, "prefixes": ["git status", "git diff"]},
        "command_blacklist": {"enabled": True, "prefixes": ["rm -rf", "e2e-blocked"]},
    }
    r = api.put(AA_FULL, json=payload)
    assert r.status_code == 200
    assert r.json() == payload
    assert api.get(AA_FULL).json() == payload


def test_secu02_each_tool_toggle_is_independent(api, restore_aa_full):
    for tool in AA_FULL_DEFAULT_TOGGLES:
        toggles = {**AA_FULL_DEFAULT_TOGGLES, tool: True}
        got = api.put(AA_FULL, json={"auto_approve": toggles}).json()["auto_approve"]
        assert got == toggles, f"toggling {tool} affected another tool"


def test_secu02_blacklist_can_be_switched_off_explicitly(api, restore_aa_full):
    r = api.put(AA_FULL, json={"command_blacklist": {"enabled": False, "prefixes": ["x"]}})
    assert r.json()["command_blacklist"] == {"enabled": False, "prefixes": ["x"]}


def test_secu02_omitted_sections_fall_back_to_safe_defaults(api, restore_aa_full):
    """PUT is a full replace: an omitted section reverts to its default, and the
    blacklist stays ENABLED (only an explicit false turns it off)."""
    api.put(AA_FULL, json={"auto_approve": {"exec": True}, "command_whitelist": {"enabled": True, "prefixes": ["x"]}})
    body = api.put(AA_FULL, json={}).json()
    assert body["auto_approve"] == AA_FULL_DEFAULT_TOGGLES
    assert body["command_whitelist"] == {"enabled": False, "prefixes": []}
    assert body["command_blacklist"]["enabled"] is True


def test_secu02_never_saved_document_reports_the_blacklist_as_enabled(api, restore_aa_full):
    """Guards the READ-side default: a document with no stored blacklist is ON.

    An empty PUT stores the defaults for every section, which is the state a fresh
    install shows; the blacklist must come back ENABLED, both from the PUT response
    and from a subsequent GET.
    """
    body = api.put(AA_FULL, json={}).json()
    assert body["command_blacklist"]["enabled"] is True
    assert api.get(AA_FULL).json()["command_blacklist"]["enabled"] is True


def test_secu02_KNOWN_QUIRK_blacklist_prefixes_without_enabled_switches_it_off(api, restore_aa_full):
    """CHARACTERISATION of current behaviour - review before treating as correct.

    ``CommandListConfig.enabled`` defaults to False, so a PUT that supplies
    ``command_blacklist.prefixes`` but omits ``enabled`` is stored as DISABLED,
    even though an entirely omitted ``command_blacklist`` means ENABLED. A client
    that only wants to edit the prefix list can therefore silently turn the
    dangerous-command blacklist off.

    If this is fixed (e.g. the request model defaults ``enabled`` to True for the
    blacklist), update this test to assert ``enabled is True``.
    """
    r = api.put(AA_FULL, json={"command_blacklist": {"prefixes": ["e2e-only-this"]}})
    assert r.status_code == 200
    assert r.json()["command_blacklist"] == {"enabled": False, "prefixes": ["e2e-only-this"]}
    # The same omission on the whitelist is harmless (its default is also off).
    w = api.put(AA_FULL, json={"command_whitelist": {"prefixes": ["git"]}}).json()
    assert w["command_whitelist"]["enabled"] is False


def test_secu02_auto_approve_full_rejects_wrong_types(api, restore_aa_full):
    before = api.get(AA_FULL).json()
    for bad in (
        {"auto_approve": {"read": "yes-please"}},
        {"command_whitelist": {"enabled": True, "prefixes": "git"}},
        {"command_blacklist": "off"},
    ):
        assert api.put(AA_FULL, json=bad).status_code == 400
    assert api.get(AA_FULL).json() == before


def test_secu02_legacy_and_full_auto_approve_are_independent_buckets(api, restore_aa_config, restore_aa_full):
    api.put(AA_CONFIG, json={"enabled": True, "trusted_paths": ["C:/legacy"]})
    full_before = api.get(AA_FULL).json()
    api.put(AA_FULL, json={"auto_approve": {"read": True}})
    assert api.get(AA_CONFIG).json() == {"enabled": True, "trusted_paths": ["C:/legacy"]}
    api.put(AA_FULL, json=full_before)
