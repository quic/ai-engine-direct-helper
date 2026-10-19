# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - global event stream with the login gate ON (EVT-01, part 2).

The Okta login gate (``AuthMiddleware``) is a Starlette ``BaseHTTPMiddleware``: it sees HTTP requests only. A WebSocket
handshake never passes through it, and the CSRF middleware states that WebSockets are "intentionally NOT covered" and that
"if a specific WS path needs origin protection, the route handler must add it explicitly". ``/api/ws/events`` does not.

Result (pinned below, real backend with ``QAI_AUTH__ENABLED=true``):

* ``GET /api/events`` (SSE) without a session is a clean ``401``; with a valid session it streams.
* ``WS /api/ws/events`` is accepted with **no cookie, a garbage cookie, a wrong-secret cookie, an expired cookie, and any
  ``Origin``** - and then delivers real domain events: a conversation title, the path and requester of a permission
  request, ... to an anonymous client. Of the five chat WebSockets, three are accepted the same way (``/api/chat/control``,
  ``/api/chat/subagents/{id}/ws``, ``/api/chat/active-runs/{tab}/ws``); ``/api/chat/ws`` answers 403.

KNOWN GAP - SECURITY (current behaviour, pinned): when the gate is on, the WebSocket twin of an authenticated endpoint is
open. The product comment calls the deployment "loopback-only", which limits exposure to the local machine - but any web
page open in the user's browser can connect to ``ws://127.0.0.1:<port>/api/ws/events`` cross-origin (browsers do not apply
the same-origin policy to WebSockets), so a page on another site can read the event stream. If the gate or the route is
fixed, the "accepted" assertions below fail - update them then (and the SSE/WS pair should become consistent).

Isolation (verified): the backend is the shared auth-ON fixture on a throw-away data dir; the session cookie is forged with
the e2e session secret (never a real one); the actor creates one conversation and one permission request and removes both.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from tests.e2e import conftest as c
from tests.e2e.conftest import Backend
from tests.e2e.test_auth_gate_on import COOKIE, _forge_cookie

pytestmark = pytest.mark.e2e

WS = f"ws://{c.E2E_HOST}:{c.E2E_PORT}"


def _cookie(secret: str = c.E2E_SESSION_SECRET, **kw: int) -> dict[str, str]:
    return {"Cookie": f"{COOKIE}={_forge_cookie(secret, **kw)}"}


def recv_all(ws, seconds: float) -> list[str]:
    out, end = [], time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            out.append(ws.recv(timeout=0.3))
        except TimeoutError:
            continue
    return out


@pytest.fixture
def actor(auth_backend: Backend) -> Iterator[httpx.Client]:
    """A client holding a validly signed session AND the CSRF cookie, so it can do real work."""
    with httpx.Client(base_url=auth_backend.base_url, timeout=15.0) as cl:
        cl.cookies.set(COOKIE, _forge_cookie(c.E2E_SESSION_SECRET), domain="127.0.0.1", path="/")
        cl.get("/api/system/uptime")
        cl.headers["X-QAI-CSRF"] = cl.cookies.get("qai_csrf") or ""
        assert cl.get("/api/auth/me").json()["authenticated"] is True
        yield cl


# --------------------------------------------------------------------------
# SSE is gated
# --------------------------------------------------------------------------


def test_sse_without_a_session_is_a_401_envelope(auth_backend: Backend) -> None:
    r = httpx.get(auth_backend.base_url + "/api/events", timeout=5)
    assert r.status_code == 401 and r.headers["content-type"].startswith("application/json")
    assert r.json()["code"] == "auth.required" and r.json()["details"]["login_url"].startswith("/auth/login?next=%2Fapi%2Fevents")


@pytest.mark.parametrize("kind", ["garbage", "wrong-secret", "expired"])
def test_sse_with_a_bad_session_is_a_401(auth_backend: Backend, kind: str) -> None:
    value = {"garbage": "garbage", "wrong-secret": _forge_cookie("not-the-secret"), "expired": _forge_cookie(c.E2E_SESSION_SECRET, exp_offset=-10)}[kind]
    with httpx.Client(base_url=auth_backend.base_url, timeout=5) as cl:
        cl.cookies.set(COOKIE, value, domain="127.0.0.1", path="/")
        assert cl.get("/api/events").status_code == 401


def test_sse_with_a_valid_session_streams(auth_backend: Backend) -> None:
    with httpx.Client(base_url=auth_backend.base_url, timeout=httpx.Timeout(5.0, read=1.5)) as cl:
        cl.cookies.set(COOKIE, _forge_cookie(c.E2E_SESSION_SECRET), domain="127.0.0.1", path="/")
        with cl.stream("GET", "/api/events") as r:
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")


# --------------------------------------------------------------------------
# WebSocket is not
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [None, {"Cookie": f"{COOKIE}=garbage"}, {"Cookie": f"{COOKIE}=" + _forge_cookie("not-the-secret")},
     {"Cookie": f"{COOKIE}=" + _forge_cookie(c.E2E_SESSION_SECRET, exp_offset=-10)}],
    ids=["no-cookie", "garbage", "wrong-secret", "expired"],
)
def test_ws_events_accepts_a_client_with_no_valid_session(auth_backend: Backend, headers: dict | None) -> None:
    """KNOWN GAP - SECURITY (current behaviour, pinned): the handshake succeeds although the HTTP twin returns 401."""
    with connect(f"{WS}/api/ws/events", additional_headers=headers, open_timeout=5):
        pass


@pytest.mark.parametrize("origin", [None, "http://127.0.0.1:8989", "http://evil.example", "null"])
def test_ws_events_accepts_any_origin(auth_backend: Backend, origin: str | None) -> None:
    """KNOWN GAP - SECURITY (current behaviour, pinned): no ``Origin`` check on the handshake."""
    with connect(f"{WS}/api/ws/events", origin=origin, open_timeout=5):
        pass


def test_an_anonymous_ws_client_receives_the_real_events_of_the_logged_in_user(actor: httpx.Client, auth_backend: Backend) -> None:
    """KNOWN GAP - SECURITY (current behaviour, pinned): the content of the stream reaches a client with no session."""
    title = "e2e-anon-" + uuid.uuid4().hex[:6]
    secret_path = rf"C:\e2e-private\{uuid.uuid4().hex[:8]}\diary.txt"
    rid = None
    cid = None
    with connect(f"{WS}/api/ws/events", origin="http://evil.example", open_timeout=5) as anon:
        time.sleep(0.4)
        try:
            cid = actor.post("/api/chat/conversations", json={"title": title}).json()["id"]
            rid = actor.post("/api/security/permission/request", json={
                "subject": {"kind": "user", "identifier": "e2e-private-subject"},
                "resource": {"kind": "path", "identifier": secret_path}, "requested_mask": {"read": True}}).json()["request"]["request_id"]
            frames = [json.loads(m) for m in recv_all(anon, 1.5)]
        finally:
            if rid:
                actor.delete(f"/api/security/permission/{rid}")
            if cid:
                actor.delete(f"/api/chat/conversations/{cid}")
    by_type = {f["type"]: f for f in frames}
    assert by_type["chat.created_conversation"]["title"] == title
    assert by_type["permission_request"]["path"] == secret_path and by_type["permission_request"]["caller"] == "e2e-private-subject"


# --------------------------------------------------------------------------
# the other chat WebSockets (so a fix is applied consistently)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "accepted"),
    [
        ("/api/ws/events", True),
        ("/api/chat/control", True),
        ("/api/chat/subagents/x/ws", True),
        ("/api/chat/active-runs/tab1/ws", True),
        ("/api/chat/ws", False),
    ],
)
def test_which_websockets_accept_a_cross_origin_client_with_no_session(auth_backend: Backend, path: str, accepted: bool) -> None:
    """KNOWN GAP - SECURITY (current behaviour, pinned): only ``/api/chat/ws`` refuses (403). The accepted ones answer
    with a ``hello`` / an application error frame, i.e. their handlers run without any identity."""
    if accepted:
        with connect(f"{WS}{path}", origin="http://evil.example", open_timeout=5):
            pass
    else:
        with pytest.raises(InvalidStatus) as exc:
            connect(f"{WS}{path}", origin="http://evil.example", open_timeout=5)
        assert exc.value.response.status_code == 403


def test_the_http_twin_of_a_chat_websocket_is_gated(auth_backend: Backend) -> None:
    assert httpx.get(auth_backend.base_url + "/api/chat/conversations", timeout=5).status_code == 401


def test_the_control_websocket_greets_an_anonymous_client(auth_backend: Backend) -> None:
    """KNOWN GAP - SECURITY (current behaviour, pinned): the protocol hello is sent to a client with no session."""
    with connect(f"{WS}/api/chat/control", origin="http://evil.example", open_timeout=5) as ws:
        assert json.loads(ws.recv(timeout=3)) == {"type": "hello", "protocol": "qai.chat.control/1"}
