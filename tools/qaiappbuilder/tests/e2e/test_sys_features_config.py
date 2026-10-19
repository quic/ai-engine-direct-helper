# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - ``/api/system/features`` and ``/api/system/config/summary`` against real settings (SYS-03).

``features`` is the list the UI uses to decide which areas to show; ``config/summary`` reports the settings the UI needs before login. The
point of this module is the claim in the inventory - "reflect what is actually enabled" - checked against the settings that DO exist.

Each case is its own backend (the settings are read at start-up), so the module takes about 15 s per case. Settings are passed as
``QAI_<SECTION>__<FIELD>`` environment variables of the throw-away backend.

NOT covered (deliberately):

* ``host`` other than the loopback one: the harness fixes the bind address, so ``QAI_SERVER__HOST`` is only read back, never listened on.
* The ``edition`` switch: it comes from ``build_info.json`` in the repo root, which these tests never edit (``external`` here).
* ``GET /api/system/env`` and ``/disk-usage`` (SYS-05/06, separate rows).

Observed behaviour worth pinning:

* ``features`` is a CONSTANT list of six names (``chat``, ``ai_coding``, ``app_builder``, ``channels``, ``model_builder``, ``security``). There is
  no setting that removes an entry, and the route registers no dependency on any of them: the inventory's "reflects what is enabled" is
  not true (SYS-ISS-14). A UI that hides an area by this list can never hide one.
* ``config/summary`` is exactly ``{host, port, docs_enabled, edition, csrf_enabled}`` and follows ``docs_enabled`` / ``csrf_enabled``.
* ``docs_enabled=false`` removes ``/docs``, ``/redoc`` AND ``/openapi.json`` (404); ``csrf_enabled=false`` makes mutating requests work without
  the ``X-QAI-CSRF`` header and stops the cookie from being issued.
* An invalid BOOLEAN setting stops the backend from starting (exit 1), it is not silently defaulted; an invalid PORT setting does not, because ``--port``
  replaces it before validation.
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e import conftest as c
from tests.e2e._owned_backend import owned_backend
from tests.e2e.test_chat_stream import _set_env

pytestmark = pytest.mark.e2e

FEATURES = ["chat", "ai_coding", "app_builder", "channels", "model_builder", "security"]
SUMMARY_KEYS = {"host", "port", "docs_enabled", "edition", "csrf_enabled"}
CHANNEL_PROBES = {
    "chat": "/api/chat/conversations", "ai_coding": "/api/cc/sessions", "app_builder": "/api/app-builder/models",
    "channels": "/api/feishu/status", "model_builder": "/api/model-catalog/entries", "security": "/api/security/runtime-config",
}


def _with(env: dict[str, str | None], fn):
    old = _set_env(env)
    try:
        with owned_backend() as ob:
            cl = ob.start()
            return fn(ob, cl)
    finally:
        _set_env(old)


def test_the_default_features_and_summary() -> None:
    def go(_ob, cl: httpx.Client) -> None:
        assert cl.get("/api/system/features").json() == {"features": FEATURES}
        s = cl.get("/api/system/config/summary").json()
        assert set(s) == SUMMARY_KEYS
        assert s == {"host": "127.0.0.1", "port": c.E2E_PORT, "docs_enabled": True, "edition": "external", "csrf_enabled": True}

    _with({}, go)


def test_every_listed_feature_has_a_live_route() -> None:
    """The list is only meaningful if each name maps to something that answers; none of these probes may be a 404 or a 5xx. (``channels`` is
    probed through ``/api/feishu/status``, which answers 400 because it needs an instance id - that still proves the route exists; ``model_builder`` has no
    parameter-free GET of its own, so the model catalog stands in for it.)"""
    def go(_ob, cl: httpx.Client) -> None:
        got = {name: cl.get(path).status_code for name, path in CHANNEL_PROBES.items()}
        # 400 is fine: ``/api/feishu/status`` exists but wants a query parameter. 404 would mean the route is gone; 5xx that it is broken.
        assert all(code in (200, 400) for code in got.values()), got
        assert got["channels"] == 400 and cl.get("/api/feishu/status").json()["code"] == "request.validation"

    _with({}, go)


def test_features_and_summary_are_read_only_and_cheap() -> None:
    def go(_ob, cl: httpx.Client) -> None:
        for path in ("/api/system/features", "/api/system/config/summary"):
            assert cl.post(path, json={}).status_code == 405 and cl.put(path, json={}).status_code == 405 and cl.delete(path).status_code == 405
            assert cl.get(path).json() == cl.get(path).json(), "stable between calls"

    _with({}, go)


@pytest.mark.parametrize(
    ("env", "expect"),
    [
        ({"QAI_SERVER__DOCS_ENABLED": "false"}, {"docs_enabled": False, "csrf_enabled": True}),
        ({"QAI_SECURITY__CSRF_ENABLED": "false"}, {"docs_enabled": True, "csrf_enabled": False}),
        ({"QAI_SERVER__DOCS_ENABLED": "false", "QAI_SECURITY__CSRF_ENABLED": "false"}, {"docs_enabled": False, "csrf_enabled": False}),
        ({"QAI_SERVER__DOCS_ENABLED": "0", "QAI_SECURITY__CSRF_ENABLED": "1"}, {"docs_enabled": False, "csrf_enabled": True}),
        ({"QAI_SERVER__HOST": "0.0.0.0"}, {"host": "0.0.0.0"}),
    ],
    ids=["docs-off", "csrf-off", "both-off", "0-and-1", "host"],
)
def test_the_summary_follows_the_settings(env: dict, expect: dict) -> None:
    def go(_ob, cl: httpx.Client) -> None:
        s = cl.get("/api/system/config/summary").json()
        assert {k: s[k] for k in expect} == expect
        assert {k: v for k, v in s.items() if k not in expect} == {k: v for k, v in {"host": "127.0.0.1", "port": c.E2E_PORT, "docs_enabled": True, "edition": "external", "csrf_enabled": True}.items() if k not in expect}
        assert cl.get("/api/system/features").json() == {"features": FEATURES}, "none of these settings touches the feature list"

    _with(env, go)


def test_turning_docs_off_removes_all_three_documentation_routes() -> None:
    def go(_ob, cl: httpx.Client) -> None:
        assert [cl.get(p).status_code for p in ("/docs", "/redoc", "/openapi.json")] == [404, 404, 404]
        assert cl.get("/api/system/health").status_code == 200

    _with({"QAI_SERVER__DOCS_ENABLED": "false"}, go)
    _with({}, lambda _ob, cl: [cl.get(p).status_code for p in ("/docs", "/redoc", "/openapi.json")] == [200, 200, 200] or pytest.fail("docs should be on by default"))


def test_turning_csrf_off_lets_a_bare_post_through_and_stops_issuing_the_cookie() -> None:
    def go(ob, _cl: httpx.Client) -> None:
        bare = httpx.Client(base_url=ob.base_url, timeout=30.0)
        assert bare.get("/api/system/uptime").status_code == 200 and "qai_csrf" not in bare.cookies
        r = bare.post("/api/chat/conversations", json={"title": "e2e-no-csrf"})
        assert r.status_code == 201
        bare.delete(f"/api/chat/conversations/{r.json()['id']}")
        bare.close()

    _with({"QAI_SECURITY__CSRF_ENABLED": "false"}, go)


def test_with_csrf_on_a_bare_post_is_refused_and_the_cookie_is_issued() -> None:
    def go(ob, _cl: httpx.Client) -> None:
        bare = httpx.Client(base_url=ob.base_url, timeout=30.0)
        bare.get("/api/system/uptime")
        assert bare.cookies.get("qai_csrf")
        r = bare.post("/api/chat/conversations", json={"title": "x"})
        assert r.status_code == 403 and r.json()["code"] == "security.csrf.missing"
        bare.close()

    _with({}, go)


@pytest.mark.parametrize("env", [{"QAI_SERVER__DOCS_ENABLED": "maybe"}, {"QAI_SERVER__DOCS_ENABLED": "2"}, {"QAI_SECURITY__CSRF_ENABLED": "perhaps"}],
                         ids=["docs-maybe", "docs-2", "csrf-perhaps"])
def test_an_invalid_boolean_stops_the_backend_from_starting(env: dict) -> None:
    old = _set_env(env)
    try:
        with owned_backend() as ob:
            with pytest.raises(RuntimeError) as exc:
                ob.start()
            assert "exited early with code 1" in str(exc.value)
    finally:
        _set_env(old)


@pytest.mark.parametrize("value", ["70000", "0", "abc", "-5"], ids=["too-big", "zero", "text", "negative"])
def test_the_port_setting_is_overridden_by_the_command_line_so_a_bad_value_is_harmless(value: str) -> None:
    """The harness (like ``Start.bat`` and the supervisor) starts the API with ``--port``; ``main()`` writes that value back into
    ``QAI_SERVER__PORT`` and into the settings, so an invalid environment value never gets validated. Pinned so nobody expects the
    setting alone to be checked at start-up."""
    def go(_ob, cl: httpx.Client) -> None:
        assert cl.get("/api/system/config/summary").json()["port"] == c.E2E_PORT

    _with({"QAI_SERVER__PORT": value}, go)
