# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Self-tests for the E2E harness itself (no backend, no network).

These guard the pre-flight logic that every real-JWT test depends on: if
``read_real_jwt_status`` silently returned ``ok`` for a bad state, every
real-JWT test would pass for the wrong reason.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from tests.e2e import _jwt
from tests.e2e._jwt import JwtProblem, read_real_jwt_status

pytestmark = pytest.mark.e2e

NOW = 1_800_000_000.0


class _FakeKeyringError(Exception):
    pass


def _install_fake_keyring(monkeypatch: pytest.MonkeyPatch, *, value=None, raises=None, fail_backend=False):
    """Replace the ``keyring`` modules ``_jwt`` imports with a controllable fake."""

    class _FailKeyring:  # stands in for keyring.backends.fail.Keyring
        pass

    class _GoodKeyring:
        pass

    kr = types.ModuleType("keyring")
    kr.get_keyring = lambda: _FailKeyring() if fail_backend else _GoodKeyring()

    def _get_password(service, key):
        assert (service, key) == (_jwt.SECRET_SERVICE, _jwt.SECRET_KEY)
        if raises is not None:
            raise raises
        return value

    kr.get_password = _get_password
    errors = types.ModuleType("keyring.errors")
    errors.KeyringError = _FakeKeyringError
    backends = types.ModuleType("keyring.backends")
    fail = types.ModuleType("keyring.backends.fail")
    fail.Keyring = _FailKeyring
    kr.errors, kr.backends = errors, backends
    backends.fail = fail
    for name, mod in {
        "keyring": kr,
        "keyring.errors": errors,
        "keyring.backends": backends,
        "keyring.backends.fail": fail,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


def _blob(deadline: float, jwt: str = "tok") -> str:
    return json.dumps({"jwt": jwt, "deadline": deadline})


def test_valid_jwt_is_ok(monkeypatch):
    _install_fake_keyring(monkeypatch, value=_blob(NOW + 3 * 3600))
    st = read_real_jwt_status(now=NOW)
    assert st.ok and st.problem is None
    assert st.remaining_seconds == pytest.approx(3 * 3600)


def test_missing_entry_is_not_found(monkeypatch):
    _install_fake_keyring(monkeypatch, value=None)
    st = read_real_jwt_status(now=NOW)
    assert not st.ok and st.problem is JwtProblem.NOT_FOUND


def test_expired_jwt(monkeypatch):
    _install_fake_keyring(monkeypatch, value=_blob(NOW - 60))
    st = read_real_jwt_status(now=NOW)
    assert not st.ok and st.problem is JwtProblem.EXPIRED


def test_expiring_soon_jwt(monkeypatch):
    _install_fake_keyring(monkeypatch, value=_blob(NOW + 5 * 60))
    st = read_real_jwt_status(now=NOW)
    assert not st.ok and st.problem is JwtProblem.EXPIRING_SOON


@pytest.mark.parametrize(
    "value",
    ["not json", "{}", json.dumps({"jwt": "", "deadline": NOW + 9999}), json.dumps({"jwt": "x"}), "[]"],
)
def test_malformed_blob(monkeypatch, value):
    _install_fake_keyring(monkeypatch, value=value)
    st = read_real_jwt_status(now=NOW)
    assert not st.ok and st.problem is JwtProblem.MALFORMED


def test_keyring_error(monkeypatch):
    _install_fake_keyring(monkeypatch, raises=_FakeKeyringError("boom"))
    st = read_real_jwt_status(now=NOW)
    assert not st.ok and st.problem is JwtProblem.KEYRING_ERROR


def test_fail_backend_is_unavailable(monkeypatch):
    _install_fake_keyring(monkeypatch, fail_backend=True)
    st = read_real_jwt_status(now=NOW)
    assert not st.ok and st.problem is JwtProblem.KEYRING_UNAVAILABLE


def test_explain_never_leaks_the_token(monkeypatch):
    secret = "SUPER-SECRET-TOKEN-VALUE"
    _install_fake_keyring(monkeypatch, value=_blob(NOW - 10, jwt=secret))
    st = read_real_jwt_status(now=NOW)
    assert secret not in st.explain() and secret not in st.detail


def test_malformed_detail_never_leaks_raw_value(monkeypatch):
    secret = "SUPER-SECRET-NOT-JSON"
    _install_fake_keyring(monkeypatch, value=secret)
    st = read_real_jwt_status(now=NOW)
    assert st.problem is JwtProblem.MALFORMED
    assert secret not in st.explain()


def test_every_problem_has_a_fix_text():
    # explain() must not KeyError for any problem value.
    for p in JwtProblem:
        _jwt.JwtStatus(False, p, "d").explain()
