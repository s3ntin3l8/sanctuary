"""The gate's fairness rule, run through the real Lua script on a real Redis.

The unit tests mock the script, so they pin the Python wrapper only; the rule
(an aged incompatible waiter closes the gate to newcomers of the holding family,
oldest waiter first) lives in Lua and needs a server to be exercised.
"""

import time

import pytest
import redis

import app.services.model_gate as mg

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:8-alpine") as container:
        yield redis.Redis(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(6379)),
            decode_responses=True,
        )


@pytest.fixture
def acquire(client):
    client.flushall()
    script = client.register_script(mg._ACQUIRE_LUA)

    def _acquire(family: str, call_id: str, fairness: int = 120) -> int:
        return script(
            keys=[mg._CALL_KEY_PREFIX + call_id, mg._WAIT_KEY_PREFIX + call_id],
            args=[
                family,
                600,
                fairness,
                30,
                *sorted(mg.COMPATIBILITY[family]),
            ],
        )

    return _acquire


def _release(client, call_id: str) -> None:
    client.delete(mg._CALL_KEY_PREFIX + call_id)


def test_same_family_calls_overlap_and_a_conflicting_family_waits(acquire):
    assert acquire("qwen", "q1") == 1
    assert acquire("qwen", "q2") == 1
    assert acquire("chandra", "c1") == 0  # ordinary conflict, marker written


def test_before_the_threshold_newcomers_of_the_holding_family_still_get_in(acquire):
    assert acquire("qwen", "q1") == 1
    assert acquire("chandra", "c1") == 0
    assert acquire("qwen", "q2", fairness=3600) == 1


def test_past_the_threshold_newcomers_are_refused_with_the_fairness_reason(
    client, acquire
):
    assert acquire("qwen", "q1") == 1
    assert acquire("chandra", "c1") == 0
    time.sleep(1.1)
    assert acquire("qwen", "q2", fairness=1) == mg._BLOCKED_BY_FAIRNESS

    # the in-flight call drains, the aged waiter gets the gate, the marker goes
    _release(client, "q1")
    assert acquire("chandra", "c1") == 1
    assert not client.exists(mg._WAIT_KEY_PREFIX + "c1")


def test_two_incompatible_aged_waiters_do_not_deadlock(client, acquire):
    assert acquire("qwen", "q1") == 1
    assert acquire("chandra", "c1", fairness=0) == 0
    time.sleep(1.1)
    assert acquire("qwen", "q2", fairness=0) == mg._BLOCKED_BY_FAIRNESS
    _release(client, "q1")
    assert acquire("qwen", "q2", fairness=0) == mg._BLOCKED_BY_FAIRNESS  # c1 is older
    assert acquire("chandra", "c1", fairness=0) == 1


def test_embed_is_never_held_off(acquire):
    assert acquire("qwen", "q1") == 1
    assert acquire("chandra", "c1") == 0
    time.sleep(1.1)
    assert acquire("embed", "e1", fairness=1) == 1
