# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 — login gate OFF, and CSRF (FEATURE-INVENTORY AUTH-03, SEC-01).

Uses the login-gate-OFF ``backend`` fixture. The login-gate-ON behaviour
(AUTH-01 / AUTH-02) lives in ``test_auth_gate_on.py`` because the two backends
cannot share port 8989.

Behaviour is taken from ``interfaces/http/middleware/{auth,csrf}.py`` and
``routes/auth.py``.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.e2e

CSRF_COOKIE = "qai_csrf"
CSRF_HEADER = "X-QAI-CSRF"


# ---------------------------------------------------------------------------
# AUTH-03  gate disabled -> protected endpoints are reachable
# ---------------------------------------------------------------------------


def test_auth03_auth_me_reports_gate_disabled(http: httpx.Client):
    """With the gate off every caller is implicitly signed in; no user object."""
    body = http.get("/api/auth/me").json()
    assert body == {
        "auth_enabled": False,
        "authenticated": True,
        "user": None,
        "expires_at": None,
    }


@pytest.mark.parametrize(
    "path",
    [
        "/api/chat/conversations",
        "/api/system/features",
        "/api/system/uptime",
    ],
)
def test_auth03_protected_endpoints_need_no_login(http: httpx.Client, path: str):
    r = http.get(path)
    assert r.status_code == 200, r.text
    # And specifically NOT the auth gate's envelope.
    assert r.json().get("code") != "auth.required"


# ---------------------------------------------------------------------------
# SEC-01  CSRF: double-submit cookie + header on unsafe methods
# ---------------------------------------------------------------------------

#: A harmless unsafe-method endpoint we can hit without side effects when the
#: CSRF check REJECTS it (the 403 happens before the handler runs).
UNSAFE_PATH = "/api/chat/conversations"


def _csrf_token(client: httpx.Client) -> str:
    """Obtain the CSRF cookie the server installs on the first response."""
    client.get("/api/system/uptime")
    token = client.cookies.get(CSRF_COOKIE)
    assert token, "server did not set the qai_csrf cookie on a safe request"
    return token


def test_sec01_server_installs_a_csrf_cookie(http: httpx.Client):
    token = _csrf_token(http)
    assert len(token) >= 16


def test_sec01_unsafe_request_without_cookie_or_header_is_rejected(http: httpx.Client):
    r = httpx.post(
        f"{http.base_url}{UNSAFE_PATH}", json={"title": "e2e-csrf-none"}, timeout=15.0
    )
    assert r.status_code == 403
    assert r.json()["code"] == "security.csrf.missing"


def test_sec01_header_without_cookie_is_rejected(http: httpx.Client):
    r = httpx.post(
        f"{http.base_url}{UNSAFE_PATH}",
        json={"title": "e2e-csrf-header-only"},
        headers={CSRF_HEADER: "anything"},
        timeout=15.0,
    )
    assert r.status_code == 403
    assert r.json()["code"] == "security.csrf.missing"


def test_sec01_cookie_without_header_is_rejected(http: httpx.Client):
    token = _csrf_token(http)
    r = http.post(UNSAFE_PATH, json={"title": "e2e-csrf-cookie-only"})
    assert r.status_code == 403, "cookie alone must not be enough"
    assert http.cookies.get(CSRF_COOKIE) == token


def test_sec01_mismatched_header_is_rejected(http: httpx.Client):
    _csrf_token(http)
    r = http.post(
        UNSAFE_PATH,
        json={"title": "e2e-csrf-mismatch"},
        headers={CSRF_HEADER: "not-the-cookie-value"},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "security.csrf.missing"


def test_sec01_matching_cookie_and_header_passes_the_csrf_check(http: httpx.Client):
    """A valid pair gets through CSRF. Creates then deletes its own conversation."""
    token = _csrf_token(http)
    r = http.post(
        UNSAFE_PATH,
        json={"title": "e2e-csrf-ok"},
        headers={CSRF_HEADER: token},
    )
    assert r.status_code == 201, r.text
    conv_id = r.json()["id"]
    try:
        assert r.json()["title"] == "e2e-csrf-ok"
    finally:
        d = http.delete(f"{UNSAFE_PATH}/{conv_id}", headers={CSRF_HEADER: token})
        assert d.status_code == 204


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_sec01_safe_methods_need_no_token(http: httpx.Client, method: str):
    r = httpx.request(method, f"{http.base_url}/api/system/uptime", timeout=15.0)
    assert r.status_code != 403
