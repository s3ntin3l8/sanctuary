"""Effective AI configuration: multi-instance registry with per-role active resolution."""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse

from app.config import (
    AI_API_KEY,
    AI_BASE_URL,
    AI_EMBED_DIM,
    AI_EMBED_MODEL,
    AI_PROVIDER,
    AI_SUMMARY_MODEL,
    AI_USER_CONTEXT,
)
from app.models.enums import AuditEventType
from app.services import audit_service


@dataclass(frozen=True)
class ChatConfig:
    id: str
    label: str
    base_url: str
    provider: str
    api_key: str
    summary_model: str
    user_context: str


@dataclass(frozen=True)
class EmbedConfig:
    id: str
    label: str
    base_url: str
    provider: str
    api_key: str
    embed_model: str
    embed_dim: int


@dataclass(frozen=True)
class OcrConfig:
    """Effective OCR (Chandra-class) extraction config for the active OCR instance.

    Mirrors ChatConfig/EmbedConfig — same instance object, different active_id
    pointer, different model field. The OCR endpoint can be a separate host
    from chat/embed (e.g. a dedicated vision-LLM box) or the same litellm
    proxy with a different model name.
    """

    id: str
    label: str
    base_url: str
    provider: str
    api_key: str
    ocr_model: str


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


def _make_id() -> str:
    return "inst_" + secrets.token_hex(4)


_LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home", ".home.arpa")


def is_external_endpoint(base_url: str) -> bool:
    """True if base_url points at the public internet (warn about data egress).

    Trusted (no warning): loopback, RFC1918/ULA private ranges, link-local, and
    ``*.local``-style names — i.e. this machine and the user's own LAN. Anything
    that resolves to a public IP or a public DNS name is treated as external.
    """
    host = (urlparse(base_url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        # A non-local hostname (public DNS like api.openai.com) → external.
        return True


def _get_ai_section(db) -> dict:
    if db is None:
        return {}
    try:
        from app.models.database import AppSettings

        settings = db.query(AppSettings).first()
        if settings and isinstance(settings.settings_json, dict):
            return settings.settings_json.get("ai", {})
    except Exception:
        pass
    return {}


def list_instances(db) -> list[dict]:
    return list(_get_ai_section(db).get("instances", []))


def get_instance(db, instance_id: str) -> dict | None:
    for inst in list_instances(db):
        if inst.get("id") == instance_id:
            return inst
    return None


_ACTIVE_KEY_BY_ROLE = {
    "chat": "active_chat_id",
    "embed": "active_embed_id",
    "ocr": "active_ocr_id",
}


def _resolve_active(db, role: str) -> dict:
    ai = _get_ai_section(db)
    instances = ai.get("instances", [])
    key = _ACTIVE_KEY_BY_ROLE.get(role, "active_chat_id")
    active_id = ai.get(key)
    for inst in instances:
        if inst.get("id") == active_id:
            return inst
    if instances:
        return instances[0]
    return {}


def get_chat_config(db=None) -> ChatConfig:
    """Return effective chat/generation config for the active chat instance."""
    env = _env_defaults()
    inst = _resolve_active(db, "chat")
    ai = _get_ai_section(db)
    user_context = ai.get("user_context") or env["user_context"]
    return ChatConfig(
        id=inst.get("id", ""),
        label=inst.get("label", "Default"),
        base_url=(inst.get("base_url") or env["base_url"]).strip().rstrip("/"),
        provider=(inst.get("provider") or env["provider"]).strip(),
        api_key=(inst.get("api_key") or env["api_key"]).strip(),
        summary_model=(inst.get("summary_model") or env["summary_model"]).strip(),
        user_context=user_context,
    )


def get_embed_config(db=None) -> EmbedConfig:
    """Return effective embedding config for the active embed instance."""
    env = _env_defaults()
    inst = _resolve_active(db, "embed")
    return EmbedConfig(
        id=inst.get("id", ""),
        label=inst.get("label", "Default"),
        base_url=(inst.get("base_url") or env["base_url"]).strip().rstrip("/"),
        provider=(inst.get("provider") or env["provider"]).strip(),
        api_key=(inst.get("api_key") or env["api_key"]).strip(),
        embed_model=(inst.get("embed_model") or env["embed_model"]).strip(),
        embed_dim=int(inst.get("embed_dim") or env["embed_dim"]),
    )


def get_ocr_config(db=None) -> OcrConfig:
    """Return effective OCR config for the active OCR instance.

    Falls back to the same env-derived base_url/provider/api_key as chat/embed
    when no instance has an OCR model set, so existing installs that haven't
    configured Chandra still produce a coherent config object.
    """
    env = _env_defaults()
    inst = _resolve_active(db, "ocr")
    return OcrConfig(
        id=inst.get("id", ""),
        label=inst.get("label", "Default"),
        base_url=(inst.get("base_url") or env["base_url"]).strip().rstrip("/"),
        provider=(inst.get("provider") or env["provider"]).strip(),
        api_key=(inst.get("api_key") or env["api_key"]).strip(),
        ocr_model=(inst.get("ocr_model") or "").strip(),
    )


def set_active(db, role: str, instance_id: str) -> None:
    """Set the active instance for 'chat', 'embed', or 'ocr'."""
    from app.services.app_settings_service import _get_or_create

    if role not in _ACTIVE_KEY_BY_ROLE:
        raise ValueError(
            f"Unknown role {role!r}; expected one of {list(_ACTIVE_KEY_BY_ROLE)}"
        )

    settings = _get_or_create(db)
    data = dict(settings.settings_json or {})
    ai = dict(data.get("ai", {}))
    ai[_ACTIVE_KEY_BY_ROLE[role]] = instance_id
    data["ai"] = ai
    settings.settings_json = data
    audit_service.record(
        db,
        AuditEventType.AI_ACTIVE_CHANGED,
        payload={"role": role, "instance_id": instance_id},
    )
    db.commit()


def save_instance(db, instance: dict) -> None:
    """Create or update an instance (matched by id)."""
    from app.services.app_settings_service import _get_or_create

    instance_id = instance.get("id")
    existing = get_instance(db, instance_id) if instance_id else None
    settings = _get_or_create(db)
    data = dict(settings.settings_json or {})
    ai = dict(data.get("ai", {}))
    instances = list(ai.get("instances", []))
    for i, inst in enumerate(instances):
        if inst.get("id") == instance.get("id"):
            instances[i] = instance
            break
    else:
        instances.append(instance)
    ai["instances"] = instances
    data["ai"] = ai
    settings.settings_json = data
    audit_service.record(
        db,
        AuditEventType.AI_INSTANCE_UPDATED
        if existing is not None
        else AuditEventType.AI_INSTANCE_CREATED,
        target_type="ai_instance",
        target_id=instance.get("id"),
    )
    db.commit()


def delete_instance(db, instance_id: str) -> None:
    """Remove an instance by ID."""
    from app.services.app_settings_service import _get_or_create

    settings = _get_or_create(db)
    data = dict(settings.settings_json or {})
    ai = dict(data.get("ai", {}))
    ai["instances"] = [i for i in ai.get("instances", []) if i.get("id") != instance_id]
    data["ai"] = ai
    settings.settings_json = data
    audit_service.record(
        db,
        AuditEventType.AI_INSTANCE_DELETED,
        target_type="ai_instance",
        target_id=instance_id,
    )
    db.commit()


def set_user_context(db, text: str) -> None:
    from app.services.app_settings_service import _get_or_create

    settings = _get_or_create(db)
    data = dict(settings.settings_json or {})
    ai = dict(data.get("ai", {}))
    ai["user_context"] = text
    data["ai"] = ai
    settings.settings_json = data
    audit_service.record(
        db,
        AuditEventType.AI_USER_CONTEXT_CHANGED,
        payload={
            "context_hash": hashlib.sha256((text or "").encode()).hexdigest()[:16]
        },
    )
    db.commit()
