from app.services.ingestion.converters import torch_threads_per_child


def test_splits_cores_across_children():
    assert torch_threads_per_child(10, 4) == 2


def test_never_below_one():
    assert torch_threads_per_child(4, 16) == 1
    assert torch_threads_per_child(None, 4) == 1
