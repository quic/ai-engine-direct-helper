# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - ``/v1/*`` with NO bearer guard configured (CHAT-06, part 2).

Own module: ``test_openai_compat.py`` keeps a guarded backend alive on port 8989 for its whole run, and a second backend cannot start
until it is gone - the suite waits 30 s and then aborts the session rather than touching a process it did not start. (That is
exactly what happened to this test while it lived in the other module: it never ran, and the abort was easy to mistake for noise.)
"""

from __future__ import annotations

import pytest

from tests.e2e._owned_backend import owned_backend
from tests.e2e.test_chat_stream import _set_env

pytestmark = pytest.mark.e2e

V = "/v1"
MSG = {"model": "m", "messages": [{"role": "user", "content": "x"}]}


def test_without_a_configured_key_the_routes_are_open() -> None:
    old = _set_env({"QAI_SERVER__OPENAI_API_KEY": None, "QAI_CHAT__LLM_BASE_URL": None})
    try:
        with owned_backend() as ob:
            cl = ob.start()
            assert cl.get(f"{V}/models").status_code == 200
            assert cl.get(f"{V}/models", headers={"Authorization": "Bearer anything"}).status_code == 200
            assert cl.get(f"{V}/models/qai-default").status_code == 200
            assert cl.post(f"{V}/chat/completions", json=MSG).status_code == 200
    finally:
        _set_env(old)
