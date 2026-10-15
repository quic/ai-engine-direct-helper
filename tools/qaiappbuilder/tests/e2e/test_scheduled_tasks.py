# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 — scheduled tasks: list / get / edit / pause / resume / run / delete,
run history and the notification bell (FEATURE-INVENTORY TASK-01).

Endpoints: ``/api/scheduled-tasks`` (``interfaces/http/routes/scheduled_tasks.py``).

HOW TASKS ARE CREATED HERE - READ THIS FIRST
--------------------------------------------
There is NO HTTP route that creates a task. Creation is the agent-facing
``scheduled_task`` tool (needs an LLM to decide to call it), which is not
deterministic and is out of scope for L1. So these tests seed ``scheduling_job``
/ ``scheduling_task_run`` rows directly into the backend's THROW-AWAY database,
with exactly the columns ``SqliteScheduledTaskStore.save`` writes. Consequences,
stated plainly:

* The CREATE path (``ScheduledTaskToolHandler._create`` and ``parse_schedule``
  at creation time) is NOT covered here. ``parse_schedule`` IS covered through
  PATCH (it is the same parser), but creation-time defaults (deliver=global,
  model capture, repeat default) are not.
* Seeding bypasses the application layer, so a bug in how the app writes a row
  would not be seen. Reads/edits/deletes then go through the real HTTP API.

Safety: seeded tasks have ``next_run_at`` ~30 days out and never fire during a
test. The one operation that arms a task to fire (``/run``) only sets
``next_run_at`` to now; the scheduler tick is 60 s and the whole backend lives
for a few seconds, and the executor would only try to run a prompt against a
model - but to be safe every test deletes its tasks in teardown.

Data hygiene: ids are ``e2e-`` prefixed; nothing here touches the user's real
database (``QAI_DATA__DATA_DIR`` points at a temp dir).
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from tests.e2e.conftest import Backend

pytestmark = pytest.mark.e2e

B = "/api/scheduled-tasks"
FAR_FUTURE_DAYS = 30


# ---------------------------------------------------------------------------
# fixtures: client + direct-to-DB seeding
# ---------------------------------------------------------------------------


@pytest.fixture
def api(http: httpx.Client) -> httpx.Client:
    http.get("/api/system/uptime")
    http.headers["X-QAI-CSRF"] = http.cookies.get("qai_csrf")
    return http


def _connect(backend: Backend) -> sqlite3.Connection:
    # Short timeout: the backend holds write locks only briefly.
    return sqlite3.connect(str(backend.data_dir / "db" / "qai.db"), timeout=15)


@pytest.fixture
def seed_task(backend: Backend, api: httpx.Client) -> Iterator[Callable[..., str]]:
    """Insert a task row (like ``SqliteScheduledTaskStore.save`` would) and
    delete it through the API at teardown."""
    created: list[str] = []

    def _seed(**overrides) -> str:
        now = datetime.now(timezone.utc)
        row = {
            "id": f"e2e-task-{uuid.uuid4().hex[:8]}",
            "name": "e2e-task",
            "prompt": "e2e prompt",
            "schedule_kind": "interval",
            "schedule_display": "every 1d",
            "interval_seconds": 86400.0,
            "conversation_id": "",
            "tab_id": "",
            "completed_runs": 0,
            "enabled": 1,
            "state": "scheduled",
            "next_run_at": (now + timedelta(days=FAR_FUTURE_DAYS)).isoformat(),
            "enabled_tools": "[]",
            "enabled_skills": "[]",
            "created_at": now.isoformat(),
            "version": 0,
        }
        row.update(overrides)
        con = _connect(backend)
        try:
            con.execute(
                f"INSERT INTO scheduling_job ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                list(row.values()),
            )
            con.commit()
        finally:
            con.close()
        created.append(row["id"])
        return row["id"]

    yield _seed

    for task_id in created:
        r = api.delete(f"{B}/{task_id}")
        assert r.status_code in (200, 404), f"cleanup of {task_id} failed: {r.status_code}"


@pytest.fixture
def seed_run(backend: Backend) -> Iterator[Callable[..., str]]:
    """Insert a run-history row; removed from the DB at teardown (run rows are
    deliberately NOT deleted with their task - see the history tests)."""
    created: list[str] = []

    def _seed(task_id: str, *, ok: int = 1, text: str = "result", age: timedelta = timedelta(0), notified: bool = False) -> str:
        run_id = f"e2e-run-{uuid.uuid4().hex[:8]}"
        ran_at = datetime.now(timezone.utc) - age
        con = _connect(backend)
        try:
            con.execute(
                "INSERT INTO scheduling_task_run (id,task_id,conversation_id,ok,status,result_text,ran_at,notified_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (run_id, task_id, "", ok, "ok" if ok else "error", text, ran_at.isoformat(),
                 datetime.now(timezone.utc).isoformat() if notified else ""),
            )
            con.commit()
        finally:
            con.close()
        created.append(run_id)
        return run_id

    yield _seed

    con = _connect(backend)
    try:
        con.executemany("DELETE FROM scheduling_task_run WHERE id = ?", [(i,) for i in created])
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# read: list / get / catalog
# ---------------------------------------------------------------------------


def test_task01_empty_install_has_no_tasks_or_notifications(api):
    assert api.get(B).json() == {"tasks": [], "total": 0}
    assert api.get(f"{B}/notifications/unread").json() == {"notifications": [], "total": 0}


def test_task01_get_returns_the_seeded_task_with_every_documented_field(api, seed_task):
    task_id = seed_task(name="my task", prompt="do the thing")
    t = api.get(f"{B}/{task_id}").json()
    assert t["task_id"] == task_id
    assert t["name"] == "my task"
    assert t["prompt"] == "do the thing"
    assert t["schedule"] == "every 1d"
    assert t["state"] == "scheduled" and t["enabled"] is True
    assert t["is_global"] is True and t["conversation_id"] == ""
    assert t["repeat_times"] is None and t["completed_runs"] == 0
    assert t["last_run_at"] is None and t["last_status"] == "" and t["last_error"] == ""
    assert t["enabled_tools"] == [] and t["enabled_skills"] == []
    datetime.fromisoformat(t["next_run_at"])


def test_task01_list_includes_seeded_tasks_and_total_matches(api, seed_task):
    a, b = seed_task(), seed_task()
    body = api.get(B).json()
    ids = {t["task_id"] for t in body["tasks"]}
    assert {a, b} <= ids
    assert body["total"] == len(body["tasks"])


def test_task01_list_can_be_filtered_by_conversation(api, seed_task):
    bound = seed_task(conversation_id="conv-e2e-A", tab_id="tab-e2e-A")
    other = seed_task(conversation_id="conv-e2e-B", tab_id="tab-e2e-B")
    glob = seed_task()
    ids = {t["task_id"] for t in api.get(B, params={"conversation_id": "conv-e2e-A"}).json()["tasks"]}
    assert ids == {bound}
    assert other not in ids and glob not in ids


def test_task01_a_bound_task_is_not_global(api, seed_task):
    bound = seed_task(conversation_id="conv-e2e-A", tab_id="tab-e2e-A")
    t = api.get(f"{B}/{bound}").json()
    assert t["is_global"] is False and t["conversation_id"] == "conv-e2e-A"


def test_task01_an_unnamed_task_shows_its_id_as_the_display_name(api, seed_task):
    task_id = seed_task(name="")
    assert api.get(f"{B}/{task_id}").json()["name"] == task_id


def test_task01_get_unknown_task_is_404(api):
    assert api.get(f"{B}/e2e-no-such-task").status_code == 404


def test_task01_catalog_lists_tools_and_enabled_skills(api):
    cat = api.get(f"{B}/catalog").json()
    assert set(cat) == {"tools", "skills"}
    # The scheduler can schedule itself, and the basics are always offered.
    assert {"read", "exec", "scheduled_task"} <= set(cat["tools"])
    assert len(cat["tools"]) == len(set(cat["tools"])), "duplicate tool names"
    assert cat["skills"] == sorted(cat["skills"])


def test_task01_catalog_is_not_swallowed_by_the_task_id_route(api):
    """/catalog and /notifications/unread are declared before /{task_id}; if that
    order were ever swapped they would 404 as 'task catalog not found'."""
    assert api.get(f"{B}/catalog").status_code == 200
    assert api.get(f"{B}/notifications/unread").status_code == 200


# ---------------------------------------------------------------------------
# edit (PATCH)
# ---------------------------------------------------------------------------


def test_task01_patch_changes_only_the_fields_sent(api, seed_task):
    task_id = seed_task(name="before", prompt="p0")
    r = api.patch(f"{B}/{task_id}", json={"name": "after"})
    assert r.status_code == 200
    t = r.json()
    assert t["name"] == "after" and t["prompt"] == "p0"
    assert api.get(f"{B}/{task_id}").json()["name"] == "after"  # persisted


def test_task01_patch_prompt(api, seed_task):
    task_id = seed_task()
    assert api.patch(f"{B}/{task_id}", json={"prompt": "new prompt"}).json()["prompt"] == "new prompt"


def test_task01_patch_empty_prompt_is_rejected_and_nothing_changes(api, seed_task):
    task_id = seed_task(prompt="keep me")
    r = api.patch(f"{B}/{task_id}", json={"prompt": "   "})
    assert r.status_code == 400
    assert api.get(f"{B}/{task_id}").json()["prompt"] == "keep me"


def test_task01_patch_with_no_fields_is_rejected(api, seed_task):
    assert api.patch(f"{B}/{seed_task()}", json={}).status_code == 400


def test_task01_patch_unknown_task_is_404(api):
    assert api.patch(f"{B}/e2e-no-such", json={"name": "x"}).status_code == 404


def test_task01_patch_wrong_type_is_a_400_validation_error(api, seed_task):
    r = api.patch(f"{B}/{seed_task()}", json={"repeat_times": "many"})
    assert r.status_code == 400 and r.json()["code"] == "request.validation"


# -- repeat_times ------------------------------------------------------------


@pytest.mark.parametrize(("sent", "stored"), [(1, 1), (5, 5), (1_000_000_000, 1_000_000_000), (None, None)])
def test_task01_repeat_times_round_trip(api, seed_task, sent, stored):
    task_id = seed_task()
    assert api.patch(f"{B}/{task_id}", json={"repeat_times": sent}).json()["repeat_times"] == stored


@pytest.mark.parametrize("sent", [0, -1, -100])
def test_task01_non_positive_repeat_means_unbounded(api, seed_task, sent):
    """Documented: only a positive integer bounds a task; anything else is 'forever'."""
    task_id = seed_task(repeat_times=3)
    assert api.patch(f"{B}/{task_id}", json={"repeat_times": sent}).json()["repeat_times"] is None


def test_task01_omitting_repeat_leaves_it_unchanged_but_null_clears_it(api, seed_task):
    task_id = seed_task(repeat_times=4)
    assert api.patch(f"{B}/{task_id}", json={"name": "x"}).json()["repeat_times"] == 4
    assert api.patch(f"{B}/{task_id}", json={"repeat_times": None}).json()["repeat_times"] is None


# -- schedule ----------------------------------------------------------------

#: (text, expected state, whether next_run_at is set)
GOOD_SCHEDULES = [
    ("every 2h", "scheduled", True),
    ("every 30m", "scheduled", True),
    ("30m", "scheduled", True),
    ("2h", "scheduled", True),
    ("1d", "scheduled", True),
    ("daily 07:30+08:00", "scheduled", True),
    ("weekly mon 09:00+08:00", "scheduled", True),
    ("0 9 * * *", "scheduled", True),
    ("2099-01-01T09:00:00Z", "scheduled", True),
    ("EVERY 2H", "scheduled", True),  # case-insensitive
]


@pytest.mark.parametrize(("text", "state", "has_next"), GOOD_SCHEDULES)
def test_task01_patch_accepts_every_documented_schedule_form(api, seed_task, text, state, has_next):
    t = api.patch(f"{B}/{seed_task()}", json={"schedule": text}).json()
    assert t["schedule"] == text
    assert t["state"] == state
    assert (t["next_run_at"] is not None) is has_next


@pytest.mark.parametrize(
    "text", ["every 0m", "every", "daily 25:00", "weekly xyz 09:00", "0 9 * *", "   ", "whenever", "tomorrow"]
)
def test_task01_patch_rejects_bad_schedules_and_keeps_the_old_one(api, seed_task, text):
    task_id = seed_task()
    r = api.patch(f"{B}/{task_id}", json={"schedule": text})
    assert r.status_code == 400
    after = api.get(f"{B}/{task_id}").json()
    assert after["schedule"] == "every 1d" and after["state"] == "scheduled"


def test_task01_a_bad_schedule_error_lists_the_accepted_forms(api, seed_task):
    detail = api.patch(f"{B}/{seed_task()}", json={"schedule": "whenever"}).json()["detail"]
    for hint in ("every", "daily", "weekly", "cron", "ISO-8601"):
        assert hint in detail, f"error message no longer mentions {hint!r}"


def test_task01_a_one_shot_in_the_past_completes_immediately(api, seed_task):
    t = api.patch(f"{B}/{seed_task()}", json={"schedule": "2000-01-01T00:00:00Z"}).json()
    assert t["state"] == "completed" and t["next_run_at"] is None


def test_task01_daily_with_an_offset_is_converted_to_utc(api, seed_task):
    """'daily 07:30+08:00' is 23:30 UTC the previous day."""
    t = api.patch(f"{B}/{seed_task()}", json={"schedule": "daily 07:30+08:00"}).json()
    nxt = datetime.fromisoformat(t["next_run_at"])
    assert (nxt.hour, nxt.minute) == (23, 30)
    assert nxt.utcoffset() == timedelta(0)


def test_task01_bare_cron_is_interpreted_in_utc(api, seed_task):
    nxt = datetime.fromisoformat(api.patch(f"{B}/{seed_task()}", json={"schedule": "0 9 * * *"}).json()["next_run_at"])
    assert (nxt.hour, nxt.minute) == (9, 0)


def test_task01_every_2h_fires_about_two_hours_from_now(api, seed_task):
    before = datetime.now(timezone.utc)
    nxt = datetime.fromisoformat(api.patch(f"{B}/{seed_task()}", json={"schedule": "every 2h"}).json()["next_run_at"])
    assert timedelta(hours=1, minutes=55) < nxt - before < timedelta(hours=2, minutes=5)


# -- start_at ----------------------------------------------------------------


def test_task01_start_at_sets_the_first_fire_of_a_recurring_task(api, seed_task):
    t = api.patch(f"{B}/{seed_task()}", json={"start_at": "2099-01-01T09:00:00+08:00"}).json()
    assert t["next_run_at"].startswith("2099-01-01T01:00:00")  # 09:00+08:00 == 01:00Z
    assert t["start_at"].startswith("2099-01-01T01:00:00")
    assert t["schedule"] == "every 1d"  # the cadence itself is untouched


@pytest.mark.parametrize("bad", ["2099-01-01T09:00:00", "nope"])
def test_task01_start_at_must_be_an_iso_timestamp_with_an_offset(api, seed_task, bad):
    task_id = seed_task()
    before = api.get(f"{B}/{task_id}").json()["next_run_at"]
    r = api.patch(f"{B}/{task_id}", json={"start_at": bad})
    assert r.status_code == 400
    assert api.get(f"{B}/{task_id}").json()["next_run_at"] == before


@pytest.mark.parametrize("clear", ["", None, "   "])
def test_task01_blank_or_null_start_at_clears_a_previously_set_first_run(api, seed_task, clear):
    """Blank means 'not supplied' (``_coerce_start_at``), so an explicit blank/null
    CLEARS the first-run time and the next fire reverts to the normal cadence.
    (An earlier version of this suite wrongly expected 400 here.)"""
    task_id = seed_task()
    api.patch(f"{B}/{task_id}", json={"start_at": "2099-01-01T09:00:00+08:00"})
    assert api.get(f"{B}/{task_id}").json()["start_at"] is not None

    t = api.patch(f"{B}/{task_id}", json={"start_at": clear}).json()
    assert t["start_at"] is None
    # Back to the cadence ('every 1d' from now), not 2099.
    nxt = datetime.fromisoformat(t["next_run_at"])
    assert nxt < datetime.now(timezone.utc) + timedelta(days=2)


def test_task01_omitting_start_at_keeps_the_existing_first_run(api, seed_task):
    task_id = seed_task()
    first = api.patch(f"{B}/{task_id}", json={"start_at": "2099-01-01T09:00:00+08:00"}).json()["start_at"]
    t = api.patch(f"{B}/{task_id}", json={"name": "renamed"}).json()
    assert t["start_at"] == first and t["name"] == "renamed"


# -- whitelists --------------------------------------------------------------


def test_task01_tool_and_skill_whitelists_round_trip_and_can_be_cleared(api, seed_task):
    task_id = seed_task()
    t = api.patch(f"{B}/{task_id}", json={"enabled_tools": ["read", "exec"], "enabled_skills": ["weather"]}).json()
    assert t["enabled_tools"] == ["read", "exec"] and t["enabled_skills"] == ["weather"]
    t = api.patch(f"{B}/{task_id}", json={"enabled_tools": [], "enabled_skills": []}).json()
    assert t["enabled_tools"] == [] and t["enabled_skills"] == []


def test_task01_whitelist_entries_are_trimmed_and_blanks_dropped(api, seed_task):
    t = api.patch(f"{B}/{seed_task()}", json={"enabled_tools": ["  read ", "", "   ", "exec"]}).json()
    assert t["enabled_tools"] == ["read", "exec"]


def test_task01_every_tool_offered_by_the_catalog_is_accepted_as_a_whitelist(api, seed_task):
    tools = api.get(f"{B}/catalog").json()["tools"]
    t = api.patch(f"{B}/{seed_task()}", json={"enabled_tools": tools}).json()
    assert t["enabled_tools"] == tools


def test_task01_KNOWN_GAP_unknown_tool_names_are_accepted_in_the_whitelist(api, seed_task):
    """CHARACTERISATION - likely a product defect, reported rather than asserted correct.

    PATCH accepts ``enabled_tools=["no_such_tool"]`` with 200. At run time the runner
    disables every advertised tool NOT in the whitelist (``scheduled_task_runner.py``,
    ``disabled = TOOL_ORDER - enabled_tools``), so a whitelist containing only an
    unknown name silently disables EVERY real tool: the task would run but could do
    nothing. ``GET /catalog`` already publishes the valid names, so validating against
    it would be cheap.

    If this is fixed (400 on unknown names), flip the assertion to ``== 400``.
    """
    task_id = seed_task()
    r = api.patch(f"{B}/{task_id}", json={"enabled_tools": ["no_such_tool"]})
    assert r.status_code == 200
    assert api.get(f"{B}/{task_id}").json()["enabled_tools"] == ["no_such_tool"]


# ---------------------------------------------------------------------------
# pause / resume / run / delete
# ---------------------------------------------------------------------------


def test_task01_pause_disables_the_task(api, seed_task):
    task_id = seed_task()
    t = api.post(f"{B}/{task_id}/pause").json()
    assert t["state"] == "paused" and t["enabled"] is False
    assert api.get(f"{B}/{task_id}").json()["state"] == "paused"


def test_task01_resume_re_arms_the_task_and_recomputes_the_next_run(api, seed_task):
    task_id = seed_task(schedule_display="every 2h", interval_seconds=7200.0)
    api.post(f"{B}/{task_id}/pause")
    before = datetime.now(timezone.utc)
    t = api.post(f"{B}/{task_id}/resume").json()
    assert t["state"] == "scheduled" and t["enabled"] is True
    nxt = datetime.fromisoformat(t["next_run_at"])
    assert timedelta(hours=1, minutes=55) < nxt - before < timedelta(hours=2, minutes=5)


def test_task01_pause_and_resume_are_idempotent(api, seed_task):
    task_id = seed_task()
    assert api.post(f"{B}/{task_id}/pause").status_code == 200
    assert api.post(f"{B}/{task_id}/pause").json()["state"] == "paused"
    assert api.post(f"{B}/{task_id}/resume").status_code == 200
    assert api.post(f"{B}/{task_id}/resume").json()["state"] == "scheduled"


def test_task01_editing_a_paused_task_does_not_silently_resume_it(api, seed_task):
    """Documented: changing the schedule must not un-pause; resume is the explicit path."""
    task_id = seed_task()
    api.post(f"{B}/{task_id}/pause")
    t = api.patch(f"{B}/{task_id}", json={"schedule": "every 3h"}).json()
    assert t["state"] == "paused" and t["enabled"] is False
    assert t["schedule"] == "every 3h"


@pytest.mark.parametrize("action", ["pause", "resume", "run"])
def test_task01_actions_on_an_unknown_task_are_404(api, action):
    assert api.post(f"{B}/e2e-no-such/{action}").status_code == 404


def test_task01_run_queues_the_task_to_fire_on_the_next_tick(api, seed_task):
    task_id = seed_task()
    before = datetime.now(timezone.utc)
    assert api.post(f"{B}/{task_id}/run").json() == {"queued": task_id}
    nxt = datetime.fromisoformat(api.get(f"{B}/{task_id}").json()["next_run_at"])
    # It was ~30 days away; now it is due immediately.
    assert nxt <= datetime.now(timezone.utc) + timedelta(seconds=5)
    assert nxt >= before - timedelta(seconds=5)


def test_task01_KNOWN_QUIRK_run_on_a_paused_task_also_un_pauses_it(api, seed_task):
    """CHARACTERISATION - confirm this is intended.

    ``_run_now`` forces ``enabled=True`` / ``state=SCHEDULED`` (the code comment says
    this is so a paused task can still be run on demand). But nothing restores the
    paused state afterwards, so a manual 'run now' permanently RESUMES the task and it
    will keep firing on its schedule. A user who only wanted one off-schedule run of a
    paused task gets an active one instead.

    If 'run' is changed to preserve the pause, flip the assertions.
    """
    task_id = seed_task()
    api.post(f"{B}/{task_id}/pause")
    api.post(f"{B}/{task_id}/run")
    t = api.get(f"{B}/{task_id}").json()
    assert t["state"] == "scheduled" and t["enabled"] is True


def test_task01_delete_removes_the_task(api, seed_task):
    task_id = seed_task()
    d = api.delete(f"{B}/{task_id}")
    assert d.status_code == 200 and d.json() == {"removed": task_id}
    assert api.get(f"{B}/{task_id}").status_code == 404
    assert task_id not in {t["task_id"] for t in api.get(B).json()["tasks"]}


def test_task01_delete_is_not_repeatable_and_unknown_is_404(api, seed_task):
    task_id = seed_task()
    assert api.delete(f"{B}/{task_id}").status_code == 200
    assert api.delete(f"{B}/{task_id}").status_code == 404
    assert api.delete(f"{B}/e2e-no-such").status_code == 404


def test_task01_deleting_one_task_leaves_the_others(api, seed_task):
    keep, drop = seed_task(), seed_task()
    api.delete(f"{B}/{drop}")
    assert api.get(f"{B}/{keep}").status_code == 200


# ---------------------------------------------------------------------------
# run history
# ---------------------------------------------------------------------------


def test_task01_runs_are_listed_newest_first(api, seed_task, seed_run):
    task_id = seed_task()
    seed_run(task_id, text="oldest", age=timedelta(hours=5))
    seed_run(task_id, text="newest", age=timedelta(minutes=1))
    seed_run(task_id, text="middle", age=timedelta(hours=1))
    body = api.get(f"{B}/{task_id}/runs").json()
    assert [r["result_text"] for r in body["runs"]] == ["newest", "middle", "oldest"]
    assert body["total"] == 3


def test_task01_runs_report_status_and_the_full_result_text(api, seed_task, seed_run):
    task_id = seed_task()
    seed_run(task_id, ok=1, text="x" * 5000)
    seed_run(task_id, ok=0, text="boom", age=timedelta(minutes=5))
    runs = api.get(f"{B}/{task_id}/runs").json()["runs"]
    ok, bad = runs[0], runs[1]
    assert ok["ok"] is True and ok["status"] == "ok" and len(ok["result_text"]) == 5000  # not truncated
    assert bad["ok"] is False and bad["status"] == "error"
    datetime.fromisoformat(ok["ran_at"])


def test_task01_runs_limit(api, seed_task, seed_run):
    task_id = seed_task()
    for i in range(4):
        seed_run(task_id, age=timedelta(minutes=i))
    assert len(api.get(f"{B}/{task_id}/runs", params={"limit": 2}).json()["runs"]) == 2


@pytest.mark.parametrize("limit", [0, 201, -1])
def test_task01_runs_limit_is_bounded(api, limit):
    assert api.get(f"{B}/x/runs", params={"limit": limit}).status_code == 400


def test_task01_runs_of_an_unknown_task_are_an_empty_list_not_an_error(api):
    assert api.get(f"{B}/e2e-no-such/runs").json() == {"runs": [], "total": 0}


def test_task01_run_history_outlives_the_task(api, seed_task, seed_run):
    """By design (migration 073: 'a run record OUTLIVES its task so history survives
    deletion'): deleting a task keeps its run rows and they stay queryable by id."""
    task_id = seed_task()
    seed_run(task_id, text="keep me")
    assert api.delete(f"{B}/{task_id}").status_code == 200
    runs = api.get(f"{B}/{task_id}/runs").json()["runs"]
    assert [r["result_text"] for r in runs] == ["keep me"]


# ---------------------------------------------------------------------------
# notification bell
# ---------------------------------------------------------------------------


def test_task01_unread_lists_only_unread_runs_newest_first_with_a_task_name(api, seed_task, seed_run):
    task_id = seed_task(name="Nightly")
    new = seed_run(task_id, text="n", age=timedelta(minutes=1))
    old = seed_run(task_id, text="o", age=timedelta(hours=2))
    read = seed_run(task_id, text="r", age=timedelta(hours=3), notified=True)
    items = [n for n in api.get(f"{B}/notifications/unread").json()["notifications"] if n["task_id"] == task_id]
    assert [n["id"] for n in items] == [new, old]
    assert read not in {n["id"] for n in items}
    assert {n["task_name"] for n in items} == {"Nightly"}


def test_task01_unread_preview_is_capped_at_200_chars(api, seed_task, seed_run):
    task_id = seed_task()
    seed_run(task_id, text="N" * 500)
    n = next(n for n in api.get(f"{B}/notifications/unread").json()["notifications"] if n["task_id"] == task_id)
    assert len(n["result_preview"]) == 200


def test_task01_a_deleted_tasks_notification_falls_back_to_its_id(api, seed_task, seed_run):
    task_id = seed_task(name="Gone")
    seed_run(task_id)
    api.delete(f"{B}/{task_id}")
    n = next(n for n in api.get(f"{B}/notifications/unread").json()["notifications"] if n["task_id"] == task_id)
    assert n["task_name"] == task_id


def test_task01_mark_read_is_idempotent_and_never_404s(api, seed_task, seed_run):
    task_id = seed_task()
    run_id = seed_run(task_id)
    assert api.post(f"{B}/notifications/{run_id}/mark-read").json() == {"marked": 1}
    assert api.post(f"{B}/notifications/{run_id}/mark-read").json() == {"marked": 0}
    assert api.post(f"{B}/notifications/e2e-no-such-run/mark-read").json() == {"marked": 0}
    assert run_id not in {n["id"] for n in api.get(f"{B}/notifications/unread").json()["notifications"]}


def test_task01_mark_all_read_clears_everything_and_reports_the_count(api, seed_task, seed_run):
    task_id = seed_task()
    for i in range(3):
        seed_run(task_id, age=timedelta(minutes=i))
    seed_run(task_id, notified=True, age=timedelta(hours=9))
    assert api.post(f"{B}/notifications/mark-all-read").json() == {"marked": 3}
    assert api.get(f"{B}/notifications/unread").json()["total"] == 0
    assert api.post(f"{B}/notifications/mark-all-read").json() == {"marked": 0}


@pytest.mark.parametrize("limit", [0, 501])
def test_task01_unread_limit_is_bounded(api, limit):
    assert api.get(f"{B}/notifications/unread", params={"limit": limit}).status_code == 400


# ---------------------------------------------------------------------------
# response format consistency
# ---------------------------------------------------------------------------


def test_task01_KNOWN_INCONSISTENCY_not_found_uses_fastapi_detail_not_the_project_envelope(api):
    """CHARACTERISATION - cosmetic API inconsistency, reported rather than endorsed.

    Validation failures here use the project's unified envelope
    (``type/code/message/details``) but 404s and tool-level 400s use FastAPI's bare
    ``{"detail": ...}`` because this router raises ``HTTPException`` directly instead
    of the domain errors every other route uses. A client that parses ``code`` for
    errors will get nothing from these.

    If the route is migrated to the shared errors, assert the envelope instead.
    """
    nf = api.get(f"{B}/e2e-no-such").json()
    assert set(nf) == {"detail"}
    ve = api.patch(f"{B}/e2e-no-such", json={"repeat_times": "x"}).json()
    assert {"type", "code", "message", "details"} <= set(ve)
