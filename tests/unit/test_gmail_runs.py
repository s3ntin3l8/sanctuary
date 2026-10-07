"""Atomicity and fencing of the Gmail run state (app/services/gmail_runs.py)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import redis

from app.services import gmail_runs

pytestmark = pytest.mark.unit

UID = 7


@pytest.fixture(autouse=True)
def _runs(fake_run_state):
    return fake_run_state


def _begin(run_id="run-1", **extra):
    state = gmail_runs.begin_run(
        "import", UID, {"run_id": run_id, "remaining": ["a", "b"], "done": 0, **extra}
    )
    assert state
    return state


def test_begin_creates_an_active_run_at_hop_zero():
    state = _begin()
    assert state["hop"] == 0 and state["finished_at"] is None and state["started_at"]
    assert gmail_runs.is_active(gmail_runs.get_run("import", UID))


def test_only_one_live_run_at_a_time():
    _begin()
    assert gmail_runs.begin_run("import", UID, {"run_id": "run-2"}) is None
    assert gmail_runs.get_run("import", UID)["run_id"] == "run-1"


def test_a_finished_run_is_replaced():
    _begin()
    gmail_runs.finish_run("import", UID, "run-1")
    assert gmail_runs.begin_run("import", UID, {"run_id": "run-2"})["run_id"] == "run-2"


def test_an_abandoned_run_is_taken_over(_runs):
    _begin()
    key = f"sanctuary:gmail_import:{UID}"
    stale = json.loads(_runs.store[key])
    stale["updated_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    _runs.store[key] = json.dumps(stale)

    assert gmail_runs.is_active(gmail_runs.get_run("import", UID))  # not finished...
    assert not gmail_runs.is_live(gmail_runs.get_run("import", UID))  # ...but dead
    assert gmail_runs.begin_run("import", UID, {"run_id": "run-2"})["run_id"] == "run-2"


def test_update_merges_a_patch_and_heartbeats():
    _begin()
    state = gmail_runs.update_run("import", UID, "run-1", {"done": 1})
    assert state["done"] == 1 and state["remaining"] == ["a", "b"]  # others untouched
    assert state["updated_at"]


def test_a_cancel_is_never_overwritten_by_a_hop_that_read_earlier():
    _begin()
    assert gmail_runs.cancel_run("import", UID) is True
    # The hop had read the state before the cancel; its write must now be refused.
    assert (
        gmail_runs.update_run("import", UID, "run-1", {"done": 1, "remaining": ["b"]})
        is None
    )
    state = gmail_runs.get_run("import", UID)
    assert (
        state["cancelled"] is True and state["remaining"] == [] and state["done"] == 0
    )


def test_a_stale_runs_writes_cannot_clobber_a_newer_run():
    _begin("run-1")
    gmail_runs.cancel_run("import", UID)
    _begin("run-2")

    assert gmail_runs.update_run("import", UID, "run-1", {"done": 9}) is None
    assert gmail_runs.finish_run("import", UID, "run-1", error="late") is None
    state = gmail_runs.get_run("import", UID)
    assert state["run_id"] == "run-2" and state["done"] == 0
    assert gmail_runs.is_active(state)


def test_the_hop_counter_fences_out_a_duplicated_hop():
    _begin()
    assert gmail_runs.update_run("import", UID, "run-1", {"done": 1}, hop=0, next_hop=1)
    # A redelivered copy of hop 0 arrives after hop 1 was already scheduled.
    assert (
        gmail_runs.update_run("import", UID, "run-1", {"done": 1}, hop=0, next_hop=1)
        is None
    )
    assert gmail_runs.get_run("import", UID)["hop"] == 1
    assert gmail_runs.update_run("import", UID, "run-1", {}, hop=1, next_hop=2)


def test_finish_records_the_end_state_and_error():
    _begin()
    state = gmail_runs.finish_run(
        "import", UID, "run-1", patch={"done": 2}, error="boom"
    )
    assert state["done"] == 2 and state["error"] == "boom" and state["finished_at"]
    assert not gmail_runs.is_active(state)
    assert (
        gmail_runs.update_run("import", UID, "run-1", {"done": 3}) is None
    )  # no revival


def test_cancel_without_an_active_run_is_false():
    assert gmail_runs.cancel_run("import", UID) is False
    _begin()
    gmail_runs.finish_run("import", UID, "run-1")
    assert gmail_runs.cancel_run("import", UID) is False


def test_kinds_and_users_are_independent():
    _begin()
    assert gmail_runs.get_run("index", UID) is None
    assert gmail_runs.get_run("import", UID + 1) is None
    assert gmail_runs.begin_run("index", UID, {"run_id": "i"})


def test_a_lost_write_race_is_retried(_runs):
    _begin()
    _runs.contend = 2  # two other writers got in first
    assert gmail_runs.update_run("import", UID, "run-1", {"done": 1})["done"] == 1


def test_endless_contention_is_reported_not_swallowed(_runs):
    _begin()
    _runs.contend = 99
    with pytest.raises(gmail_runs.RunStateUnavailable):
        gmail_runs.update_run("import", UID, "run-1", {"done": 1})


def test_redis_down_on_write_raises_but_reads_degrade(monkeypatch):
    monkeypatch.setattr(
        gmail_runs,
        "_get_client",
        lambda: (_ for _ in ()).throw(redis.ConnectionError("down")),
    )
    assert (
        gmail_runs.get_run("import", UID) is None
    )  # the page just shows nothing running
    with pytest.raises(gmail_runs.RunStateUnavailable):
        gmail_runs.begin_run("import", UID, {"run_id": "x"})
