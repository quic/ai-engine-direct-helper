# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 — conversation CRUD over REST (FEATURE-INVENTORY CHAT-01).

Endpoints: ``/api/chat/conversations`` (create / list / get / rename / pin /
favorite / delete). Shapes and error mapping are taken from
``interfaces/http/routes/chat/_rest.py`` and ``interfaces/http/error_handlers.py``
(validation errors are 400, NOT FastAPI's default 422; missing ids are 404).

Data hygiene (project convention): every conversation a test creates has an
``e2e-`` prefixed title and is deleted in teardown, even when the test fails.
The backend uses a throw-away data dir, so nothing here can touch the user's
real conversations; the cleanup is still done so tests do not leak into each
other within the module.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest

pytestmark = pytest.mark.e2e

BASE = "/api/chat/conversations"
CSRF_COOKIE = "qai_csrf"
CSRF_HEADER = "X-QAI-CSRF"


@pytest.fixture
def api(http: httpx.Client) -> httpx.Client:
    """The shared client, with a CSRF token installed on every unsafe request."""
    http.get("/api/system/uptime")  # server sets the qai_csrf cookie
    token = http.cookies.get(CSRF_COOKIE)
    assert token
    http.headers[CSRF_HEADER] = token
    return http


@pytest.fixture
def make_conversation(api: httpx.Client) -> Iterator[Callable[..., dict]]:
    """Factory for ``e2e-``-prefixed conversations; all are deleted at teardown."""
    created: list[str] = []

    def _make(label: str = "conv") -> dict:
        title = f"e2e-{label}-{uuid.uuid4().hex[:8]}"
        r = api.post(BASE, json={"title": title})
        assert r.status_code == 201, r.text
        body = r.json()
        created.append(body["id"])
        return body

    yield _make

    for conv_id in created:
        # 204 if it still exists, 404 if the test already deleted it: both fine.
        r = api.delete(f"{BASE}/{conv_id}")
        assert r.status_code in (204, 404), f"cleanup of {conv_id} failed: {r.status_code}"


def _ids(api: httpx.Client, **params) -> list[str]:
    r = api.get(BASE, params={"limit": 500, **params})
    assert r.status_code == 200, r.text
    return [c["id"] for c in r.json()["items"]]


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_chat01_create_returns_a_new_empty_conversation(make_conversation):
    conv = make_conversation("create")
    assert conv["id"]
    assert conv["title"].startswith("e2e-create-")
    assert conv["message_count"] == 0
    assert conv["pinned"] is False
    assert conv["favorite"] is False
    assert conv["created_at"] and conv["updated_at"]


def test_chat01_created_conversations_have_distinct_ids(make_conversation):
    a, b = make_conversation("a"), make_conversation("b")
    assert a["id"] != b["id"]


@pytest.mark.parametrize(
    "body",
    [
        {},  # missing title
        {"title": ""},  # min_length=1
        {"title": "x" * 257},  # max_length=256
        {"title": 123},  # wrong type
    ],
)
def test_chat01_create_rejects_invalid_titles_with_400(api: httpx.Client, body: dict):
    r = api.post(BASE, json=body)
    assert r.status_code == 400, r.text
    assert r.status_code != 422  # the project rewrites FastAPI's 422 to 400


def test_chat01_create_accepts_a_title_at_the_length_limit(make_conversation, api):
    title = "e2e-" + "x" * 252  # exactly 256
    r = api.post(BASE, json={"title": title})
    assert r.status_code == 201, r.text
    api.delete(f"{BASE}/{r.json()['id']}")


def test_chat01_unicode_title_round_trips(api: httpx.Client, make_conversation):
    title = f"e2e-中文-ñ-🙂-{uuid.uuid4().hex[:6]}"
    r = api.post(BASE, json={"title": title})
    assert r.status_code == 201
    conv_id = r.json()["id"]
    try:
        assert api.get(f"{BASE}/{conv_id}").json()["title"] == title
    finally:
        api.delete(f"{BASE}/{conv_id}")


# ---------------------------------------------------------------------------
# read / list
# ---------------------------------------------------------------------------


def test_chat01_get_returns_what_was_created(api: httpx.Client, make_conversation):
    conv = make_conversation("get")
    got = api.get(f"{BASE}/{conv['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == conv["id"]
    assert got.json()["title"] == conv["title"]


def test_chat01_list_contains_created_conversations(api: httpx.Client, make_conversation):
    a, b = make_conversation("la"), make_conversation("lb")
    ids = _ids(api)
    assert a["id"] in ids and b["id"] in ids


def test_chat01_list_is_ordered_most_recently_updated_first(
    api: httpx.Client, make_conversation
):
    first = make_conversation("old")
    second = make_conversation("new")
    ids = _ids(api)
    assert ids.index(second["id"]) < ids.index(first["id"])


def test_chat01_list_respects_limit_and_offset(api: httpx.Client, make_conversation):
    for i in range(3):
        make_conversation(f"page{i}")
    page1 = api.get(BASE, params={"limit": 2, "offset": 0}).json()["items"]
    page2 = api.get(BASE, params={"limit": 2, "offset": 2}).json()["items"]
    assert len(page1) == 2
    assert page2, "offset should reach the remaining rows"
    assert {c["id"] for c in page1}.isdisjoint({c["id"] for c in page2})


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"offset": -1}])
def test_chat01_list_rejects_out_of_range_paging(api: httpx.Client, params: dict):
    assert api.get(BASE, params=params).status_code == 400


def test_chat01_get_unknown_id_is_404(api: httpx.Client):
    r = api.get(f"{BASE}/conv-does-not-exist-{uuid.uuid4().hex}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


def test_chat01_rename_changes_the_title_and_persists(api: httpx.Client, make_conversation):
    conv = make_conversation("rename")
    new_title = f"e2e-renamed-{uuid.uuid4().hex[:8]}"
    r = api.patch(f"{BASE}/{conv['id']}", json={"title": new_title})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == new_title
    assert api.get(f"{BASE}/{conv['id']}").json()["title"] == new_title


@pytest.mark.parametrize("title", ["", "x" * 257])
def test_chat01_rename_rejects_invalid_titles(
    api: httpx.Client, make_conversation, title: str
):
    conv = make_conversation("badrename")
    r = api.patch(f"{BASE}/{conv['id']}", json={"title": title})
    assert r.status_code == 400
    # The failed rename must not have changed anything.
    assert api.get(f"{BASE}/{conv['id']}").json()["title"] == conv["title"]


def test_chat01_rename_unknown_id_is_404(api: httpx.Client):
    r = api.patch(f"{BASE}/conv-does-not-exist-{uuid.uuid4().hex}", json={"title": "e2e-x"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# pin / favorite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("action", "field"), [("pin", "pinned"), ("favorite", "favorite")])
def test_chat01_pin_and_favorite_toggle_and_filter(
    api: httpx.Client, make_conversation, action: str, field: str
):
    flagged = make_conversation(f"{action}-on")
    plain = make_conversation(f"{action}-off")

    r = api.patch(f"{BASE}/{flagged['id']}/{action}", json={field: True})
    assert r.status_code == 200, r.text
    assert r.json()[field] is True

    query_flag = "pinned" if field == "pinned" else "favorite"
    only = _ids(api, **{query_flag: "true"})
    assert flagged["id"] in only
    assert plain["id"] not in only

    # Toggle back off -> drops out of the filtered view.
    assert api.patch(f"{BASE}/{flagged['id']}/{action}", json={field: False}).json()[field] is False
    assert flagged["id"] not in _ids(api, **{query_flag: "true"})


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_chat01_delete_removes_the_conversation(api: httpx.Client, make_conversation):
    conv = make_conversation("delete")
    d = api.delete(f"{BASE}/{conv['id']}")
    assert d.status_code == 204
    assert d.content == b""
    assert api.get(f"{BASE}/{conv['id']}").status_code == 404
    assert conv["id"] not in _ids(api)


def test_chat01_delete_unknown_id_is_404(api: httpx.Client):
    assert api.delete(f"{BASE}/conv-does-not-exist-{uuid.uuid4().hex}").status_code == 404


def test_chat01_delete_is_not_repeatable(api: httpx.Client, make_conversation):
    conv = make_conversation("twice")
    assert api.delete(f"{BASE}/{conv['id']}").status_code == 204
    assert api.delete(f"{BASE}/{conv['id']}").status_code == 404


def test_chat01_deleting_one_conversation_leaves_others_alone(
    api: httpx.Client, make_conversation
):
    keep, drop = make_conversation("keep"), make_conversation("drop")
    assert api.delete(f"{BASE}/{drop['id']}").status_code == 204
    assert api.get(f"{BASE}/{keep['id']}").status_code == 200
    assert drop["id"] not in _ids(api) and keep["id"] in _ids(api)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_chat01_search_by_title_finds_the_conversation(api: httpx.Client, make_conversation):
    token = uuid.uuid4().hex[:10]
    needle = make_conversation(f"srch{token}")
    other = make_conversation("unrelated")
    r = api.get(BASE, params={"query": token})
    assert r.status_code == 200, r.text
    ids = [c["id"] for c in r.json()["items"]]
    assert needle["id"] in ids
    assert other["id"] not in ids


def test_chat01_search_with_no_match_returns_an_empty_list(api: httpx.Client):
    r = api.get(BASE, params={"query": f"zz-no-such-title-{uuid.uuid4().hex}"})
    assert r.status_code == 200
    assert r.json()["items"] == []
