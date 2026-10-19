# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - chat when there is no model to talk to, and the cloud-path error handling (CHAT-10, CHAT-02 part 2).

Three configurations, one backend per test (never module-scoped: two backends cannot share port 8989) (the chat endpoint is read at start-up from ``QAI_CHAT__LLM_*``):

* **Nothing configured** - CHAT-10. The turn does not hang and is not an error: it ends ``completed`` with the fixed text
  ``[no LLM endpoint configured]``.
* **Loopback endpoint with nothing listening** - the on-device path. One explanatory ``chat.local.service_unavailable`` error
  frame (in Chinese, naming Service as the thing to start) and the turn still ends ``completed``.
* **Cloud path** (``localhost.`` - resolves to this machine but the product does not recognise it as loopback), with and without a
  key. No key: ``chat.llm.provider_api_key_missing`` BEFORE any network call. With a key: ``Authorization: Bearer <key>`` is sent and
  upstream failures are classified (401 ``auth_failed``, 400 ``http_error``, 404 ``model_unavailable``, 429 ``throttling``).

NOT covered (deliberately):

* **A cloud endpoint that cannot be reached.** The product retries transient network faults on a 3/5/10/30 s schedule for up to
  10 minutes before giving up; a refused connection is retried 3 times on 1/3/10 s. Both are sensible, and both are far too slow
  for the suite (a probe waited 150 s and was cancelled). 5xx on the cloud path is likewise retried 3 times on 2/5/15 s.
* A real provider, a real key, or the OS keyring: the key here is an environment variable of the throw-away backend.
* ``Retry-After`` handling on 429 (the stand-in does not send the header).

Isolation (verified): the key (``e2e-key-123``) exists only in the backend's environment; the module asserts the OS keyring holds no new
cloud credential. The stand-in binds loopback on an ephemeral port.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator

import httpx
import pytest

from tests.e2e import _fake_llm
from tests.e2e._fake_llm import StandInLLM
from tests.e2e._owned_backend import owned_backend
from tests.e2e.test_chat_stream import Turn, _keyring_cloud_state, _set_env, run_turn

pytestmark = pytest.mark.e2e

KEY = "e2e-key-123"


@pytest.fixture(scope="module")
def llm() -> Iterator[StandInLLM]:
    s = _fake_llm.start()
    yield s
    s.stop()


_START = _keyring_cloud_state()


@pytest.fixture(scope="module", autouse=True)
def keyring_untouched() -> Iterator[None]:
    yield
    assert _keyring_cloud_state() == _START


def _conv(cl: httpx.Client) -> str:
    return cl.post("/api/chat/conversations", json={"title": "e2e"}).json()["id"]


def _cloud_url(llm: StandInLLM) -> str:
    return llm.base_url.replace("127.0.0.1", "localhost.")


# --------------------------------------------------------------------------
# CHAT-10: nothing configured
# --------------------------------------------------------------------------


def test_with_no_endpoint_configured_the_turn_ends_at_once_with_a_fixed_notice() -> None:
    old = _set_env({"QAI_CHAT__LLM_BASE_URL": None, "QAI_CHAT__LLM_API_KEY": None, "QAI_CHAT__LLM_DEFAULT_MODEL": None})
    try:
        with owned_backend() as ob:
            cl = ob.start()
            cid = _conv(cl)
            t0 = time.monotonic()
            t = run_turn(cl, cid, "t-none", "hello", seconds=20)
            assert time.monotonic() - t0 < 10, "it must not hang"
            assert t.status == 200 and t.text == "[no LLM endpoint configured]"
            assert t.ends == ["completed", "snapshot"] and t.errors == [] and t.event_names[-1] == "done"
            items = cl.get(f"/api/chat/conversations/{cid}/messages").json()["items"]
            assert [(m["role"], m["text"]) for m in items] == [("user", "hello"), ("assistant", "[no LLM endpoint configured]")]
            for prompt in ("[error 500]", "[slow] 5", "[tool read {}]"):
                assert run_turn(cl, _conv(cl), "t-none2", prompt, seconds=20).text == "[no LLM endpoint configured]"
    finally:
        _set_env(old)


def test_with_no_endpoint_the_model_list_and_health_are_still_fine() -> None:
    old = _set_env({"QAI_CHAT__LLM_BASE_URL": None, "QAI_CHAT__LLM_API_KEY": None, "QAI_CHAT__LLM_DEFAULT_MODEL": None})
    try:
        with owned_backend() as ob:
            cl = ob.start()
            assert cl.get("/api/system/health").status_code == 200
            assert cl.get("/api/chat/tools").status_code == 200
            assert cl.post("/api/chat/stop", json={"tab_id": "x"}).status_code == 200
    finally:
        _set_env(old)


# --------------------------------------------------------------------------
# on-device path, nothing listening
# --------------------------------------------------------------------------


def test_a_loopback_endpoint_with_nothing_listening_explains_what_to_start() -> None:
    old = _set_env({"QAI_CHAT__LLM_BASE_URL": "http://127.0.0.1:9/v1", "QAI_CHAT__LLM_DEFAULT_MODEL": "e2e-model", "QAI_CHAT__LLM_API_KEY": None})
    try:
        with owned_backend() as ob:
            cl = ob.start()
            t0 = time.monotonic()
            t = run_turn(cl, _conv(cl), "t-down", "[echo] hi", seconds=60)
            assert time.monotonic() - t0 < 40
            assert [e["code"] for e in t.errors] == ["chat.local.service_unavailable"]
            msg = t.errors[0]["message"]
            assert "Service" in msg and "1." in msg, "the message lists what to check, starting with Service"
            assert t.ends == ["completed", "snapshot"], "the turn itself still completes"
            # the reply is saved so the explanation stays in the conversation
            cid = _conv(cl)
            run_turn(cl, cid, "t-down2", "[echo] hi", seconds=60)
            roles = [m["role"] for m in cl.get(f"/api/chat/conversations/{cid}/messages").json()["items"]]
            assert roles == ["user", "assistant"]
    finally:
        _set_env(old)


@pytest.mark.parametrize(
    ("host", "treated_as_local"),
    [("localhost", True), ("LOCALHOST", True), ("127.0.0.1", True), ("127.0.0.2", True), ("[::1]", True), ("localhost.", False)],
)
def test_which_spellings_of_this_machine_count_as_an_on_device_endpoint(llm: StandInLLM, host: str, treated_as_local: bool) -> None:
    """On-device (loopback) means: the name ``localhost`` (any case), or an IP literal that is loopback. A trailing dot is NOT
    recognised, so ``localhost.`` takes the cloud path and, with no key, is stopped before the network. Observable difference:
    the on-device path says ``service_unavailable`` when nothing listens, the cloud path says ``provider_api_key_missing``.
    The port is closed (``:9``) so no request can succeed either way."""
    old = _set_env({"QAI_CHAT__LLM_BASE_URL": f"http://{host}:9/v1", "QAI_CHAT__LLM_DEFAULT_MODEL": "e2e-model", "QAI_CHAT__LLM_API_KEY": None})
    try:
        with owned_backend() as ob:
            cl = ob.start()
            t = run_turn(cl, _conv(cl), "t-host", "[echo] hi", seconds=60)
            codes = [e["code"] for e in t.errors]
            assert codes == (["chat.local.service_unavailable"] if treated_as_local else ["chat.llm.provider_api_key_missing"]), (host, codes)
    finally:
        _set_env(old)

# --------------------------------------------------------------------------
# cloud path
# --------------------------------------------------------------------------


def test_a_cloud_request_without_a_key_is_stopped_before_the_network(llm: StandInLLM) -> None:
    old = _set_env({"QAI_CHAT__LLM_BASE_URL": _cloud_url(llm), "QAI_CHAT__LLM_DEFAULT_MODEL": "e2e-model", "QAI_CHAT__LLM_API_KEY": None})
    try:
        with owned_backend() as ob:
            cl = ob.start()
            llm.clear()
            for prompt in ("[echo] hi", "[error 401]"):
                t = run_turn(cl, _conv(cl), "t-nokey", prompt)
                (err,) = t.errors
                assert err["code"] == "chat.llm.provider_api_key_missing" and err["retryable"] is False
                assert err["message"] == "This cloud model has no API key configured. Set your API key to start using it."
                assert t.ends == ["failed"] and t.text == ""
            assert llm.chat_requests() == [], "nothing was sent"
    finally:
        _set_env(old)
