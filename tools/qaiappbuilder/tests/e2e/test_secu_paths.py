# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 — path allow-list: grants, patterns, permission check, audit (SECU-01).

Endpoints under ``/api/security``: ``path-grants`` (POST/GET/DELETE),
``path_patterns`` (GET/PUT), ``permission/check`` (POST), ``audit/recent`` (GET).
Shapes come from ``interfaces/http/routes/security/{_grants,_paths,_permission}.py``.

Safety / isolation (verified, not assumed): the runtime-state buckets persist to
``<data_dir>/config/forge_config.json`` and the grants/audit rows live in
``<data_dir>/db/qai.db``. The backend runs with a throw-away ``QAI_DATA__DATA_DIR``,
so none of this can touch the user's real configuration. A path grant is a
database row only - creating one does not change any OS ACL.

Subjects used here are ``e2e-`` prefixed and every grant a test creates is
revoked in teardown.

Behaviour worth knowing (observed against the real backend):

* An implicit default-deny for a path with no grant writes NO audit row (the
  "would-ask" outcome is deferred until a user answers), whereas an ALLOW does.
* Creating a grant writes a ``grant_created`` audit row; revoking one writes none.
* Validation errors are 400 (not FastAPI's 422); duplicate grants are 409;
  unknown grant ids are 404.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest

pytestmark = pytest.mark.e2e

SEC = "/api/security"


@pytest.fixture
def api(http: httpx.Client) -> httpx.Client:
    http.get("/api/system/uptime")  # server installs the qai_csrf cookie
    http.headers["X-QAI-CSRF"] = http.cookies.get("qai_csrf")
    return http


@pytest.fixture
def subject() -> dict:
    """A unique ``e2e-`` subject so grants/audit rows never collide between tests."""
    return {"kind": "user", "identifier": f"e2e-subject-{uuid.uuid4().hex[:8]}"}


@pytest.fixture
def make_grant(api: httpx.Client, subject: dict) -> Iterator[Callable[..., dict]]:
    """Create path grants for ``subject``; every one is revoked at teardown."""
    created: list[str] = []

    def _make(path: str, **mask) -> dict:
        r = api.post(
            f"{SEC}/path-grants",
            json={"subject": subject, "path": path, "mask": mask or {"read": True}, "source": "user"},
        )
        assert r.status_code == 201, r.text
        grant = r.json()["grant"]
        created.append(grant["grant_id"])
        return grant

    yield _make

    for gid in created:
        r = api.delete(f"{SEC}/path-grants/{gid}")
        assert r.status_code in (204, 404), f"cleanup of grant {gid} failed: {r.status_code}"


def _check(api: httpx.Client, subject: dict, path: str, **mask) -> dict:
    r = api.post(
        f"{SEC}/permission/check",
        json={
            "subject": subject,
            "resource": {"kind": "path", "identifier": path},
            "requested_mask": mask or {"read": True},
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _list_grants(api: httpx.Client, subject: dict) -> list[dict]:
    r = api.get(
        f"{SEC}/path-grants",
        params={"subject_kind": subject["kind"], "subject_identifier": subject["identifier"]},
    )
    assert r.status_code == 200, r.text
    return r.json()["grants"]


def _audit(api: httpx.Client, **params) -> dict:
    r = api.get(f"{SEC}/audit/recent", params={"limit": 100, **params})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# path grants: create / list / revoke
# ---------------------------------------------------------------------------


def test_secu01_create_grant_returns_the_stored_grant(make_grant, subject):
    g = make_grant("C:/e2e-secu/a.txt", read=True)
    assert g["grant_id"]
    assert g["subject"] == subject
    assert g["path"] == "C:/e2e-secu/a.txt"
    assert g["mask"] == {"read": True, "write": False, "execute": False, "delete": False}
    assert g["source"] == "user"
    assert g["expires_at"] is None


def test_secu01_grants_are_listed_per_subject(api, make_grant, subject):
    a = make_grant("C:/e2e-secu/a.txt")
    b = make_grant("C:/e2e-secu/b.txt")
    ids = {g["grant_id"] for g in _list_grants(api, subject)}
    assert {a["grant_id"], b["grant_id"]} <= ids


def test_secu01_listing_is_scoped_to_the_subject(api, make_grant):
    make_grant("C:/e2e-secu/mine.txt")
    stranger = {"kind": "user", "identifier": f"e2e-stranger-{uuid.uuid4().hex[:8]}"}
    assert _list_grants(api, stranger) == []


def test_secu01_revoke_removes_the_grant(api, make_grant, subject):
    g = make_grant("C:/e2e-secu/revoke.txt")
    d = api.delete(f"{SEC}/path-grants/{g['grant_id']}")
    assert d.status_code == 204 and d.content == b""
    assert g["grant_id"] not in {x["grant_id"] for x in _list_grants(api, subject)}


def test_secu01_revoke_unknown_or_repeated_is_404(api, make_grant):
    assert api.delete(f"{SEC}/path-grants/no-such-{uuid.uuid4().hex}").status_code == 404
    g = make_grant("C:/e2e-secu/twice.txt")
    assert api.delete(f"{SEC}/path-grants/{g['grant_id']}").status_code == 204
    assert api.delete(f"{SEC}/path-grants/{g['grant_id']}").status_code == 404


def test_secu01_duplicate_grant_for_same_path_is_409(api, make_grant, subject):
    g = make_grant("C:/e2e-secu/dup.txt")
    r = api.post(
        f"{SEC}/path-grants",
        json={"subject": subject, "path": g["path"], "mask": {"read": True}, "source": "user"},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "security.path_grant.conflict"


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"path": ""}, "request.validation"),
        ({"subject": {"kind": "bogus", "identifier": "x"}}, "request.validation"),
        ({"subject": {"kind": "user", "identifier": ""}}, "request.validation"),
        ({"source": "bogus"}, "request.validation"),
        ({"expires_at": "not-a-date"}, "security.invalid_datetime"),
        ({"expires_at": "2030-01-01T00:00:00"}, "security.naive_datetime"),
    ],
)
def test_secu01_create_grant_validation(api, subject, override, code):
    body = {"subject": subject, "path": "C:/e2e-secu/v.txt", "mask": {"read": True}, "source": "user"}
    body.update(override)
    r = api.post(f"{SEC}/path-grants", json=body)
    assert r.status_code == 400, r.text
    assert r.json()["code"] == code
    # A rejected request must not have created anything.
    assert _list_grants(api, subject) == []


def test_secu01_grant_expiry_round_trips(api, subject):
    r = api.post(
        f"{SEC}/path-grants",
        json={
            "subject": subject,
            "path": "C:/e2e-secu/exp.txt",
            "mask": {"read": True},
            "source": "user",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    )
    assert r.status_code == 201, r.text
    gid = r.json()["grant"]["grant_id"]
    try:
        assert r.json()["grant"]["expires_at"].startswith("2099-01-01T00:00:00")
    finally:
        api.delete(f"{SEC}/path-grants/{gid}")


# ---------------------------------------------------------------------------
# permission/check: the grant actually changes the decision
# ---------------------------------------------------------------------------


def test_secu01_no_grant_means_deny(api, subject):
    res = _check(api, subject, "C:/e2e-secu/none.txt", read=True)
    assert res["decision"] == "deny"
    assert res["matched_grant_id"] is None


def test_secu01_grant_allows_exactly_the_granted_access(api, make_grant, subject):
    g = make_grant("C:/e2e-secu/rw.txt", read=True)
    allowed = _check(api, subject, g["path"], read=True)
    assert allowed["decision"] == "allow"
    assert allowed["matched_grant_id"] == g["grant_id"]
    # Read was granted; write was not.
    assert _check(api, subject, g["path"], write=True)["decision"] == "deny"


def test_secu01_a_grant_does_not_leak_to_another_path_or_subject(api, make_grant, subject):
    g = make_grant("C:/e2e-secu/only-this.txt", read=True)
    assert _check(api, subject, "C:/e2e-secu/other.txt", read=True)["decision"] == "deny"
    stranger = {"kind": "user", "identifier": f"e2e-stranger-{uuid.uuid4().hex[:8]}"}
    assert _check(api, stranger, g["path"], read=True)["decision"] == "deny"


def test_secu01_revoking_a_grant_takes_effect_immediately(api, make_grant, subject):
    g = make_grant("C:/e2e-secu/live.txt", read=True)
    assert _check(api, subject, g["path"], read=True)["decision"] == "allow"
    assert api.delete(f"{SEC}/path-grants/{g['grant_id']}").status_code == 204
    assert _check(api, subject, g["path"], read=True)["decision"] == "deny"


def test_secu01_a_multi_permission_grant_allows_each_but_not_the_ungranted(
    api, make_grant, subject
):
    g = make_grant("C:/e2e-secu/multi.txt", read=True, write=True)
    assert _check(api, subject, g["path"], read=True)["decision"] == "allow"
    assert _check(api, subject, g["path"], write=True)["decision"] == "allow"
    assert _check(api, subject, g["path"], delete=True)["decision"] == "deny"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"subject": {"kind": "user", "identifier": "x"}},
        {
            "subject": {"kind": "user", "identifier": "x"},
            "resource": {"kind": "bogus", "identifier": "y"},
            "requested_mask": {"read": True},
        },
    ],
)
def test_secu01_check_rejects_malformed_requests(api, body):
    assert api.post(f"{SEC}/permission/check", json=body).status_code == 400


# ---------------------------------------------------------------------------
# audit trail
# ---------------------------------------------------------------------------


def test_secu01_an_allow_decision_is_audited(api, make_grant, subject):
    g = make_grant("C:/e2e-secu/audited.txt", read=True)
    res = _check(api, subject, g["path"], read=True)
    rows = {e["audit_id"]: e for e in _audit(api)["entries"]}
    assert res["audit_id"] in rows
    row = rows[res["audit_id"]]
    assert row["decision"] == "allow"
    assert row["subject"] == subject
    assert row["resource"] == {"kind": "path", "identifier": g["path"]}
    assert row["op"] == "read"


def test_secu01_creating_a_grant_is_audited(api, make_grant, subject):
    g = make_grant("C:/e2e-secu/created.txt")
    mine = [
        e
        for e in _audit(api)["entries"]
        if e["subject"] == subject and e["resource"]["identifier"] == g["path"]
    ]
    assert any(e["note"] == "grant_created" and e["decision"] == "allow" for e in mine)


def test_secu01_an_implicit_deny_writes_no_audit_row(api, subject):
    """A default-deny with no matching rule is a deferred 'ask'; no row is written
    until a user answers (P-17). Recorded so a change to this is noticed."""
    before = _audit(api)["total"]
    assert _check(api, subject, "C:/e2e-secu/unknown.txt", read=True)["decision"] == "deny"
    assert _audit(api)["total"] == before


def test_secu01_audit_is_newest_first_and_pages_do_not_overlap(api, make_grant, subject):
    g = make_grant("C:/e2e-secu/page.txt", read=True)
    for _ in range(4):
        _check(api, subject, g["path"], read=True)
    full = _audit(api)["entries"]
    stamps = [e["occurred_at"] for e in full]
    assert stamps == sorted(stamps, reverse=True)
    p1 = api.get(f"{SEC}/audit/recent", params={"limit": 2, "offset": 0}).json()
    p2 = api.get(f"{SEC}/audit/recent", params={"limit": 2, "offset": 2}).json()
    assert len(p1["entries"]) == 2 and p2["entries"]
    assert {e["audit_id"] for e in p1["entries"]}.isdisjoint({e["audit_id"] for e in p2["entries"]})
    assert p1["total"] == p2["total"] >= 5


def test_secu01_audit_time_window_filters(api, make_grant, subject):
    g = make_grant("C:/e2e-secu/window.txt", read=True)
    _check(api, subject, g["path"], read=True)
    assert _audit(api, since="2999-01-01T00:00:00+00:00")["total"] == 0
    assert _audit(api, until="2000-01-01T00:00:00+00:00")["total"] == 0
    assert _audit(api, since="2000-01-01T00:00:00+00:00")["total"] >= 1


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 501},
        {"offset": -1},
        {"since": "garbage"},
        {"since": "2030-01-01T00:00:00"},  # naive: no timezone
    ],
)
def test_secu01_audit_rejects_bad_parameters(api, params):
    assert api.get(f"{SEC}/audit/recent", params=params).status_code == 400


# ---------------------------------------------------------------------------
# path_patterns: whole-document read/write allow-lists (GET/PUT, no delete)
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_patterns(api: httpx.Client) -> Iterator[None]:
    """path_patterns has no DELETE; reset to the empty default afterwards."""
    yield
    api.put(
        f"{SEC}/path_patterns",
        json={
            "read_allow_patterns": {"enabled": False, "patterns": []},
            "write_allow_patterns": {"enabled": False, "patterns": []},
        },
    )


def test_secu01_path_patterns_default_is_disabled_and_empty(api, clean_patterns):
    body = api.get(f"{SEC}/path_patterns").json()
    assert body == {
        "read_allow_patterns": {"enabled": False, "patterns": []},
        "write_allow_patterns": {"enabled": False, "patterns": []},
    }


def test_secu01_path_patterns_round_trip(api, clean_patterns):
    put = api.put(
        f"{SEC}/path_patterns",
        json={"read_allow_patterns": {"enabled": True, "patterns": ["C:/e2e-r/**"]}},
    )
    assert put.status_code == 200
    got = api.get(f"{SEC}/path_patterns").json()
    assert got["read_allow_patterns"] == {"enabled": True, "patterns": ["C:/e2e-r/**"]}


def test_secu01_path_patterns_partial_update_preserves_the_sibling(api, clean_patterns):
    api.put(
        f"{SEC}/path_patterns",
        json={"read_allow_patterns": {"enabled": True, "patterns": ["C:/e2e-r/**"]}},
    )
    after = api.put(
        f"{SEC}/path_patterns",
        json={"write_allow_patterns": {"enabled": True, "patterns": ["C:/e2e-w/**"]}},
    ).json()
    assert after["read_allow_patterns"]["patterns"] == ["C:/e2e-r/**"]  # untouched
    assert after["write_allow_patterns"]["patterns"] == ["C:/e2e-w/**"]


def test_secu01_path_patterns_empty_body_changes_nothing(api, clean_patterns):
    api.put(
        f"{SEC}/path_patterns",
        json={"read_allow_patterns": {"enabled": True, "patterns": ["C:/e2e-r/**"]}},
    )
    after = api.put(f"{SEC}/path_patterns", json={}).json()
    assert after["read_allow_patterns"]["patterns"] == ["C:/e2e-r/**"]


def test_secu01_path_patterns_rejects_wrong_types(api, clean_patterns):
    r = api.put(
        f"{SEC}/path_patterns",
        json={"read_allow_patterns": {"enabled": "maybe", "patterns": "not-a-list"}},
    )
    assert r.status_code == 400
    # ...and left the stored document unchanged.
    assert api.get(f"{SEC}/path_patterns").json()["read_allow_patterns"]["patterns"] == []
