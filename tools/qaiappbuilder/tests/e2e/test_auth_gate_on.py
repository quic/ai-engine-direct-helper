# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 — login gate ON (FEATURE-INVENTORY AUTH-01 / AUTH-02).

Starts a backend with ``QAI_AUTH__ENABLED=true`` and NO session. It never
performs a real SSO login (AUTH-04 is not automated) and never calls
``/auth/logout`` (that would delete the user's real JWT from the OS keyring).

IMPORTANT behaviour, taken from ``interfaces/http/middleware/auth.py`` (the
2026-07-17 UX contract): an unauthenticated BROWSER navigation is NOT redirected
to /auth/login. The SPA shell must render first and then show its own login
prompt, so HTML navigations pass through. Only API/JSON callers get 401. The
FEATURE-INVENTORY AUTH-01 row originally said "303 to /auth/login" for browser
requests; that was wrong and has been corrected.

Cookie forgery tests use the fixed ``QAI_AUTH__SESSION_SECRET`` the harness
gives the backend; they prove the gate validates the signature rather than
merely the presence of a cookie.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest

from tests.e2e.conftest import E2E_SESSION_SECRET, Backend

pytestmark = pytest.mark.e2e

COOKIE = "qai_session"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _client_with_session(backend: Backend, cookie: str) -> httpx.Client:
    """A fresh client carrying ``cookie`` as its session.

    Cookies are set on the client instance (not per request): httpx deprecates
    per-request ``cookies=`` and the project runs pytest with warnings-as-errors.
    A fresh client per call also keeps tests from sharing cookie state.
    """
    client = httpx.Client(base_url=backend.base_url, timeout=15.0)
    client.cookies.set(COOKIE, cookie, domain="127.0.0.1", path="/")
    return client


def _forge_cookie(secret: str, *, exp_offset: int = 3600) -> str:
    """Build a session cookie the same way ``dump_session`` does."""
    now = int(time.time())
    payload = {
        "user": {"username": "e2e-user", "email": "e2e@example.invalid", "name": "e2e"},
        "iat": now,
        "exp": now + exp_offset,
    }
    data = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), data.encode(), hashlib.sha256).digest())
    return f"{data}.{sig}"


# ---------------------------------------------------------------------------
# AUTH-01  unauthenticated requests
# ---------------------------------------------------------------------------


def test_auth01_me_reports_gate_enabled_and_not_authenticated(auth_http: httpx.Client):
    body = auth_http.get("/api/auth/me").json()
    assert body["auth_enabled"] is True
    assert body["authenticated"] is False
    assert body["user"] is None


@pytest.mark.parametrize(
    "path", ["/api/chat/conversations", "/api/system/features", "/api/system/uptime"]
)
def test_auth01_api_request_without_session_gets_401_envelope(
    auth_http: httpx.Client, path: str
):
    r = auth_http.get(path)
    assert r.status_code == 401
    body = r.json()
    assert body["type"] == "UnauthorizedError"
    assert body["code"] == "auth.required"
    # The envelope tells the SPA where to log in, preserving the original path.
    assert body["details"]["login_url"].startswith("/auth/login?next=")


def test_auth01_login_url_preserves_path_and_query(auth_http: httpx.Client):
    body = auth_http.get("/api/chat/conversations?limit=3").json()
    assert "%2Fapi%2Fchat%2Fconversations%3Flimit%3D3" in body["details"]["login_url"]


def test_auth01_browser_navigation_is_NOT_redirected(auth_http: httpx.Client):
    """The SPA shell must load first; the redirect-to-Okta behaviour was removed."""
    r = auth_http.get("/chat", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code not in (301, 302, 303, 307, 308), (
        "a server-side redirect to the login page is a regression of the "
        "2026-07-17 UX contract"
    )
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_auth01_json_fetch_to_a_non_api_path_is_401(auth_http: httpx.Client):
    """Non-HTML callers (no html/wildcard Accept) get the envelope, not the SPA."""
    r = auth_http.get("/chat", headers={"accept": "application/json"})
    assert r.status_code == 401
    assert r.json()["code"] == "auth.required"


def test_auth01_garbage_cookie_is_treated_as_unauthenticated(auth_backend: Backend):
    with _client_with_session(auth_backend, "garbage.value") as c:
        assert c.get("/api/chat/conversations").status_code == 401


def test_auth01_cookie_signed_with_the_wrong_secret_is_rejected(auth_backend: Backend):
    forged = _forge_cookie("not-the-real-secret")
    with _client_with_session(auth_backend, forged) as c:
        assert c.get("/api/chat/conversations").status_code == 401


def test_auth01_expired_cookie_is_rejected(auth_backend: Backend):
    expired = _forge_cookie(E2E_SESSION_SECRET, exp_offset=-60)
    with _client_with_session(auth_backend, expired) as c:
        assert c.get("/api/chat/conversations").status_code == 401


def test_auth01_validly_signed_cookie_is_accepted(auth_backend: Backend):
    """Positive control: proves the 401s above come from validation, not a blanket deny."""
    good = _forge_cookie(E2E_SESSION_SECRET)
    with _client_with_session(auth_backend, good) as c:
        r = c.get("/api/chat/conversations")
    assert r.status_code == 200, r.text


def test_auth01_me_recognises_a_valid_session(auth_backend: Backend):
    good = _forge_cookie(E2E_SESSION_SECRET)
    with _client_with_session(auth_backend, good) as c:
        body = c.get("/api/auth/me").json()
    assert body["authenticated"] is True
    assert body["user"]["username"] == "e2e-user"
    assert body["expires_at"] > time.time()


# ---------------------------------------------------------------------------
# AUTH-02  public paths need no login
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/system/health",
        "/api/system/build-info",
        "/api/system/edition",
        "/api/auth/me",
        "/openapi.json",
        "/auth/signed-out",
    ],
)
def test_auth02_public_paths_are_reachable_without_a_session(
    auth_http: httpx.Client, path: str
):
    r = auth_http.get(path, follow_redirects=False)
    assert r.status_code == 200, f"{path} -> {r.status_code}"


def test_auth02_spa_root_and_assets_are_public(auth_http: httpx.Client):
    assert auth_http.get("/", headers={"accept": "text/html"}).status_code == 200


def test_auth02_login_endpoint_is_reachable_and_does_not_leak_a_session(
    auth_http: httpx.Client,
):
    """/auth/login starts the Okta redirect; it must not 401, and must not set a session."""
    r = auth_http.get("/auth/login", follow_redirects=False)
    assert r.status_code != 401
    assert COOKIE not in r.cookies


def test_auth02_v1_proxy_is_not_gated_by_the_session(auth_http: httpx.Client):
    """/v1/ authenticates on its own credentials, so the session gate must not 401 it.

    We only assert it is not the SESSION gate's envelope; the /v1 handler may
    still answer with its own status (e.g. 401 for a missing API key, 404, 200).
    """
    r = auth_http.get("/v1/models")
    try:
        body = r.json()
    except ValueError:
        body = {}
    assert not (isinstance(body, dict) and body.get("code") == "auth.required"), (
        "/v1/ was blocked by the session gate"
    )


def test_auth02_the_gate_blocks_everything_not_on_the_public_list(auth_http: httpx.Client):
    """Guard against the public list silently growing: a sample of private routes."""
    for path in ("/api/chat/conversations", "/api/security/grants", "/api/versions"):
        r = auth_http.get(path)
        assert r.status_code == 401, f"{path} is reachable without login"


def test_auth_gate_on_backend_is_isolated(auth_backend: Backend):
    """The gate-ON backend must still use the temp data dir, never a real one."""
    assert "qai-e2e-" in str(auth_backend.data_dir)
