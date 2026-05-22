"""Empirical guard: call_json_ai uses DB-configured AI provider, not ENV default.

This test catches regressions where a Celery worker (db=None path) silently
falls back to the ENV default Ollama URL instead of the DB-configured backend.

Key design decisions:
- `fake_reload_from_db` sets `chat_provider.base_url = SENTINEL_URL` and
  `chat_provider.provider = "openai"`.  Setting provider to "openai" avoids the
  HTTP probe inside `get_type()` (which short-circuits for non-"auto" values).
- `_stream_response` is monkeypatched at the module level so the outgoing URL
  is captured before any real network I/O.
- `get_ai_debug_redact` is patched to avoid hitting the DB for the redact flag.
- `get_db_session` is patched (for the db=None path) to inject a mock session.
"""

import json

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SENTINEL_URL = "http://sentinel.test:9999"
OLLAMA_DEFAULT = "http://127.0.0.1:11434"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chat_config(base_url: str):
    """Return a ChatConfig pointing at *base_url*."""
    from app.services.ai_config import ChatConfig

    return ChatConfig(
        id="test-instance",
        label="Test",
        base_url=base_url,
        provider="openai",
        api_key="sk-test",
        summary_model="test-model",
        user_context="",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCallJsonAiUsesDbProvider:
    """call_json_ai must route to the DB-configured provider URL in both paths."""

    def _common_patches(self, monkeypatch, module):
        """Apply patches shared by both db=None and db=<session> tests.

        Returns a list that will be populated with captured request URLs once
        the monkeypatched *_stream_response* is invoked.
        """
        sentinel_cfg = _make_chat_config(SENTINEL_URL)
        captured_urls: list[str] = []

        def fake_get_chat_config(db):
            return sentinel_cfg

        def fake_reload_from_db(db):
            # Simulate what reload_from_db does: update the singleton with the
            # DB-configured backend.  Setting provider to "openai" avoids the
            # HTTP probe inside get_type() (which short-circuits for non-"auto").
            module.chat_provider.base_url = SENTINEL_URL
            module.chat_provider.provider = "openai"

        def fake_get_ai_debug_redact(db):
            return False

        def fake_stream_response(
            *,
            params: dict,
            ptype,
            debug_label: str,
            resolved_model: str,
            ingest_batch_id=None,
            doc_case_id=None,
            redact: bool = False,
        ):
            # Capture the URL that would be used for the outgoing POST.
            captured_urls.append(params.get("url", ""))
            # Return a minimal valid (response, thinking) tuple so call_json_ai
            # can proceed without raising.
            return json.dumps({"result": "ok"}), ""

        monkeypatch.setattr(module, "get_chat_config", fake_get_chat_config)
        monkeypatch.setattr(module.chat_provider, "reload_from_db", fake_reload_from_db)
        monkeypatch.setattr(
            "app.services.user_settings_service.get_ai_debug_redact",
            fake_get_ai_debug_redact,
        )
        monkeypatch.setattr(module, "_stream_response", fake_stream_response)

        return captured_urls

    def test_db_none_path_uses_sentinel_url(self, monkeypatch):
        """When db=None (Celery worker path), call_json_ai must open its own
        DB session, reload the provider, and route to the DB-configured URL —
        NOT the ENV-default Ollama localhost:11434.
        """
        from unittest.mock import MagicMock

        from app.services.intelligence import _ai_call as module

        captured_urls = self._common_patches(monkeypatch, module)

        # Patch the DB session factory that call_json_ai uses in its else-branch.
        mock_db = MagicMock()

        def fake_get_db_session():
            return mock_db

        monkeypatch.setattr("app.dependencies.get_db_session", fake_get_db_session)

        # Force singleton to the wrong URL so the test fails if the reload is
        # skipped.
        original_url = module.chat_provider.base_url
        original_provider = module.chat_provider.provider
        module.chat_provider.base_url = OLLAMA_DEFAULT
        module.chat_provider.provider = "ollama"

        try:
            module.call_json_ai(
                system_prompt="You are a test assistant.",
                user_prompt='Reply with {"result": "ok"}',
                options={"temperature": 0.0},
                debug_label="test_routing",
                schema=None,
                model="test-model",
                db=None,  # ← the critical argument: no session passed by caller
                two_pass=False,
            )
        finally:
            module.chat_provider.base_url = original_url
            module.chat_provider.provider = original_provider

        assert captured_urls, (
            "_stream_response was never called — call_json_ai may have returned "
            "early before reaching the HTTP dispatch."
        )
        for url in captured_urls:
            assert SENTINEL_URL in url, (
                f"Expected sentinel URL {SENTINEL_URL!r} in request, got {url!r}. "
                "call_json_ai is routing to the wrong backend."
            )
            assert "11434" not in url, (
                f"call_json_ai used Ollama default URL: {url!r}. "
                "The db=None reload path is not working correctly."
            )

    def test_db_provided_path_uses_sentinel_url(self, monkeypatch):
        """When a db session is provided (FastAPI handler path), call_json_ai
        must reload from that session and route to its configured URL.
        """
        from unittest.mock import MagicMock

        from app.services.intelligence import _ai_call as module

        captured_urls = self._common_patches(monkeypatch, module)

        mock_db = MagicMock()

        original_url = module.chat_provider.base_url
        original_provider = module.chat_provider.provider
        module.chat_provider.base_url = OLLAMA_DEFAULT
        module.chat_provider.provider = "ollama"

        try:
            module.call_json_ai(
                system_prompt="You are a test assistant.",
                user_prompt='Reply with {"result": "ok"}',
                options={"temperature": 0.0},
                debug_label="test_routing_with_db",
                schema=None,
                model="test-model",
                db=mock_db,  # ← real session provided
                two_pass=False,
            )
        finally:
            module.chat_provider.base_url = original_url
            module.chat_provider.provider = original_provider

        assert captured_urls, "_stream_response was never called"
        for url in captured_urls:
            assert SENTINEL_URL in url, (
                f"Expected sentinel URL {SENTINEL_URL!r} in request, got {url!r}."
            )
