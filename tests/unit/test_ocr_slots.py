"""Unit tests for ocr_slots.ocr_slot / set_limit / inflight_count.

Mirrors the mocking style of test_worker_control.py: the Redis client and
the registered Lua script are both mocked, so these tests assert the Python
wrapper's acquire/retry/timeout/fail-open control flow, NOT the Lua script's
internal counting logic (that requires a live Redis server, which the unit
test job intentionally doesn't run — see docker compose in .github/workflows
/ci.yml, where Redis is only started for the advisory e2e job). The Lua's
"missing limit key -> DEFAULT_OCR_CONCURRENCY, never 0" coalescing is
exercised manually per the OCR-concurrency plan's end-to-end verification
step against a live app + Redis.
"""

from unittest.mock import MagicMock, patch

import pytest
import redis

import app.services.ocr_slots as ocr_slots_module
from app.services.ocr_slots import inflight_count, ocr_slot, set_limit


@pytest.fixture(autouse=True)
def _reset_singletons():
    """ocr_slots caches the client/script as module globals; isolate tests."""
    ocr_slots_module._sync_client = None
    ocr_slots_module._acquire_script = None
    yield
    ocr_slots_module._sync_client = None
    ocr_slots_module._acquire_script = None


def _mock_client(script_results=None, script_side_effect=None):
    """Build a mock redis client whose registered script returns/raises as given."""
    client = MagicMock()
    script = MagicMock()
    if script_side_effect is not None:
        script.side_effect = script_side_effect
    else:
        script.side_effect = iter(script_results or [1])
    client.register_script.return_value = script
    return client, script


@pytest.mark.unit
def test_ocr_slot_acquires_and_releases():
    client, script = _mock_client(script_results=[1])
    with patch.object(ocr_slots_module, "_get_client", return_value=client):
        with ocr_slot(label="doc:1:page:1") as token:
            assert token is not None
            client.delete.assert_not_called()
    client.delete.assert_called_once_with(token)


@pytest.mark.unit
def test_ocr_slot_passes_ttl_and_default_limit_to_script():
    client, script = _mock_client(script_results=[1])
    with patch.object(ocr_slots_module, "_get_client", return_value=client):
        with ocr_slot():
            pass
    _, kwargs = script.call_args
    assert kwargs["keys"][1] == ocr_slots_module._LIMIT_KEY
    assert kwargs["args"] == [
        ocr_slots_module._SENTINEL_TTL_SECONDS,
        ocr_slots_module.DEFAULT_OCR_CONCURRENCY,
    ]


@pytest.mark.unit
def test_ocr_slot_blocks_then_admits():
    """0 (full) then 1 (a slot freed up) — retries and eventually succeeds."""
    client, script = _mock_client(script_results=[0, 0, 1])
    with (
        patch.object(ocr_slots_module, "_get_client", return_value=client),
        patch("time.sleep", return_value=None),
    ):
        with ocr_slot(timeout=5.0) as token:
            assert token is not None
    assert script.call_count == 3
    client.delete.assert_called_once()


@pytest.mark.unit
def test_ocr_slot_times_out_when_never_admitted():
    client, script = _mock_client(script_side_effect=lambda **_: 0)
    with (
        patch.object(ocr_slots_module, "_get_client", return_value=client),
        patch("time.sleep", return_value=None),
    ):
        with pytest.raises(TimeoutError):
            with ocr_slot(timeout=0.01):
                pass
    # Never admitted, so no sentinel to release.
    client.delete.assert_not_called()


@pytest.mark.unit
def test_ocr_slot_fails_open_on_redis_error():
    """RedisError on acquire -> yields None and proceeds, no exception raised."""
    client, script = _mock_client(script_side_effect=redis.RedisError("down"))
    with patch.object(ocr_slots_module, "_get_client", return_value=client):
        with ocr_slot() as token:
            assert token is None
    # Degraded open — nothing was acquired, so nothing to release.
    client.delete.assert_not_called()


@pytest.mark.unit
def test_ocr_slot_release_swallows_redis_error():
    """A RedisError on release (delete) must not propagate out of the with-block."""
    client, script = _mock_client(script_results=[1])
    client.delete.side_effect = redis.RedisError("down")
    with patch.object(ocr_slots_module, "_get_client", return_value=client):
        with ocr_slot():
            pass  # no exception on exit despite delete() raising


@pytest.mark.unit
def test_set_limit_publishes_value():
    client = MagicMock()
    with patch.object(ocr_slots_module, "_get_client", return_value=client):
        set_limit(6)
    client.set.assert_called_once_with(ocr_slots_module._LIMIT_KEY, "6")


@pytest.mark.unit
def test_set_limit_swallows_redis_error():
    client = MagicMock()
    client.set.side_effect = redis.RedisError("down")
    with patch.object(ocr_slots_module, "_get_client", return_value=client):
        set_limit(4)  # must not raise


@pytest.mark.unit
def test_inflight_count_counts_matching_keys():
    client = MagicMock()
    client.scan_iter.return_value = iter(["a", "b", "c"])
    with patch.object(ocr_slots_module, "_get_client", return_value=client):
        assert inflight_count() == 3
    client.scan_iter.assert_called_once_with(
        match=ocr_slots_module._CALL_KEY_PREFIX + "*", count=100
    )


@pytest.mark.unit
def test_inflight_count_returns_zero_on_redis_error():
    client = MagicMock()
    client.scan_iter.side_effect = redis.RedisError("down")
    with patch.object(ocr_slots_module, "_get_client", return_value=client):
        assert inflight_count() == 0
