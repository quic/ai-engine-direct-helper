# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - permission request / approval flow (SECU-03).

Endpoints under ``/api/security`` (``interfaces/http/routes/security/_permission.py``):
``POST /permission/request``, ``GET /permission/pending``, ``POST /permission/{id}/approve``,
``POST /permission/{id}/reject``, ``DELETE /permission/{id}`` (withdraw) and
``POST /permission/cancel`` (bulk, operator side). ``POST /permission/check`` and the grants CRUD are
covered by ``test_secu_paths.py``; here ``check`` is used only to observe the EFFECT of a decision.

NOT covered (deliberately):

* **The live ASK path** - a tool call blocking on a request until the operator answers (the
  ``PermissionWaitRegistry`` future being woken ALLOW/DENY), the ``ASK_CONVERSATION_REGISTRY``
  hand-off that scopes a ``session`` grant to a conversation, and the native FileGuard bridge. They
  need a running tool/agent (LLM) or a native hook, and the native guard is a security switch this
  suite does not turn. Consequently ``session`` and ``process`` grants are only checked as stored
  rows, not as scopes that later match; ``POST /permission/cancel`` with an in-flight waiter (the only
  case where it returns a non-empty list) is untested.
* **Smart approval** (``RequestPermissionUseCase`` auto-approve / auto-reject via an LLM classifier).
* **The pending-store rehydrate after a restart.**

Isolation (verified): requests, grants and audit rows are rows in the throw-away
``<data_dir>/db/qai.db``; a grant is a database row only (it changes no OS ACL). Subjects are
``e2e-`` prefixed and every grant a test creates is revoked in teardown; requests have no HTTP delete,
so a test that leaves one PENDING withdraws it. Nothing touches the keyring.

Observed behaviour worth pinning (it differs from what one might assume):

* ``requested_mask`` with no bit set is a **500** on ``/permission/request`` AND ``/permission/check``
  (the domain ``ValueError`` is not mapped to a 4xx), although every other bad field is a clean 400.
* Only ``permanent`` makes a later ``/permission/check`` allow the same file. ``once``, an unknown
  scope and the empty string persist NOTHING; ``session``/``process`` persist a row bound to a
  conversation id / boot id which a bare ``check`` does not satisfy.
* ``GET /path-grants`` does not expose ``effect``, ``scope_kind`` or ``is_directory``: a remembered
  REJECTION (deny) looks identical to an allow grant on the wire.
* A bulk cancel of a PENDING request that has no live waiter returns ``{"cancelled": []}`` and leaves it
  PENDING; the pending list is newest-first.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime

import httpx
import pytest

from tests.e2e.conftest import Backend

pytestmark = pytest.mark.e2e

SEC = "/api/security"
BASE = r"C:\e2e-probe\deep\dir"


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


@pytest.fixture
def api(http: httpx.Client) -> httpx.Client:
    http.get("/api/system/uptime")
    http.headers["X-QAI-CSRF"] = http.cookies.get("qai_csrf")
    return http


def _uid() -> str:
    return "e2e-" + uuid.uuid4().hex[:8]


def _subject(identifier: str | None = None) -> dict:
    return {"kind": "user", "identifier": identifier or _uid()}


def _path(base: str = BASE) -> str:
    return rf"{base}\{_uid()}.txt"


@pytest.fixture
def tracker(api: httpx.Client) -> Iterator[Callable[[dict], dict]]:
    """Remember subjects whose grants must be revoked, and requests that must be withdrawn."""
    subjects: list[dict] = []
    requests: list[str] = []

    def _track(subject: dict) -> dict:
        subjects.append(subject)
        return subject

    _track.requests = requests  # type: ignore[attr-defined]
    yield _track
    for s in subjects:
        r = api.get(f"{SEC}/path-grants", params={"subject_kind": s["kind"], "subject_identifier": s["identifier"]})
        for g in r.json().get("grants", []):
            api.delete(f"{SEC}/path-grants/{g['grant_id']}")
    for rid in requests:
        api.delete(f"{SEC}/permission/{rid}")  # 412 when already resolved - fine


def _request(api: httpx.Client, tracker: Callable, subject: dict, resource: dict, mask: dict, *, keep: bool = False) -> str:
    r = api.post(f"{SEC}/permission/request", json={"subject": subject, "resource": resource, "requested_mask": mask})
    assert r.status_code == 201, r.text
    rid = r.json()["request"]["request_id"]
    tracker.requests.append(rid)  # type: ignore[attr-defined]
    return rid


def _req_path(api: httpx.Client, tracker: Callable, subject: dict, path: str, mask: dict | None = None) -> str:
    return _request(api, tracker, subject, {"kind": "path", "identifier": path}, mask or {"read": True})


def _check(api: httpx.Client, subject: dict, kind: str, ident: str, mask: dict) -> tuple[str, bool]:
    r = api.post(f"{SEC}/permission/check", json={"subject": subject, "resource": {"kind": kind, "identifier": ident}, "requested_mask": mask})
    assert r.status_code == 200, r.text
    j = r.json()
    return j["decision"], bool(j["matched_grant_id"])


def _grants(api: httpx.Client, subject: dict) -> list[dict]:
    r = api.get(f"{SEC}/path-grants", params={"subject_kind": subject["kind"], "subject_identifier": subject["identifier"]})
    assert r.status_code == 200, r.text
    return r.json()["grants"]


def _pending_ids(api: httpx.Client) -> list[str]:
    r = api.get(f"{SEC}/permission/pending")
    assert r.status_code == 200, r.text
    return [x["request_id"] for x in r.json()["requests"]]


@pytest.fixture
def sql(backend: Backend) -> Callable[..., list[tuple]]:
    path = str(backend.data_dir / "db" / "qai.db")

    def _run(q: str, args: tuple = ()) -> list[tuple]:
        con = sqlite3.connect(path, timeout=15)
        try:
            return con.execute(q, args).fetchall()
        finally:
            con.close()

    return _run


def _error(r: httpx.Response, status: int, code: str) -> None:
    assert r.status_code == status, r.text
    assert r.json()["code"] == code


# --------------------------------------------------------------------------
# request
# --------------------------------------------------------------------------


def test_request_creates_a_pending_item_and_lists_it(api: httpx.Client, tracker: Callable) -> None:
    s = _subject()
    p = _path()
    r = api.post(f"{SEC}/permission/request", json={"subject": s, "resource": {"kind": "path", "identifier": p}, "requested_mask": {"read": True}})
    assert r.status_code == 201
    req = r.json()["request"]
    tracker.requests.append(req["request_id"])  # type: ignore[attr-defined]
    assert len(req["request_id"]) == 26, "ULID"
    assert req["subject"] == s and req["resource"] == {"kind": "path", "identifier": p}
    assert req["requested_mask"] == {"read": True, "write": False, "execute": False, "delete": False}
    assert req["state"] == "pending" and req["resolved_at"] is None and req["resolution_reason"] == ""
    assert req["is_native_subprocess"] is False
    datetime.fromisoformat(req["created_at"])
    listed = [x for x in api.get(f"{SEC}/permission/pending").json()["requests"] if x["request_id"] == req["request_id"]]
    assert listed == [req], "the list entry is byte-identical to the creation response"


def test_request_is_persisted(api: httpx.Client, tracker: Callable, sql: Callable[..., list[tuple]]) -> None:
    rid = _req_path(api, tracker, _subject(), _path())
    assert sql("SELECT state FROM security_permission_request WHERE id = ?", (rid,)) == [("pending",)]


@pytest.mark.parametrize("kind", ["path", "skill", "network", "exec", "dep"])
def test_request_accepts_every_resource_kind(api: httpx.Client, tracker: Callable, kind: str) -> None:
    rid = _request(api, tracker, _subject(), {"kind": kind, "identifier": f"{kind}-target"}, {"read": True})
    assert rid in _pending_ids(api)


@pytest.mark.parametrize("kind", ["user", "preset", "system"])
def test_request_accepts_every_subject_kind(api: httpx.Client, tracker: Callable, kind: str) -> None:
    rid = _request(api, tracker, {"kind": kind, "identifier": _uid()}, {"kind": "path", "identifier": _path()}, {"read": True})
    assert rid in _pending_ids(api)


def test_request_flags_the_native_file_guard_subject(api: httpx.Client, tracker: Callable) -> None:
    rid = _request(api, tracker, {"kind": "system", "identifier": "native.file_guard"}, {"kind": "path", "identifier": _path()}, {"read": True})
    mine = [x for x in api.get(f"{SEC}/permission/pending").json()["requests"] if x["request_id"] == rid][0]
    assert mine["is_native_subprocess"] is True
    other = _request(api, tracker, {"kind": "system", "identifier": "something.else"}, {"kind": "path", "identifier": _path()}, {"read": True})
    assert [x for x in api.get(f"{SEC}/permission/pending").json()["requests"] if x["request_id"] == other][0]["is_native_subprocess"] is False


@pytest.mark.parametrize("mask", [{}, {"read": False}, {"read": False, "write": False, "execute": False, "delete": False}])
def test_request_with_no_permission_bit_is_a_500(api: httpx.Client, mask: dict) -> None:
    """KNOWN GAP (current behaviour, pinned): a client error reported as ``internal.unexpected``. If the
    domain ``ValueError`` gets mapped to a 4xx this becomes 400 - update this test then."""
    r = api.post(f"{SEC}/permission/request", json={"subject": _subject(), "resource": {"kind": "path", "identifier": _path()}, "requested_mask": mask})
    _error(r, 500, "internal.unexpected")


def test_check_with_no_permission_bit_is_also_a_500(api: httpx.Client) -> None:
    """KNOWN GAP (current behaviour, pinned): same defect on the sibling endpoint."""
    r = api.post(f"{SEC}/permission/check", json={"subject": _subject(), "resource": {"kind": "path", "identifier": "x"}, "requested_mask": {}})
    _error(r, 500, "internal.unexpected")


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"subject": {"kind": "robot", "identifier": "x"}}, "body.subject.kind"),
        ({"resource": {"kind": "weird", "identifier": "x"}}, "body.resource.kind"),
        ({"subject": {"kind": "user", "identifier": ""}}, "body.subject.identifier"),
        ({"subject": {"kind": "user", "identifier": "x" * 513}}, "body.subject.identifier"),
        ({"resource": {"kind": "path", "identifier": ""}}, "body.resource.identifier"),
        ({"resource": {"kind": "path", "identifier": "x" * 4097}}, "body.resource.identifier"),
        ({"requested_mask": {"read": "maybe"}}, "body.requested_mask.read"),
    ],
)
def test_request_validation(api: httpx.Client, payload: dict, field: str) -> None:
    body = {"subject": _subject(), "resource": {"kind": "path", "identifier": "x"}, "requested_mask": {"read": True}, **payload}
    r = api.post(f"{SEC}/permission/request", json=body)
    assert r.status_code == 400 and field in r.json()["details"]["field_errors"]


def test_request_requires_every_part(api: httpx.Client) -> None:
    for missing in ("subject", "resource", "requested_mask"):
        body = {"subject": _subject(), "resource": {"kind": "path", "identifier": "x"}, "requested_mask": {"read": True}}
        del body[missing]
        r = api.post(f"{SEC}/permission/request", json=body)
        assert r.status_code == 400 and f"body.{missing}" in r.json()["details"]["field_errors"]


def test_request_boundary_lengths_are_accepted(api: httpx.Client, tracker: Callable) -> None:
    rid = _request(api, tracker, {"kind": "user", "identifier": "e" * 512}, {"kind": "path", "identifier": "C:\\" + "p" * 4093}, {"read": True})
    assert rid in _pending_ids(api)


def test_pending_is_newest_first_and_excludes_resolved(api: httpx.Client, tracker: Callable) -> None:
    s = _subject()
    ids = [_req_path(api, tracker, s, _path()) for _ in range(4)]
    mine = [i for i in _pending_ids(api) if i in ids]
    assert mine == list(reversed(ids))
    assert api.post(f"{SEC}/permission/{ids[0]}/approve", json={}).status_code == 200
    assert api.delete(f"{SEC}/permission/{ids[1]}").status_code == 200
    assert [i for i in _pending_ids(api) if i in ids] == [ids[3], ids[2]]


# --------------------------------------------------------------------------
# approve: state machine
# --------------------------------------------------------------------------


def test_approve_resolves_the_request(api: httpx.Client, tracker: Callable) -> None:
    s = _subject()
    rid = _req_path(api, tracker, s, _path())
    r = api.post(f"{SEC}/permission/{rid}/approve", json={"reason": "ok", "decided_by": {"kind": "user", "identifier": "e2e-admin"}})
    assert r.status_code == 200
    req = r.json()["request"]
    assert req["request_id"] == rid and req["state"] == "approved" and req["resolution_reason"] == "ok"
    assert datetime.fromisoformat(req["resolved_at"]) >= datetime.fromisoformat(req["created_at"])
    assert rid not in _pending_ids(api)


def test_approve_accepts_an_empty_body_object(api: httpx.Client, tracker: Callable) -> None:
    rid = _req_path(api, tracker, _subject(), _path())
    r = api.post(f"{SEC}/permission/{rid}/approve", json={})
    assert r.status_code == 200 and r.json()["request"]["resolution_reason"] == ""


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_a_resolved_request_cannot_be_resolved_again(api: httpx.Client, tracker: Callable, action: str) -> None:
    rid = _req_path(api, tracker, _subject(), _path())
    assert api.post(f"{SEC}/permission/{rid}/approve", json={}).status_code == 200
    r = api.post(f"{SEC}/permission/{rid}/{action}", json={})
    _error(r, 412, "security.permission_request.already_resolved")
    assert f"cannot {action}" in r.json()["message"] and "approved" in r.json()["message"]
    _error(api.delete(f"{SEC}/permission/{rid}"), 412, "security.permission_request.already_resolved")


def test_the_first_decision_wins(api: httpx.Client, tracker: Callable) -> None:
    rid = _req_path(api, tracker, _subject(), _path())
    assert api.post(f"{SEC}/permission/{rid}/reject", json={"reason": "no"}).status_code == 200
    _error(api.post(f"{SEC}/permission/{rid}/approve", json={}), 412, "security.permission_request.already_resolved")
    assert api.get(f"{SEC}/permission/pending").status_code == 200 and rid not in _pending_ids(api)


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_unknown_request_is_404(api: httpx.Client, verb: str) -> None:
    _error(api.post(f"{SEC}/permission/nope-{_uid()}/{verb}", json={}), 404, "security.permission_request.not_found")


def test_cancel_unknown_request_is_404(api: httpx.Client) -> None:
    _error(api.delete(f"{SEC}/permission/nope-{_uid()}"), 404, "security.permission_request.not_found")


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_blank_request_id_is_400(api: httpx.Client, verb: str) -> None:
    r = api.post(f"{SEC}/permission/%20/{verb}", json={})
    assert r.status_code == 400 and r.json()["code"] == "validation.string.empty"


@pytest.mark.parametrize(
    "body",
    [{"grant": 5}, {"reason": None}, {"decided_by": {"kind": "x", "identifier": "y"}}, {"grant_range": ["file"]}],
)
def test_approve_rejects_malformed_bodies_and_leaves_the_request_pending(api: httpx.Client, tracker: Callable, body: dict) -> None:
    rid = _req_path(api, tracker, _subject(), _path())
    r = api.post(f"{SEC}/permission/{rid}/approve", json=body)
    assert r.status_code == 400 and r.json()["code"] == "request.validation"
    assert rid in _pending_ids(api)


def test_approve_requires_a_json_body(api: httpx.Client, tracker: Callable) -> None:
    rid = _req_path(api, tracker, _subject(), _path())
    r = api.post(f"{SEC}/permission/{rid}/approve")
    assert r.status_code == 400 and "body" in r.json()["details"]["field_errors"]
    assert rid in _pending_ids(api)


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_reason_is_limited_to_2048_characters_and_a_failure_keeps_the_request_pending(
    api: httpx.Client, tracker: Callable, verb: str
) -> None:
    rid = _req_path(api, tracker, _subject(), _path())
    _error(api.post(f"{SEC}/permission/{rid}/{verb}", json={"reason": "x" * 2049}), 400, "validation.string.max_length")
    assert rid in _pending_ids(api), "a rejected decision must not half-apply"
    assert api.post(f"{SEC}/permission/{rid}/{verb}", json={"reason": "x" * 2048}).status_code == 200


def test_reason_rejects_control_characters_and_keeps_unicode(api: httpx.Client, tracker: Callable) -> None:
    rid = _req_path(api, tracker, _subject(), _path())
    _error(api.post(f"{SEC}/permission/{rid}/reject", json={"reason": "bad\x00ctrl"}), 400, "validation.string.control_char")
    assert rid in _pending_ids(api)
    r = api.post(f"{SEC}/permission/{rid}/approve", json={"reason": "中文 — ok ✓"})
    assert r.status_code == 200 and r.json()["request"]["resolution_reason"] == "中文 — ok ✓"


# --------------------------------------------------------------------------
# approve: what each scope leaves behind
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["once", "bogus", ""])
def test_once_and_unknown_scopes_persist_no_grant_and_do_not_allow_later(api: httpx.Client, tracker: Callable, scope: str) -> None:
    s = tracker(_subject())
    p = _path()
    rid = _req_path(api, tracker, s, p)
    assert api.post(f"{SEC}/permission/{rid}/approve", json={"grant": scope}).status_code == 200
    assert _grants(api, s) == []
    assert _check(api, s, "path", p, {"read": True}) == ("deny", False), "the approval was single-use"


def test_permanent_scope_allows_the_same_file_afterwards(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    p = _path()
    rid = _req_path(api, tracker, s, p)
    assert api.post(f"{SEC}/permission/{rid}/approve", json={"grant": "permanent"}).status_code == 200
    (g,) = _grants(api, s)
    assert g["path"] == p and g["source"] == "user" and g["expires_at"] is None
    assert g["mask"] == {"read": True, "write": False, "execute": False, "delete": False}
    assert _check(api, s, "path", p, {"read": True}) == ("allow", True)


def test_scope_value_is_case_insensitive(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    p = _path()
    rid = _req_path(api, tracker, s, p)
    assert api.post(f"{SEC}/permission/{rid}/approve", json={"grant": " PERMANENT ", "grant_range": " FILE "}).status_code == 200
    assert _check(api, s, "path", p, {"read": True}) == ("allow", True)


@pytest.mark.parametrize(("scope", "hours", "key_is_boot_id"), [("session", 12.0, False), ("process", 8.0, True)])
def test_session_and_process_scopes_store_an_expiring_scoped_row(
    api: httpx.Client, tracker: Callable, sql: Callable[..., list[tuple]], scope: str, hours: float, key_is_boot_id: bool
) -> None:
    """The row is bound to a conversation id (session) or this backend's boot id (process), so a bare
    ``/permission/check`` - which carries neither - does not match it. The matching itself needs the live
    ASK path and is not covered (see the module docstring)."""
    s = tracker(_subject())
    p = _path()
    rid = _req_path(api, tracker, s, p)
    assert api.post(f"{SEC}/permission/{rid}/approve", json={"grant": scope}).status_code == 200
    (g,) = _grants(api, s)
    ttl = (datetime.fromisoformat(g["expires_at"]) - datetime.fromisoformat(g["created_at"])).total_seconds() / 3600
    assert ttl == pytest.approx(hours, abs=0.01)
    ((effect, kind, key),) = sql("SELECT effect, scope_kind, scope_key FROM security_path_grant WHERE subject_identifier = ?", (s["identifier"],))
    assert (effect, kind) == ("allow", scope)
    assert (len(key) == 26) is key_is_boot_id and (key == "") is (not key_is_boot_id), key
    assert _check(api, s, "path", p, {"read": True}) == ("deny", False)


def test_the_process_scope_key_is_the_same_boot_id_for_every_grant(api: httpx.Client, tracker: Callable, sql: Callable[..., list[tuple]]) -> None:
    keys = set()
    for _ in range(2):
        s = tracker(_subject())
        rid = _req_path(api, tracker, s, _path())
        api.post(f"{SEC}/permission/{rid}/approve", json={"grant": "process"})
        keys.add(sql("SELECT scope_key FROM security_path_grant WHERE subject_identifier = ?", (s["identifier"],))[0][0])
    assert len(keys) == 1 and len(next(iter(keys))) == 26


def test_the_grant_covers_only_the_approved_mask(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    p = _path()
    rid = _req_path(api, tracker, s, p, {"read": True})
    api.post(f"{SEC}/permission/{rid}/approve", json={"grant": "permanent"})
    assert _check(api, s, "path", p, {"read": True}) == ("allow", True)
    assert _check(api, s, "path", p, {"write": True}) == ("deny", False)
    assert _check(api, s, "path", p, {"read": True, "write": True}) == ("deny", False)


def test_the_grant_belongs_to_the_requesting_subject(api: httpx.Client, tracker: Callable) -> None:
    s1, s2 = tracker(_subject()), tracker(_subject())
    p = _path()
    rid = _req_path(api, tracker, s1, p)
    api.post(f"{SEC}/permission/{rid}/approve", json={"grant": "permanent"})
    assert _check(api, s2, "path", p, {"read": True}) == ("deny", False)


def test_a_multi_bit_request_is_granted_as_a_whole(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    p = _path()
    rid = _req_path(api, tracker, s, p, {"read": True, "write": True})
    api.post(f"{SEC}/permission/{rid}/approve", json={"grant": "permanent"})
    assert _check(api, s, "path", p, {"read": True, "write": True}) == ("allow", True)
    assert _check(api, s, "path", p, {"read": True}) == ("allow", True)


# --------------------------------------------------------------------------
# approve: grant range
# --------------------------------------------------------------------------


def test_directory_range_authorises_the_parent_folder_and_its_subtree(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    d = rf"C:\e2e-probe\dd\ee\{_uid()}"
    rid = _req_path(api, tracker, s, rf"{d}\one.txt")
    api.post(f"{SEC}/permission/{rid}/approve", json={"grant": "permanent", "grant_range": "directory"})
    assert [g["path"] for g in _grants(api, s)] == [d]
    assert _check(api, s, "path", rf"{d}\two.txt", {"read": True}) == ("allow", True)
    assert _check(api, s, "path", rf"{d}\sub\deeper\three.txt", {"read": True}) == ("allow", True)
    assert _check(api, s, "path", r"C:\e2e-probe\dd\ee\other\x.txt", {"read": True}) == ("deny", False)
    assert _check(api, s, "path", d + r"-evil\x.txt", {"read": True}) == ("deny", False), "a look-alike sibling is not inside"


def test_directory_range_on_a_shallow_path_falls_back_to_the_single_file(api: httpx.Client, tracker: Callable) -> None:
    """The parent must have at least two components; ``C:\\e2e-shallow`` has one, so it must not be
    authorised as a whole folder."""
    s = tracker(_subject())
    rid = _req_path(api, tracker, s, r"C:\e2e-shallow\f.txt")
    api.post(f"{SEC}/permission/{rid}/approve", json={"grant": "permanent", "grant_range": "directory"})
    assert [g["path"] for g in _grants(api, s)] == [r"C:\e2e-shallow\f.txt"]
    assert _check(api, s, "path", r"C:\e2e-shallow\other.txt", {"read": True}) == ("deny", False)


def test_directory_range_value_is_trimmed_and_case_insensitive(api: httpx.Client, tracker: Callable) -> None:
    """A padded / upper-case range must still widen to the folder (not silently degrade to one file)."""
    s = tracker(_subject())
    d = rf"C:\e2e-probe\dd\ee\{_uid()}"
    rid = _req_path(api, tracker, s, rf"{d}\one.txt")
    api.post(f"{SEC}/permission/{rid}/approve", json={"grant": "permanent", "grant_range": " DIRECTORY "})
    assert [g["path"] for g in _grants(api, s)] == [d]


def test_directory_range_needs_a_parent_with_two_path_components(api: httpx.Client, tracker: Callable) -> None:
    """Boundary of the depth rule: ``C:\\a\\b`` (two components) is the smallest folder that may be authorised whole."""
    s = tracker(_subject())
    parent = rf"C:\e2e-{uuid.uuid4().hex[:6]}\inner"
    rid = _req_path(api, tracker, s, rf"{parent}\f.txt")
    api.post(f"{SEC}/permission/{rid}/approve", json={"grant": "permanent", "grant_range": "directory"})
    assert [g["path"] for g in _grants(api, s)] == [parent]
    assert _check(api, s, "path", rf"{parent}\sibling.txt", {"read": True}) == ("allow", True)


def test_directory_range_without_a_persisting_scope_grants_nothing(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    rid = _req_path(api, tracker, s, rf"C:\e2e-probe\dd\ee\{_uid()}\one.txt")
    api.post(f"{SEC}/permission/{rid}/approve", json={"grant": "once", "grant_range": "directory"})
    assert _grants(api, s) == []


def test_program_range_authorises_every_command_of_the_same_binary(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    rid = _request(api, tracker, s, {"kind": "exec", "identifier": r'"C:\Windows\System32\WindowsPowerShell\v1.0\PowerShell.exe" -c x'}, {"execute": True})
    api.post(f"{SEC}/permission/{rid}/approve", json={"grant": "permanent", "grant_range": "program"})
    assert [g["path"] for g in _grants(api, s)] == ["powershell"], "the binary token is stored, normalised"
    assert _check(api, s, "exec", "powershell Get-Date", {"execute": True}) == ("allow", True)
    assert _check(api, s, "exec", "POWERSHELL.EXE -x", {"execute": True}) == ("allow", True)
    assert _check(api, s, "exec", "cmd /c dir", {"execute": True}) == ("deny", False)


def test_program_range_on_a_path_resource_is_ignored(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    p = _path()
    rid = _req_path(api, tracker, s, p)
    api.post(f"{SEC}/permission/{rid}/approve", json={"grant": "permanent", "grant_range": "program"})
    assert [g["path"] for g in _grants(api, s)] == [p]


def test_exec_resource_ignores_the_directory_range(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    cmd = r"C:\tools\sub\dir\prog.exe --flag"
    rid = _request(api, tracker, s, {"kind": "exec", "identifier": cmd}, {"execute": True})
    api.post(f"{SEC}/permission/{rid}/approve", json={"grant": "permanent", "grant_range": "directory"})
    assert [g["path"] for g in _grants(api, s)] == [cmd]


# --------------------------------------------------------------------------
# reject
# --------------------------------------------------------------------------


def test_reject_resolves_the_request(api: httpx.Client, tracker: Callable) -> None:
    rid = _req_path(api, tracker, _subject(), _path())
    r = api.post(f"{SEC}/permission/{rid}/reject", json={"reason": "no"})
    assert r.status_code == 200
    req = r.json()["request"]
    assert req["state"] == "rejected" and req["resolution_reason"] == "no" and req["resolved_at"] is not None
    assert rid not in _pending_ids(api)


def test_a_plain_reject_leaves_no_trace_in_the_grants(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    p = _path()
    rid = _req_path(api, tracker, s, p)
    api.post(f"{SEC}/permission/{rid}/reject", json={})
    assert _grants(api, s) == []
    assert _check(api, s, "path", p, {"read": True}) == ("deny", False)


def test_remembered_rejection_is_stored_as_a_deny_grant(api: httpx.Client, tracker: Callable, sql: Callable[..., list[tuple]]) -> None:
    s = tracker(_subject())
    p = _path()
    rid = _req_path(api, tracker, s, p)
    api.post(f"{SEC}/permission/{rid}/reject", json={"grant": "permanent"})
    ((effect, kind),) = sql("SELECT effect, scope_kind FROM security_path_grant WHERE subject_identifier = ?", (s["identifier"],))
    assert (effect, kind) == ("deny", "permanent")
    assert _check(api, s, "path", p, {"read": True}) == ("deny", True), "denied BY the grant, not by the default"


def test_a_remembered_deny_overrides_an_allow_grant_for_the_same_path(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    p = _path()
    api.post(f"{SEC}/path-grants", json={"subject": s, "path": p, "mask": {"read": True}})
    assert _check(api, s, "path", p, {"read": True}) == ("allow", True)
    rid = _req_path(api, tracker, s, p)
    api.post(f"{SEC}/permission/{rid}/reject", json={"grant": "permanent"})
    assert _check(api, s, "path", p, {"read": True})[0] == "deny"


def test_session_rejection_is_stored_but_not_matched_by_a_bare_check(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    p = _path()
    rid = _req_path(api, tracker, s, p)
    api.post(f"{SEC}/permission/{rid}/reject", json={"grant": "session"})
    assert len(_grants(api, s)) == 1
    assert _check(api, s, "path", p, {"read": True}) == ("deny", False)


def test_remembered_rejection_scope_keys_follow_the_scope(api: httpx.Client, tracker: Callable, sql: Callable[..., list[tuple]]) -> None:
    """A remembered REJECTION is keyed the same way as an approval: ``process`` to this backend's boot id
    (26 chars), ``session`` to the conversation id (empty over plain HTTP)."""
    keys = {}
    for scope in ("session", "process"):
        s = tracker(_subject())
        rid = _req_path(api, tracker, s, _path())
        api.post(f"{SEC}/permission/{rid}/reject", json={"grant": scope})
        ((effect, kind, key),) = sql("SELECT effect, scope_kind, scope_key FROM security_path_grant WHERE subject_identifier = ?", (s["identifier"],))
        assert (effect, kind) == ("deny", scope)
        keys[scope] = key
    assert keys["session"] == "" and len(keys["process"]) == 26


@pytest.mark.parametrize(("scope", "hours"), [("session", 12.0), ("process", 8.0)])
def test_remembered_rejection_expires_like_an_approval(api: httpx.Client, tracker: Callable, scope: str, hours: float) -> None:
    s = tracker(_subject())
    rid = _req_path(api, tracker, s, _path())
    api.post(f"{SEC}/permission/{rid}/reject", json={"grant": scope})
    (g,) = _grants(api, s)
    ttl = (datetime.fromisoformat(g["expires_at"]) - datetime.fromisoformat(g["created_at"])).total_seconds() / 3600
    assert ttl == pytest.approx(hours, abs=0.01)


def test_a_remembered_rejection_can_cover_the_whole_program(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    rid = _request(api, tracker, s, {"kind": "exec", "identifier": r'"C:\Windows\System32\cmd.exe" /c dir'}, {"execute": True})
    api.post(f"{SEC}/permission/{rid}/reject", json={"grant": "permanent", "grant_range": "program"})
    assert [g["path"] for g in _grants(api, s)] == ["cmd"]
    assert _check(api, s, "exec", "CMD.EXE /c echo hi", {"execute": True}) == ("deny", True)


def test_a_rejection_range_that_does_not_fit_the_resource_kind_stays_exact(api: httpx.Client, tracker: Callable) -> None:
    """``program`` only means something for an exec command and ``directory`` only for a path: asked for the
    wrong kind, the remembered rejection must stay an EXACT match, not widen."""
    s1, s2 = tracker(_subject()), tracker(_subject())
    p = _path()
    r1 = _req_path(api, tracker, s1, p)
    api.post(f"{SEC}/permission/{r1}/reject", json={"grant": "permanent", "grant_range": "program"})
    assert [g["path"] for g in _grants(api, s1)] == [p]
    cmd = r"C:\tools\sub\dir\prog.exe --flag"
    r2 = _request(api, tracker, s2, {"kind": "exec", "identifier": cmd}, {"execute": True})
    api.post(f"{SEC}/permission/{r2}/reject", json={"grant": "permanent", "grant_range": "directory"})
    assert [g["path"] for g in _grants(api, s2)] == [cmd]


def test_a_remembered_rejection_can_cover_the_whole_directory(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    d = rf"C:\e2e-probe\dd\ee\{_uid()}"
    rid = _req_path(api, tracker, s, rf"{d}\one.txt")
    api.post(f"{SEC}/permission/{rid}/reject", json={"grant": "permanent", "grant_range": "directory"})
    assert [g["path"] for g in _grants(api, s)] == [d]
    assert _check(api, s, "path", rf"{d}\sub\other.txt", {"read": True}) == ("deny", True)


def test_a_remembered_deny_grant_looks_like_an_allow_grant_on_the_wire(api: httpx.Client, tracker: Callable) -> None:
    """KNOWN GAP (current behaviour, pinned): ``GET /path-grants`` has no ``effect`` / ``scope_kind`` /
    ``is_directory`` / ``is_program`` field, so a client cannot tell a stored rejection from an approval.
    If the wire shape gains those fields this fails - update it then."""
    s_allow, s_deny = tracker(_subject()), tracker(_subject())
    p = _path()
    a = _req_path(api, tracker, s_allow, p)
    api.post(f"{SEC}/permission/{a}/approve", json={"grant": "permanent"})
    d = _req_path(api, tracker, s_deny, p)
    api.post(f"{SEC}/permission/{d}/reject", json={"grant": "permanent"})
    (ga,), (gd,) = _grants(api, s_allow), _grants(api, s_deny)
    assert sorted(ga) == sorted(gd) == ["created_at", "expires_at", "grant_id", "mask", "path", "source", "subject"]
    assert ga["source"] == gd["source"] == "user"


# --------------------------------------------------------------------------
# cancel (withdraw)
# --------------------------------------------------------------------------


def test_cancel_withdraws_a_pending_request(api: httpx.Client, tracker: Callable) -> None:
    rid = _req_path(api, tracker, _subject(), _path())
    r = api.delete(f"{SEC}/permission/{rid}")
    assert r.status_code == 200
    req = r.json()["request"]
    assert req["state"] == "cancelled" and req["resolved_at"] is not None and req["resolution_reason"] == ""
    assert rid not in _pending_ids(api)


def test_cancel_accepts_an_attributed_body(api: httpx.Client, tracker: Callable) -> None:
    rid = _req_path(api, tracker, _subject(), _path())
    r = api.request("DELETE", f"{SEC}/permission/{rid}", json={"cancelled_by": {"kind": "user", "identifier": "e2e-admin"}})
    assert r.status_code == 200 and r.json()["request"]["state"] == "cancelled"


def test_cancel_twice_and_cancel_then_approve_are_412(api: httpx.Client, tracker: Callable) -> None:
    rid = _req_path(api, tracker, _subject(), _path())
    assert api.delete(f"{SEC}/permission/{rid}").status_code == 200
    r = api.delete(f"{SEC}/permission/{rid}")
    _error(r, 412, "security.permission_request.already_resolved")
    assert "cannot cancel" in r.json()["message"] and "cancelled" in r.json()["message"]
    _error(api.post(f"{SEC}/permission/{rid}/approve", json={}), 412, "security.permission_request.already_resolved")


def test_cancel_creates_no_grant(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    p = _path()
    rid = _req_path(api, tracker, s, p)
    api.delete(f"{SEC}/permission/{rid}")
    assert _grants(api, s) == [] and _check(api, s, "path", p, {"read": True}) == ("deny", False)


# --------------------------------------------------------------------------
# bulk cancel
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [{}, {"request_id": "x", "cancel_all": True}, {"pid": 0}, {"pid": -1}, {"cancel_all": False}, {"request_id": ""}, {"pid": 5, "request_id": "x"}],
)
def test_bulk_cancel_needs_exactly_one_selector(api: httpx.Client, body: dict) -> None:
    r = api.post(f"{SEC}/permission/cancel", json=body)
    assert r.status_code == 400 and r.json() == {"detail": "exactly one of request_id / pid / cancel_all must be set"}


def test_bulk_cancel_rejects_a_non_integer_pid(api: httpx.Client) -> None:
    r = api.post(f"{SEC}/permission/cancel", json={"pid": "abc"})
    assert r.status_code == 400 and "body.pid" in r.json()["details"]["field_errors"]


@pytest.mark.parametrize("body", [{"request_id": "nope-unknown"}, {"pid": 99999999}, {"cancel_all": True}])
def test_bulk_cancel_with_nothing_waiting_is_a_quiet_no_op(api: httpx.Client, body: dict) -> None:
    r = api.post(f"{SEC}/permission/cancel", json=body)
    assert r.status_code == 200 and r.json() == {"cancelled": []}


def test_bulk_cancel_does_not_resolve_a_request_that_has_no_live_waiter(api: httpx.Client, tracker: Callable) -> None:
    """It silences ASK popups; it is not the same as withdrawing a request. A request created over HTTP has
    no waiter, so nothing is woken and it stays PENDING (the withdraw endpoint is ``DELETE``)."""
    rid = _req_path(api, tracker, _subject(), _path())
    assert api.post(f"{SEC}/permission/cancel", json={"request_id": rid}).json() == {"cancelled": []}
    assert api.post(f"{SEC}/permission/cancel", json={"cancel_all": True}).json() == {"cancelled": []}
    assert rid in _pending_ids(api)


# --------------------------------------------------------------------------
# audit trail
# --------------------------------------------------------------------------


def _audit_for(api: httpx.Client, subject: dict) -> list[tuple[str, str, str]]:
    entries = api.get(f"{SEC}/audit/recent", params={"limit": 200}).json()["entries"]
    return [(e["note"], e["decision"], e["op"]) for e in entries if e["subject"]["identifier"] == subject["identifier"]]


def test_decisions_write_one_definitive_audit_row_each(api: httpx.Client, tracker: Callable) -> None:
    approved, rejected, cancelled = tracker(_subject()), tracker(_subject()), tracker(_subject())
    ra = _req_path(api, tracker, approved, _path(), {"read": True, "write": True})
    api.post(f"{SEC}/permission/{ra}/approve", json={"grant": "permanent"})
    rr = _request(api, tracker, rejected, {"kind": "exec", "identifier": "powershell -x"}, {"execute": True})
    api.post(f"{SEC}/permission/{rr}/reject", json={"reason": "no"})
    rc = _req_path(api, tracker, cancelled, _path(), {"delete": True})
    api.delete(f"{SEC}/permission/{rc}")
    assert ("approved scope=permanent", "allow", "path") in _audit_for(api, approved)
    assert _audit_for(api, rejected) == [("user_rejected", "deny", "exec")]
    assert _audit_for(api, cancelled) == [("user_cancelled", "deny", "path")]


def test_a_permanent_approval_also_audits_the_grant_creation(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    rid = _req_path(api, tracker, s, _path())
    api.post(f"{SEC}/permission/{rid}/approve", json={"grant": "permanent"})
    assert ("grant_created", "allow", "") in _audit_for(api, s)


def test_a_failed_decision_writes_no_audit_row(api: httpx.Client, tracker: Callable) -> None:
    s = tracker(_subject())
    rid = _req_path(api, tracker, s, _path())
    api.post(f"{SEC}/permission/{rid}/approve", json={"reason": "x" * 3000})
    api.post(f"{SEC}/permission/{rid}/approve", json={"grant": 5})
    assert _audit_for(api, s) == []
