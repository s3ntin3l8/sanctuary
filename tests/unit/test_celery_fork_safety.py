"""Prefork children must not reuse DB sockets inherited from the parent."""

import importlib
from unittest.mock import patch

import pytest
from celery.signals import worker_process_init

celery_module = importlib.import_module("app.tasks.celery_app")


@pytest.mark.unit
def test_dispose_hook_is_connected_to_worker_process_init():
    receivers = [r() for _id, r, *_ in worker_process_init.receivers]
    assert celery_module._dispose_inherited_db_connections in receivers


@pytest.mark.unit
def test_dispose_hook_drops_pool_without_closing_parent_sockets():
    with patch("app.config.engine") as engine:
        celery_module._dispose_inherited_db_connections()
    engine.dispose.assert_called_once_with(close=False)
