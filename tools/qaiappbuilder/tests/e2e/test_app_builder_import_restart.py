# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - an imported App Builder model survives a restart (AB-13, part 2).

Own module with its own backend: ``test_app_builder_import.py`` keeps a module-scoped backend on port 8989, and a restart needs its own.
"""

from __future__ import annotations

import pytest

from tests.e2e._owned_backend import owned_backend
from tests.e2e.test_app_builder_import import AB, BUILTIN, _commit, _ids, _plan, make_pack

pytestmark = pytest.mark.e2e

def test_an_imported_model_is_listed_after_the_built_in_ones_and_survives_a_restart() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        c = ob.work / "c"
        c.mkdir()
        _commit(cl, _plan(cl, make_pack(c, "re", "e2e-restart")))
        assert sorted(_ids(cl)[:4]) == sorted(BUILTIN) and _ids(cl)[4:] == ["e2e-restart"]
        ob.stop()
        cl2 = ob.start()
        assert _ids(cl2) == [*BUILTIN, "e2e-restart"] and cl2.get(f"{AB}/models/e2e-restart").json()["user_imported"] is True
