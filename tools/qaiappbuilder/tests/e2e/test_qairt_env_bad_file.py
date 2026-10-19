# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - a ``qairt_env.json`` that is valid JSON but not an object stops the backend from starting (MB-07, part 3).

Own module: each case needs a backend that FAILS to start (about 10 s each), and none may overlap a module-scoped backend.
"""

from __future__ import annotations

import pytest

from tests.e2e._owned_backend import owned_backend

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("payload", ["[1, 2]", '"just a string"', "null"], ids=["list", "string", "null"])
def test_a_valid_json_env_file_that_is_not_an_object_stops_the_backend_from_starting(payload: str) -> None:
    """KNOWN GAP (current behaviour, pinned) - MB-ISS-01: ``resolve_qairt_installation`` catches ``(OSError, ValueError,
    TypeError)`` but ``payload.get`` on a list/str/int/None raises ``AttributeError``. It is called while the DI container is
    built, so the whole API fails to start (exit 1) instead of degrading like a corrupt file does. If the function is hardened
    this becomes a normal start with no active version - update the test then. (``5`` behaves the same; omitted for time.)"""
    with owned_backend() as ob:
        cfg = ob.data_dir / "config"
        cfg.mkdir()
        (cfg / "qairt_env.json").write_text(payload, encoding="utf-8")
        with pytest.raises(RuntimeError) as exc:
            ob.start()
        assert "backend exited early with code 1" in str(exc.value)
        assert "AttributeError" in str(exc.value) and "has no attribute 'get'" in str(exc.value)