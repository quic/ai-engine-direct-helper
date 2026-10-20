# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``UnexpectedErrorMiddleware``: an unhandled exception is answered with the 500 envelope
and is NOT re-raised (re-raising makes uvicorn drop the connection mid-response)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse
from starlette.testclient import TestClient

from interfaces.http.error_handlers import add_unexpected_error_middleware, register_error_handlers

pytestmark = pytest.mark.unit


class _Passthrough(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):  # noqa: ANN001
        return await call_next(request)


def _app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.add_middleware(_Passthrough)
    add_unexpected_error_middleware(app)

    @app.get("/boom")
    async def boom() -> None:
        raise AttributeError("'list' object has no attribute 'get'")

    @app.get("/ok")
    async def ok() -> dict:
        return {"ok": True}

    @app.get("/stream-then-boom")
    async def stream_then_boom() -> StreamingResponse:
        async def gen():  # noqa: ANN202
            yield b"first-chunk"
            raise RuntimeError("after the response started")

        return StreamingResponse(gen())

    return app


def test_an_unhandled_exception_becomes_the_500_envelope_and_is_not_re_raised() -> None:
    # raise_server_exceptions=True would re-raise whatever escapes the app: the point is that nothing does.
    with TestClient(_app(), raise_server_exceptions=True) as c:
        r = c.get("/boom")
    assert r.status_code == 500
    assert r.json() == {
        "type": "InternalServerError",
        "code": "internal.unexpected",
        "message": "An unexpected internal error occurred.",
    }


def test_a_normal_request_is_untouched() -> None:
    with TestClient(_app()) as c:
        assert c.get("/ok").json() == {"ok": True}


def test_the_server_keeps_answering_after_an_error() -> None:
    with TestClient(_app(), raise_server_exceptions=True) as c:
        assert c.get("/boom").status_code == 500
        assert c.get("/ok").status_code == 200


def test_once_the_response_has_started_the_exception_is_re_raised_unchanged() -> None:
    with TestClient(_app(), raise_server_exceptions=True) as c, pytest.raises(RuntimeError, match="after the response started"):
        c.get("/stream-then-boom")


def test_non_http_scopes_pass_through() -> None:
    import asyncio

    from interfaces.http.error_handlers import UnexpectedErrorMiddleware

    seen: list[str] = []

    async def inner(scope, receive, send):  # noqa: ANN001, ANN202
        seen.append(scope["type"])

    asyncio.run(UnexpectedErrorMiddleware(inner)({"type": "websocket"}, None, None))
    assert seen == ["websocket"]
