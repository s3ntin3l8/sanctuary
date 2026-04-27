"""Effective AI configuration: DB overrides win, env vars fill blanks."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import (
    AI_API_KEY,
    AI_BASE_URL,
    AI_EMBED_DIM,
    AI_EMBED_MODEL,
    AI_PROVIDER,
    AI_SUMMARY_MODEL,
    AI_USER_CONTEXT,
)


@dataclass(frozen=True)
class AIEffectiveConfig:
    base_url: str
    provider: str
    api_key: str
    summary_model: str
    embed_model: str
    embed_dim: int
    user_context: str


def _env_defaults() -> dict:
    return {
        "base_url": AI_BASE_URL,
        "provider": AI_PROVIDER,
        "api_key": AI_API_KEY,
        "summary_model": AI_SUMMARY_MODEL,
        "embed_model": AI_EMBED_MODEL,
        "embed_dim": AI_EMBED_DIM,
        "user_context": AI_USER_CONTEXT,
    }


def get_effective_config(db=None) -> AIEffectiveConfig:
    """Return the active AI config. DB wins; env fills any blank field."""
    stored: dict = {}
    if db is not None:
        try:
            from app.models.database import UserSettings

            settings = db.query(UserSettings).first()
            if settings and isinstance(settings.settings_json, dict):
                stored = settings.settings_json.get("ai", {})
        except Exception:
            pass

    env = _env_defaults()
    return AIEffectiveConfig(
        base_url=(stored.get("base_url") or env["base_url"]).strip().rstrip("/"),
        provider=(stored.get("provider") or env["provider"]).strip(),
        api_key=(stored.get("api_key") or env["api_key"]).strip(),
        summary_model=(stored.get("summary_model") or env["summary_model"]).strip(),
        embed_model=(stored.get("embed_model") or env["embed_model"]).strip(),
        embed_dim=int(stored.get("embed_dim") or env["embed_dim"]),
        user_context=(stored.get("user_context") or env["user_context"]),
    )
