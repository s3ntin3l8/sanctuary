"""Unit tests for model_gate — acquire/release, degrade-open, and the
heartbeat added in PR3a of the 2026-09-27 ingestion audit.

Mirrors the mocking style of test_ocr_slots.py: the Redis client and the
registered Lua script are mocked, so these assert the Python wrapper's
control flow, not the Lua script's own atomicity (that needs a live Redis,
exercised manually — see the OCR-concurrency plan's end-to-end step).
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import redis

import app.services.model_gate as model_gate_module
from app.services.model_gate import model_gate


@pytest.fixture(autouse=True)
def _reset_singletons():
    """model_gate caches the client/script as module globals; isolate tests."""
    model_gate_module._sync_client = None
    model_gate_module._acquire_script = None
    yield
    model_gate_module._sync_client = None
    model_gate_module._acquire_script = None


def _mock_client(script_results=None, script_side_effect=None):
    client = MagicMock()
    script = MagicMock()
    if script_side_effect is not None:
        script.side_effect = script_side_effect
    else:
        script.side_effect = iter(script_results or [1])
    client.register_script.return_value = script
    return client, script


@pytest.mark.unit
def test_model_gate_acquires_and_releases():
    client, _ = _mock_client(script_results=[1])
    with patch.object(model_gate_module, "_get_client", return_value=client):
        with model_gate("chandra", label="doc:1") as token:
            assert token is not None
            client.delete.assert_not_called()
    client.delete.assert_called_once_with(token)


@pytest.mark.unit
def test_model_gate_rejects_unknown_family():
    with pytest.raises(ValueError, match="unknown family"):
        with model_gate("not-a-real-family"):
            pass


@pytest.mark.unit
def test_model_gate_degrades_open_when_redis_unavailable():
    # redis-py's register_script() is a pure Python-side wrapper with no I/O
    # (the actual EVALSHA happens on the returned Script object's own call),
    # so the realistic failure point is the script call itself, not
    # registration.
    client, _ = _mock_client(
        script_side_effect=redis.ConnectionError("simulated outage")
    )
    with patch.object(model_gate_module, "_get_client", return_value=client):
        with model_gate("chandra", label="doc:1") as token:
            assert token is None  # degraded — no gating, but the call proceeds


@pytest.mark.unit
def test_model_gate_times_out_when_blocked_the_whole_wait():
    # Every acquire attempt returns 0 (blocked by an incompatible family).
    # "chandra" (not "qwen") sidesteps the ingest-queue-defer bias, which is
    # unrelated to what this test exercises.
    client, _ = _mock_client(script_side_effect=lambda **kwargs: 0)
    with patch.object(model_gate_module, "_get_client", return_value=client):
        with pytest.raises(TimeoutError):
            with model_gate("chandra", timeout=0.05, label="doc:1"):
                pass


@pytest.mark.unit
def test_model_gate_starts_and_stops_a_heartbeat_thread():
    """New in PR3a: while the gate is held, a background thread must be
    refreshing the sentinel's TTL — and must stop once the with-block exits,
    not leak a running thread."""
    client, _ = _mock_client(script_results=[1])
    with patch.object(model_gate_module, "_get_client", return_value=client):
        threads_during: list[str] = []
        with model_gate("chandra", label="doc:1"):
            threads_during = [
                t.name
                for t in threading.enumerate()
                if "model_gate-heartbeat" in t.name
            ]
        # Give the daemon thread's join() a moment in case of scheduling lag.
        time.sleep(0.05)
        threads_after = [
            t.name for t in threading.enumerate() if "model_gate-heartbeat" in t.name
        ]

    assert any("chandra" in name for name in threads_during)
    assert threads_after == []


@pytest.mark.unit
def test_model_gate_heartbeat_refreshes_ttl_before_expiry():
    """The heartbeat must call EXPIRE on its own sentinel key at an interval
    short enough that at least one refresh lands well before the sentinel's
    TTL would otherwise lapse."""
    client, _ = _mock_client(script_results=[1])
    with (
        patch.object(model_gate_module, "_get_client", return_value=client),
        patch.object(model_gate_module, "_SENTINEL_TTL_SECONDS", 0.09),
        patch.object(model_gate_module, "_HEARTBEAT_INTERVAL_DIVISOR", 3),
    ):
        with model_gate("chandra", label="doc:1") as token:
            # Interval is TTL/3 ≈ 0.03s — sleeping well past two intervals
            # should see at least one EXPIRE call on our own sentinel.
            time.sleep(0.1)
            client.expire.assert_any_call(token, 0.09)


@pytest.mark.unit
def test_model_gate_heartbeat_logs_and_stops_if_sentinel_already_lapsed(caplog):
    """If EXPIRE reports the key no longer exists (returns falsy), the
    sentinel already lapsed — log it and stop refreshing rather than loop
    forever against a key that isn't coming back."""
    client, _ = _mock_client(script_results=[1])
    client.expire.return_value = 0
    with (
        patch.object(model_gate_module, "_get_client", return_value=client),
        patch.object(model_gate_module, "_SENTINEL_TTL_SECONDS", 0.03),
        patch.object(model_gate_module, "_HEARTBEAT_INTERVAL_DIVISOR", 3),
        caplog.at_level("WARNING"),
    ):
        with model_gate("chandra", label="doc:1"):
            time.sleep(0.05)

    assert any("already expired" in rec.message for rec in caplog.records)


@pytest.mark.unit
def test_model_gate_passes_wait_marker_and_fairness_args_to_the_script():
    client, script = _mock_client(script_results=[1])
    client.llen.return_value = 0  # qwen consults the ingest queue first
    with patch.object(model_gate_module, "_get_client", return_value=client):
        with model_gate("qwen", label="doc:1") as token:
            pass
    kwargs = script.call_args.kwargs
    sentinel_key, wait_key = kwargs["keys"]
    assert sentinel_key == token
    assert wait_key.startswith(model_gate_module._WAIT_KEY_PREFIX)
    assert wait_key.rsplit(":", 1)[1] == sentinel_key.rsplit(":", 1)[1]
    assert kwargs["args"][:4] == [
        "qwen",
        model_gate_module._SENTINEL_TTL_SECONDS,
        model_gate_module._FAIRNESS_AFTER_SECONDS,
        model_gate_module._WAIT_KEY_TTL_SECONDS,
    ]


@pytest.mark.unit
def test_model_gate_drops_its_wait_marker_when_it_gives_up():
    """A timed-out waiter must not keep holding off other acquirers until the
    marker's TTL lapses."""
    client, script = _mock_client(script_side_effect=lambda **kwargs: 0)
    with patch.object(model_gate_module, "_get_client", return_value=client):
        with pytest.raises(TimeoutError):
            with model_gate("chandra", timeout=0.05, label="doc:1"):
                pass
    wait_key = script.call_args.kwargs["keys"][1]
    client.delete.assert_called_once_with(wait_key)
