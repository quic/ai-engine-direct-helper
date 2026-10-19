# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - global event stream, login gate OFF (EVT-01).

``GET /api/events`` (SSE) and ``WS /api/ws/events`` (what the Web UI prefers) are ONE notification channel: both are
driven by ``events.subscribe("*")`` and send the same payload objects. Events used here are produced by cheap,
deterministic actions: a permission request / decision (``security.permission_*``), a conversation being created
(``chat.created_conversation``) and a policy change (``security.policy_changed``).

NOT covered (deliberately):

* **Events from long-running work** - download / run / conversion lifecycle, scheduled-task notifications, ``reboot``.
  They need a model, an LLM or a network download. Per the route's own comment, per-frame data (chat tokens, run
  frames, download progress) is NOT on this bus at all, so there is nothing to assert about it here.
* **The bounded drop-oldest queue (256).** Filling it needs a slow consumer; not reproducible without a stalled socket.
* **A stalled / half-open client.** Disconnects tested here are clean closes.

Isolation (verified): everything is in-memory pub/sub inside the throw-away backend; the permission requests and
conversations a test creates are withdrawn / deleted in teardown. Streams are read on a daemon thread with its own client
that is closed at the end of each test. No keyring, no files outside the data dir.

Observed behaviour worth pinning:

* An event is delivered to every connected client; a client that connects later sees no history.
* The two transports carry identical JSON objects (SSE adds the ``event:`` name line; the WS frame has no name, the routing
  key is the payload's ``type``).
* An idle WebSocket gets ``{"type": "__heartbeat__"}`` after 30 s; an idle SSE stream gets ``: heartbeat`` (not tested
  for SSE: it needs a 30 s wait and the WS test already pins the timing).
* ``event_type`` and the payload ``type`` are NOT always the same string: ``security.permission_requested`` is sent with
  ``"type": "permission_request"`` in the body.
* The login-gate behaviour is in ``test_events_auth_gate.py``.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from websockets.sync.client import connect

from tests.e2e import conftest as c
from tests.e2e.conftest import Backend

pytestmark = pytest.mark.e2e

SEC = "/api/security"
WS_URL = f"ws://{c.E2E_HOST}:{c.E2E_PORT}/api/ws/events"


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


class Sse:
    """One SSE connection read on a daemon thread. ``frames`` holds complete frames (blank-line terminated)."""

    def __init__(self, base_url: str, path: str = "/api/events") -> None:
        self.frames: list[str] = []
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self._ready = threading.Event()
        self._client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(10.0, read=None))
        self._thread = threading.Thread(target=self._run, args=(path,), daemon=True)
        self._thread.start()
        assert self._ready.wait(10), "the SSE stream never answered"

    def _run(self, path: str) -> None:
        try:
            with self._client.stream("GET", path) as r:
                self.status, self.headers = r.status_code, dict(r.headers)
                self._ready.set()
                buf = ""
                for chunk in r.iter_text():
                    buf += chunk
                    while "\n\n" in buf:
                        frame, buf = buf.split("\n\n", 1)
                        self.frames.append(frame)
        except Exception:  # noqa: BLE001 - closing the client ends the read
            self._ready.set()

    def events(self) -> list[tuple[str, dict]]:
        out = []
        for f in list(self.frames):
            lines = dict(l.split(": ", 1) for l in f.splitlines() if ": " in l and not l.startswith(":"))
            if "event" in lines and "data" in lines:
                out.append((lines["event"], json.loads(lines["data"])))
        return out

    def wait_for(self, name: str, count: int = 1, timeout: float = 6.0) -> list[dict]:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = [d for n, d in self.events() if n == name]
            if len(got) >= count:
                return got
            time.sleep(0.05)
        raise AssertionError(f"no {count}x {name!r} within {timeout}s; got {[n for n, _ in self.events()]}")

    def close(self) -> None:
        self._client.close()


@pytest.fixture
def sse(backend: Backend) -> Iterator[Callable[[], Sse]]:
    made: list[Sse] = []

    def _open() -> Sse:
        s = Sse(backend.base_url)
        made.append(s)
        time.sleep(0.3)  # let the server finish subscribing before the test acts
        return s

    yield _open
    for s in made:
        s.close()


def recv_all(ws, seconds: float) -> list[str]:
    out, end = [], time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            out.append(ws.recv(timeout=0.3))
        except TimeoutError:
            continue
    return out


@pytest.fixture
def pending(api: httpx.Client) -> Iterator[Callable[[str], str]]:
    """Create permission requests; withdraw the ones still pending afterwards."""
    made: list[str] = []

    def _make(path: str | None = None, subject: str | None = None) -> str:
        r = api.post(f"{SEC}/permission/request", json={
            "subject": {"kind": "user", "identifier": subject or _uid()},
            "resource": {"kind": "path", "identifier": path or rf"C:\e2e-ev\{_uid()}\f.txt"},
            "requested_mask": {"read": True}})
        assert r.status_code == 201, r.text
        made.append(r.json()["request"]["request_id"])
        return made[-1]

    yield _make
    for rid in made:
        api.delete(f"{SEC}/permission/{rid}")


# --------------------------------------------------------------------------
# SSE
# --------------------------------------------------------------------------


def test_sse_handshake_headers(sse: Callable[[], Sse]) -> None:
    s = sse()
    assert s.status == 200
    assert s.headers["content-type"].startswith("text/event-stream")
    assert s.headers["cache-control"] == "no-cache" and s.headers["x-accel-buffering"] == "no"
    assert s.headers["connection"] == "keep-alive"
    assert s.frames == [], "nothing is sent until something happens"


def test_a_permission_request_arrives_as_a_named_event_with_a_legacy_body(sse: Callable[[], Sse], pending: Callable[..., str]) -> None:
    s = sse()
    subject, path = _uid(), rf"C:\e2e-ev\{_uid()}\a.txt"
    rid = pending(path, subject)
    (ev,) = s.wait_for("security.permission_requested")
    assert ev == {
        "type": "permission_request", "id": rid, "op": "read", "path": path, "caller": subject, "channel": "web",
        "session_id": "", "timestamp": ev["timestamp"], "is_native_subprocess": False,
    }
    assert ev["timestamp"].endswith("+00:00")


def test_each_decision_has_its_own_event_type(api: httpx.Client, sse: Callable[[], Sse], pending: Callable[..., str]) -> None:
    s = sse()
    subj = _uid()
    a, r, w = pending(subject=subj), pending(subject=subj), pending(subject=subj)
    api.post(f"{SEC}/permission/{a}/approve", json={"grant": "once"})
    api.post(f"{SEC}/permission/{r}/reject", json={"reason": "no"})
    api.delete(f"{SEC}/permission/{w}")
    (appr,) = s.wait_for("security.permission_approved")
    (rej,) = s.wait_for("security.permission_rejected")
    (can,) = s.wait_for("security.permission_request_cancelled")
    assert appr["request_id"] == {"value": a} and appr["granted_mask"] == {"read": True, "write": False, "execute": False, "delete": False}
    assert rej["request_id"] == {"value": r} and rej["reason"] == "no"
    assert can["request_id"] == {"value": w}
    for ev, t in ((appr, "security.permission_approved"), (rej, "security.permission_rejected"), (can, "security.permission_request_cancelled")):
        assert ev["type"] == t and ev["subject"] == {"kind": "user", "identifier": subj}


def test_events_come_in_the_order_they_happened(api: httpx.Client, sse: Callable[[], Sse], pending: Callable[..., str]) -> None:
    s = sse()
    subj = _uid()
    ids = []
    for _ in range(4):
        rid = pending(subject=subj)
        ids.append(rid)
        api.post(f"{SEC}/permission/{rid}/reject", json={})
    s.wait_for("security.permission_rejected", 4)
    names = [n for n, d in s.events() if (d.get("id") or d.get("request_id", {}).get("value") if isinstance(d.get("request_id"), dict) else d.get("id")) in ids]
    assert names == ["security.permission_requested", "security.permission_rejected"] * 4


def test_a_failed_action_publishes_nothing(api: httpx.Client, sse: Callable[[], Sse], pending: Callable[..., str]) -> None:
    s = sse()
    rid = pending()
    s.wait_for("security.permission_requested")
    n = len(s.events())
    assert api.post(f"{SEC}/permission/{rid}/approve", json={"reason": "x" * 3000}).status_code == 400
    assert api.post(f"{SEC}/permission/nope-{_uid()}/approve", json={}).status_code == 404
    time.sleep(0.6)
    assert len(s.events()) == n, "a rejected decision must not announce itself"


def test_a_conversation_and_a_policy_change_are_announced(api: httpx.Client, sse: Callable[[], Sse]) -> None:
    s = sse()
    cid = api.post("/api/chat/conversations", json={"title": "e2e-ev"}).json()["id"]
    try:
        (conv,) = s.wait_for("chat.created_conversation")
        assert conv["title"] == "e2e-ev" and conv["conversation_id"] == {"value": cid} and conv["type"] == "chat.created_conversation"
    finally:
        api.delete(f"/api/chat/conversations/{cid}")
    before = api.get(f"{SEC}/policy").json()["version"]
    api.put(f"{SEC}/policy", json={"rules": []})
    (pol,) = s.wait_for("security.policy_changed")
    assert pol["new_version"] == before + 1 and pol["old_version"] == before and pol["requires_reboot"] is False


def test_every_client_gets_every_event_and_a_late_joiner_gets_no_history(sse: Callable[[], Sse], pending: Callable[..., str]) -> None:
    first = sse()
    pending()
    first.wait_for("security.permission_requested")
    late = sse()
    time.sleep(0.5)
    assert late.events() == [], "no replay for a client that connects after the event"
    pending()
    first.wait_for("security.permission_requested", 2)
    late.wait_for("security.permission_requested", 1)
    assert [d["id"] for n, d in late.events()] == [d["id"] for n, d in first.events()][-1:]


def test_a_closed_client_does_not_disturb_the_others_or_the_backend(api: httpx.Client, sse: Callable[[], Sse], pending: Callable[..., str], backend: Backend) -> None:
    keep, drop = sse(), sse()
    drop.close()
    time.sleep(0.4)
    for _ in range(3):
        pending()
    keep.wait_for("security.permission_requested", 3)
    assert api.get("/api/system/health").status_code == 200
    errors = [l for l in backend.log_path.read_text(encoding="utf-8", errors="replace").splitlines() if "unexpected_error" in l or "Traceback" in l]
    assert errors == []


def test_many_simultaneous_clients_each_get_the_event(sse: Callable[[], Sse], pending: Callable[..., str]) -> None:
    clients = [sse() for _ in range(6)]
    rid = pending()
    for s in clients:
        assert [d["id"] for d in s.wait_for("security.permission_requested")] == [rid]


@pytest.mark.parametrize(
    ("mask", "op"),
    [
        ({"read": True}, "read"),
        ({"write": True}, "write"),
        ({"execute": True}, "exec"),
        ({"read": True, "write": True}, "write"),
        ({"write": True, "execute": True}, "write"),
        ({"read": True, "execute": True}, "exec"),
        ({"delete": True}, "read"),
    ],
)
def test_the_op_in_a_permission_frame_follows_write_then_exec_then_read(
    api: httpx.Client, sse: Callable[[], Sse], pending: Callable[..., str], mask: dict, op: str
) -> None:
    """``op`` is derived from the mask with write > exec > read; a delete-only mask has no name of its own and shows as read."""
    s = sse()
    rid = _uid()
    r = api.post(f"{SEC}/permission/request", json={
        "subject": {"kind": "user", "identifier": rid}, "resource": {"kind": "path", "identifier": rf"C:\e2e-ev\{rid}.txt"},
        "requested_mask": mask})
    assert r.status_code == 201
    try:
        (ev,) = s.wait_for("security.permission_requested")
        assert ev["op"] == op
    finally:
        api.delete(f"{SEC}/permission/{r.json()['request']['request_id']}")


def test_non_ascii_text_is_sent_literally_not_escaped(sse: Callable[[], Sse], pending: Callable[..., str]) -> None:
    s = sse()
    path = r"C:\e2e-ev\中文目录\文件 ✓.txt"
    pending(path, "e2e-用户")
    (ev,) = s.wait_for("security.permission_requested")
    assert ev["path"] == path and ev["caller"] == "e2e-用户"
    raw = next(f for f in s.frames if "permission_requested" in f)
    assert "中文目录" in raw and "\\u4e2d" not in raw, "SSE uses ensure_ascii=False"


def test_every_event_timestamp_is_a_full_iso_8601_utc_time(api: httpx.Client, sse: Callable[[], Sse], pending: Callable[..., str]) -> None:
    from datetime import datetime, timedelta, timezone

    s = sse()
    rid = pending()
    api.post(f"{SEC}/permission/{rid}/approve", json={"grant": "once"})
    (req,) = s.wait_for("security.permission_requested")
    (appr,) = s.wait_for("security.permission_approved")
    now = datetime.now(timezone.utc)
    for stamp in (req["timestamp"], appr["occurred_at"]):
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
        assert abs((now - parsed).total_seconds()) < 60 and "T" in stamp and len(stamp) > 19, f"not a full timestamp: {stamp}"
    assert datetime.fromisoformat(appr["occurred_at"]) >= datetime.fromisoformat(req["timestamp"])

# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------


def test_ws_connects_without_a_subprotocol_and_sends_nothing_until_something_happens() -> None:
    with connect(WS_URL, open_timeout=5) as ws:
        assert ws.subprotocol is None
        assert recv_all(ws, 0.8) == []


def test_ws_carries_the_same_payload_as_sse_without_the_event_name(api: httpx.Client, sse: Callable[[], Sse], pending: Callable[..., str]) -> None:
    s = sse()
    with connect(WS_URL, open_timeout=5) as ws:
        time.sleep(0.3)
        subj = _uid()
        a, r = pending(subject=subj), pending(subject=subj)
        api.post(f"{SEC}/permission/{r}/reject", json={"reason": "no"})
        frames = [json.loads(m) for m in recv_all(ws, 1.5)]
    assert [f["type"] for f in frames] == ["permission_request", "permission_request", "security.permission_rejected"]
    s.wait_for("security.permission_rejected")
    sse_payloads = [d for n, d in s.events() if d.get("id") in (a, r) or d.get("request_id") == {"value": r}]
    assert frames == sse_payloads, "byte-for-byte the same objects on both transports"


def test_ws_receives_events_of_every_context_not_only_security(api: httpx.Client) -> None:
    with connect(WS_URL, open_timeout=5) as ws:
        time.sleep(0.3)
        cid = api.post("/api/chat/conversations", json={"title": "e2e-ws-chat"}).json()["id"]
        try:
            frames = [json.loads(m) for m in recv_all(ws, 1.2)]
        finally:
            api.delete(f"/api/chat/conversations/{cid}")
    assert [f["type"] for f in frames] == ["chat.created_conversation"] and frames[0]["title"] == "e2e-ws-chat"

def test_ws_frames_are_compact_json_but_sse_data_lines_use_the_default_separators(sse: Callable[[], Sse], pending: Callable[..., str]) -> None:
    s = sse()
    with connect(WS_URL, open_timeout=5) as ws:
        time.sleep(0.3)
        pending()
        text = recv_all(ws, 1.0)[0]
    s.wait_for("security.permission_requested")
    assert '": ' not in text and '","' in text, "WS frame: no spaces after ':' or ','"
    raw = next(f for f in s.frames if "permission_requested" in f)
    assert '": "' in raw, "SSE frame: json.dumps default separators"


def test_ws_idle_connection_gets_a_json_heartbeat_after_30_seconds() -> None:
    with connect(WS_URL, open_timeout=5) as ws:
        t0 = time.monotonic()
        msg = ws.recv(timeout=40)
        waited = time.monotonic() - t0
    assert json.loads(msg) == {"type": "__heartbeat__"}
    assert 28 <= waited <= 36, f"heartbeat after {waited:.1f}s"


def test_ws_clients_that_disconnect_do_not_break_the_others(api: httpx.Client, pending: Callable[..., str], backend: Backend) -> None:
    conns = [connect(WS_URL, open_timeout=5) for _ in range(8)]
    time.sleep(0.3)
    try:
        pending()
        assert [len(recv_all(w, 0.8)) for w in conns] == [1] * 8
        for w in conns[:4]:
            w.close()
        time.sleep(0.5)
        pending()
        assert [len(recv_all(w, 0.8)) for w in conns[4:]] == [1] * 4
    finally:
        for w in conns:
            w.close()
    assert api.get("/api/system/health").status_code == 200
    log = backend.log_path.read_text(encoding="utf-8", errors="replace")
    assert not [l for l in log.splitlines() if "unexpected_error" in l or "Traceback" in l]


def test_ws_path_is_not_a_plain_http_route_and_unknown_ws_paths_are_refused(api: httpx.Client) -> None:
    assert api.get("/api/ws/events").status_code == 404
    with pytest.raises(Exception) as exc:
        connect(f"ws://{c.E2E_HOST}:{c.E2E_PORT}/api/ws/nope", open_timeout=4)
    assert "403" in str(exc.value)
