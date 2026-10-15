# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pre-flight check for the REAL QAI Service JWT.

Policy (decided by the owner): E2E reads the real JWT from the OS keyring that
the user's normal SSO login populated. If it cannot be read, the test FAILS -
it is never skipped and never silently downgraded. The failure message must say
WHY, because "JWT unreadable" has several distinct causes with different fixes.

This module only READS the keyring. It never writes or deletes (logout is the
one operation that deletes the real JWT, and E2E never exercises it).

The storage layout mirrors ``interfaces.http.auth.qai_service_token``:
``service="qai.service.session"``, ``key="jwt"``, value is JSON
``{"jwt": <str>, "deadline": <epoch seconds>}``. We re-implement the read
instead of importing the product module so a refactor there cannot silently
turn this check into a no-op - the format is part of what we are verifying.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum

SECRET_SERVICE = "qai.service.session"
SECRET_KEY = "jwt"

#: Refuse a JWT that dies within this window; a test run could outlive it.
MIN_REMAINING_SECONDS = 15 * 60


class JwtProblem(str, Enum):
    KEYRING_UNAVAILABLE = "keyring_unavailable"
    KEYRING_ERROR = "keyring_error"
    NOT_FOUND = "not_found"
    MALFORMED = "malformed"
    EXPIRED = "expired"
    EXPIRING_SOON = "expiring_soon"


@dataclass(frozen=True)
class JwtStatus:
    ok: bool
    problem: JwtProblem | None
    detail: str
    remaining_seconds: float | None = None

    def explain(self) -> str:
        """Human-readable failure text (what happened + what to do)."""
        if self.ok:
            return self.detail
        fixes = {
            JwtProblem.KEYRING_UNAVAILABLE: (
                "No usable OS keyring backend in this Python environment. Run "
                "the E2E suite with the same Python/user that normally runs "
                "AppBuilder."
            ),
            JwtProblem.KEYRING_ERROR: (
                "The keyring raised an error while reading. On ARM64 Windows "
                "this can be flaky in a fresh process; retry once, and if it "
                "persists check Credential Manager."
            ),
            JwtProblem.NOT_FOUND: (
                "No JWT is stored. Sign in once through the normal AppBuilder "
                "UI (SSO) as the SAME Windows user, then re-run."
            ),
            JwtProblem.MALFORMED: (
                "The stored value is not the expected JSON "
                "{'jwt': ..., 'deadline': ...}. The product's storage format "
                "may have changed; update tests/e2e/_jwt.py."
            ),
            JwtProblem.EXPIRED: (
                "The stored JWT has expired (it is valid ~8h). Sign in again "
                "through the normal AppBuilder UI, then re-run."
            ),
            JwtProblem.EXPIRING_SOON: (
                f"The stored JWT expires in under "
                f"{MIN_REMAINING_SECONDS // 60} minutes and may die mid-run. "
                "Sign in again, then re-run."
            ),
        }
        assert self.problem is not None
        return f"Real QAI Service JWT unusable [{self.problem.value}]: {self.detail}. {fixes[self.problem]}"


def read_real_jwt_status(*, now: float | None = None) -> JwtStatus:
    """Inspect the stored JWT without ever returning (or logging) its value."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as _FailKeyring
        from keyring.errors import KeyringError
    except ImportError as exc:
        return JwtStatus(False, JwtProblem.KEYRING_UNAVAILABLE, f"keyring not importable: {exc}")

    try:
        backend = keyring.get_keyring()
    except Exception as exc:  # noqa: BLE001
        return JwtStatus(False, JwtProblem.KEYRING_UNAVAILABLE, f"cannot select backend: {exc}")
    if isinstance(backend, _FailKeyring):
        return JwtStatus(False, JwtProblem.KEYRING_UNAVAILABLE, "keyring resolved to the 'fail' backend")

    try:
        raw = keyring.get_password(SECRET_SERVICE, SECRET_KEY)
    except KeyringError as exc:
        return JwtStatus(False, JwtProblem.KEYRING_ERROR, f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - some backends raise non-KeyringError
        return JwtStatus(False, JwtProblem.KEYRING_ERROR, f"{type(exc).__name__}: {exc}")

    if raw is None:
        return JwtStatus(False, JwtProblem.NOT_FOUND, f"no entry {SECRET_SERVICE}/{SECRET_KEY}")

    try:
        blob = json.loads(raw)
        token = blob["jwt"]
        deadline = float(blob["deadline"])
        if not isinstance(token, str) or not token:
            raise ValueError("empty jwt")
    except (ValueError, KeyError, TypeError) as exc:
        # Do NOT include ``raw`` - it may contain the credential.
        return JwtStatus(False, JwtProblem.MALFORMED, f"{type(exc).__name__}: {exc}")

    remaining = deadline - (time.time() if now is None else now)
    if remaining <= 0:
        return JwtStatus(False, JwtProblem.EXPIRED, f"expired {-remaining / 3600:.1f}h ago", remaining)
    if remaining < MIN_REMAINING_SECONDS:
        return JwtStatus(
            False,
            JwtProblem.EXPIRING_SOON,
            f"{remaining / 60:.0f} min left",
            remaining,
        )
    return JwtStatus(True, None, f"valid, {remaining / 3600:.1f}h left", remaining)
