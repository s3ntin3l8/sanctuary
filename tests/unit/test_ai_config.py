"""Unit tests for ai_config helpers."""

import pytest

from app.services.ai_config import is_external_endpoint

_LOCAL = [
    "http://127.0.0.1:11434",
    "http://localhost:1234",
    "http://192.168.2.174:1234",  # user's LAN inference box
    "http://10.0.0.5:8080",
    "http://172.16.0.1:11434",
    "http://[::1]:1234",  # IPv6 loopback
    "http://[fd00::1]:1234",  # IPv6 unique-local
    "http://ollama.local:11434",
    "http://pc.lan:1234",
    "",  # no host
]

_PUBLIC = [
    "https://api.openai.com",
    "http://8.8.8.8:1234",
    "https://my-vps.example.com:4000",
    "http://1.2.3.4",
]


@pytest.mark.unit
def test_local_endpoints_are_not_external():
    for url in _LOCAL:
        assert is_external_endpoint(url) is False, url


@pytest.mark.unit
def test_public_endpoints_are_external():
    for url in _PUBLIC:
        assert is_external_endpoint(url) is True, url
