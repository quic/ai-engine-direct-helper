# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - chat on the CLOUD path WITH a key (CHAT-02 part 3). See ``test_chat_no_llm.py`` for the no-key cases and for what is not covered.

Own module: it keeps one backend alive for the whole run (port 8989), and the no-key module has its own. The key
(``e2e-key-123``) is only in the backend's environment (``QAI_CHAT__LLM_API_KEY``); the OS keyring is never touched, and the module
asserts no cloud credential appeared. The cloud path is selected with ``localhost.`` (trailing dot): it resolves to this machine but the
product's loopback check does not recognise it.

The two slow cases (429: 3 retries on a 1.5 s base; 5xx: 3 retries on 2/5/15 s) are kept because the retry frames and counts are
the contract; they take about 12 s and 25 s.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import httpx
import pytest

from tests.e2e import _fake_llm
from tests.e2e._fake_llm import StandInLLM
from tests.e2e._owned_backend import owned_backend
from tests.e2e.test_chat_stream import _keyring_cloud_state, _set_env, run_turn

pytestmark = pytest.mark.e2e

KEY = "e2e-key-123"
_START = _keyring_cloud_state()


@pytest.fixture(scope="module", autouse=True)
def keyring_untouched() -> Iterator[None]:
    yield
    assert _keyring_cloud_state() == _START


@pytest.fixture(scope="module")
def llm() -> Iterator[StandInLLM]:
    s = _fake_llm.start()
    yield s
    s.stop()


def _conv(cl: httpx.Client) -> str:
    return cl.post("/api/chat/conversations", json={"title": "e2e"}).json()["id"]

@pytest.fixture(scope="module")
def cloud_key(llm: StandInLLM) -> Iterator[httpx.Client]:
    old = _set_env({"QAI_CHAT__LLM_BASE_URL": llm.base_url.replace("127.0.0.1", "localhost."), "QAI_CHAT__LLM_DEFAULT_MODEL": "e2e-model", "QAI_CHAT__LLM_API_KEY": KEY})
    try:
        with owned_backend() as ob:
            yield ob.start()
    finally:
        _set_env(old)


def test_a_cloud_request_carries_the_bearer_key_and_the_model(cloud_key: httpx.Client, llm: StandInLLM) -> None:
    llm.clear()
    t = run_turn(cloud_key, _conv(cloud_key), "t-key", "[echo] hi")
    assert t.text == "ECHO: hi" and t.ends == ["completed", "snapshot"]
    (req,) = llm.chat_requests()
    auth = {k.lower(): v for k, v in req.headers.items()}["authorization"]
    assert auth == f"Bearer {KEY}" and req.body["model"] == "e2e-model"


def test_the_key_never_appears_in_any_reply_or_stored_message(cloud_key: httpx.Client, llm: StandInLLM) -> None:
    cid = _conv(cloud_key)
    run_turn(cloud_key, cid, "t-leak", "[echo] hi")
    run_turn(cloud_key, cid, "t-leak", "[error 401]")
    blob = json.dumps(cloud_key.get(f"/api/chat/conversations/{cid}/messages").json())
    assert KEY not in blob


@pytest.mark.parametrize(
    ("prompt", "code", "retryable"),
    [("[error 401]", "chat.llm.auth_failed", False), ("[error 400]", "chat.llm.http_error", False),
     ("[error 404]", "chat.llm.model_unavailable", False)],
)
def test_cloud_upstream_errors_are_classified(cloud_key: httpx.Client, llm: StandInLLM, prompt: str, code: str, retryable: bool) -> None:
    llm.clear()
    t = run_turn(cloud_key, _conv(cloud_key), "t-cls", prompt)
    (err,) = t.errors
    assert err["code"] == code and err["retryable"] is retryable and t.ends == ["failed"]
    assert len(llm.chat_requests()) == 1, "non-retryable: exactly one chat request"
    if code != "chat.llm.model_unavailable":
        assert "stand-in error" in err["message"], "the upstream's own message is passed through"


def test_a_429_is_retried_then_reported_as_throttling(cloud_key: httpx.Client, llm: StandInLLM) -> None:
    llm.clear()
    t0 = time.monotonic()
    t = run_turn(cloud_key, _conv(cloud_key), "t-429", "[error 429]", seconds=90)
    (err,) = t.errors
    assert err["code"] == "throttling" and err["retryable"] is True and t.ends == ["failed"]
    assert len(llm.chat_requests()) == 4, "the original try and 3 retries (MAX_THROTTLE_RETRIES)"
    assert 3 < time.monotonic() - t0 < 60, "the retries back off"


def test_a_5xx_is_retried_with_progress_frames_then_reported(cloud_key: httpx.Client, llm: StandInLLM) -> None:
    llm.clear()
    t0 = time.monotonic()
    t = run_turn(cloud_key, _conv(cloud_key), "t-5xx", "[error 500]", seconds=120)
    elapsed = time.monotonic() - t0
    retries = [f for f in t.frames("network_retry")]
    (err,) = t.errors
    assert err["code"] == "chat.llm.server_error" and t.ends == ["failed"]
    assert len(retries) == 3 and all(f["payload"]["code"] == "chat.llm.server_error" for f in retries)
    assert len(llm.chat_requests()) == 4, "the original try and 3 retries (BOUNDED_SERVER_MAX_RETRIES)"
    assert 6 < elapsed < 90, f"backoff 2+5+15 s (jittered), took {elapsed:.0f}s"

# --------------------------------------------------------------------------
# sampling parameters reach a cloud model
# --------------------------------------------------------------------------


def _sampling(req) -> dict:
    return {k: req.body.get(k) for k in ("temperature", "top_p", "max_tokens")}


def test_with_no_override_the_cloud_request_carries_the_default_temperature_only(cloud_key: httpx.Client, llm: StandInLLM) -> None:
    llm.clear()
    run_turn(cloud_key, _conv(cloud_key), "t-s0", "[echo] hi")
    assert _sampling(llm.chat_requests()[0]) == {"temperature": 0.7, "top_p": None, "max_tokens": None}


def test_the_sampling_query_parameters_are_forwarded_to_a_cloud_model(cloud_key: httpx.Client, llm: StandInLLM) -> None:
    llm.clear()
    run_turn(cloud_key, _conv(cloud_key), "t-s1", "[echo] hi", temperature=0.3, top_p=0.8, max_tokens=77)
    assert _sampling(llm.chat_requests()[0]) == {"temperature": 0.3, "top_p": 0.8, "max_tokens": 77}


def test_tool_params_may_carry_sampling_too_and_the_explicit_query_wins(cloud_key: httpx.Client, llm: StandInLLM) -> None:
    llm.clear()
    run_turn(cloud_key, _conv(cloud_key), "t-s2", "[echo] hi", tool_params=json.dumps({"temperature": 0.4, "top_p": 0.5}))
    assert _sampling(llm.chat_requests()[0]) == {"temperature": 0.4, "top_p": 0.5, "max_tokens": None}
    llm.clear()
    run_turn(cloud_key, _conv(cloud_key), "t-s3", "[echo] hi", tool_params=json.dumps({"temperature": 0.4}), temperature=0.9)
    assert _sampling(llm.chat_requests()[0])["temperature"] == 0.9


def test_max_tokens_zero_means_no_limit_and_is_not_sent(cloud_key: httpx.Client, llm: StandInLLM) -> None:
    llm.clear()
    run_turn(cloud_key, _conv(cloud_key), "t-s4", "[echo] hi", max_tokens=0)
    assert _sampling(llm.chat_requests()[0])["max_tokens"] is None


def test_a_malformed_tool_params_is_ignored_not_fatal(cloud_key: httpx.Client, llm: StandInLLM) -> None:
    llm.clear()
    t = run_turn(cloud_key, _conv(cloud_key), "t-s5", "[echo] hi", tool_params="{not json")
    assert t.text == "ECHO: hi" and _sampling(llm.chat_requests()[0])["temperature"] == 0.7
