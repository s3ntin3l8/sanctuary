"""Effective AI configuration: multi-instance registry with per-role active resolution."""

from __future__ import annotations

import hashlib
import secrets
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
from app.core import secrets as secrets_store
from app.models.enums import AuditEventType
from app.services import audit_service
from app.services.ai_provider import normalize_base_url


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


# Placeholder stored for endpoints that need no key. Not a secret, so it is
# neither encrypted nor treated as one.
_NO_KEY = "not-needed"


def _raw_ai_section(db) -> dict:
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


def _get_ai_section(db) -> dict:
    """The stored AI section with instance API keys decrypted.

    Decryption sits outside _raw_ai_section's blanket except: a wrong or
    missing SECRETS_ENCRYPTION_KEY must fail loudly, not silently fall back to
    the env-default endpoint.
    """
    ai = _raw_ai_section(db)
    instances = []
    for inst in ai.get("instances", []):
        if secrets_store.is_encrypted(inst.get("api_key")):
            inst = {**inst, "api_key": secrets_store.decrypt(inst["api_key"])}
        instances.append(inst)
    return {**ai, "instances": instances} if instances else ai


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
        base_url=normalize_base_url(inst.get("base_url") or env["base_url"]),
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
        base_url=normalize_base_url(inst.get("base_url") or env["base_url"]),
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
        base_url=normalize_base_url(inst.get("base_url") or env["base_url"]),
        provider=(inst.get("provider") or env["provider"]).strip(),
        api_key=(inst.get("api_key") or env["api_key"]).strip(),
        ocr_model=(inst.get("ocr_model") or "").strip(),
    )


# --- Verifier (opt-in second opinion by another model) ---------------------------
#
# Not a fourth entry in _ACTIVE_KEY_BY_ROLE: those roles fall back to the
# first instance (and the env endpoint) when nothing is picked, which for the
# verifier would mean sending document text somewhere nobody chose. The
# verifier resolves only on an exact ``active_verifier_id`` match, with its own
# model, and only while ``verifier_enabled`` is on.

# Groups of AI output the verifier can check, as the settings page offers them.
VERIFIER_GROUPS = (
    "document_fields",
    "summary",
    "relationships",
    "claims",
    "action_items",
    "brief",
)
DOCUMENT_VERIFIER_GROUPS = tuple(g for g in VERIFIER_GROUPS if g != "brief")


@dataclass(frozen=True)
class VerifierSettings:
    """The stored verifier switch and scope (``AppSettings.settings_json["ai"]``)."""

    enabled: bool
    instance_id: str | None
    model: str
    groups: tuple[str, ...]
    all_cases: bool
    case_ids: tuple[str, ...]


@dataclass(frozen=True)
class VerifierConfig:
    """Connection of the configured verifier: an endpoint and a model."""

    id: str
    label: str
    base_url: str
    provider: str
    api_key: str
    model: str


def get_verifier_settings(db) -> VerifierSettings:
    ai = _raw_ai_section(db)
    groups = ai.get("verifier_groups")
    case_ids = ai.get("verifier_case_ids")
    return VerifierSettings(
        enabled=bool(ai.get("verifier_enabled", False)),
        instance_id=ai.get("active_verifier_id") or None,
        model=(ai.get("verifier_model") or "").strip(),
        groups=tuple(
            g
            for g in (groups if isinstance(groups, list) else [])
            if g in VERIFIER_GROUPS
        ),
        all_cases=bool(ai.get("verifier_all_cases", False)),
        case_ids=tuple(
            c
            for c in (case_ids if isinstance(case_ids, list) else [])
            if isinstance(c, str)
        ),
    )


def get_verifier_config(db) -> VerifierConfig | None:
    """The verifier's endpoint and model, or None when either is not chosen.

    No fallback to another instance or to the env endpoint.
    """
    vs = get_verifier_settings(db)
    if not vs.instance_id or not vs.model:
        return None
    inst = get_instance(db, vs.instance_id)
    if inst is None or not (inst.get("base_url") or "").strip():
        return None
    return VerifierConfig(
        id=inst["id"],
        label=inst.get("label", ""),
        base_url=normalize_base_url(inst["base_url"]),
        provider=(inst.get("provider") or "auto").strip(),
        api_key=(inst.get("api_key") or _NO_KEY).strip(),
        model=vs.model,
    )


def external_ai_in_use(db) -> bool:
    """True while document text can leave this machine through an AI endpoint.

    That is the active chat or embeddings endpoint, the OCR endpoint while
    Chandra is the extraction engine (Docling reads locally), or the verifier
    while it is switched on, when ``is_external_endpoint`` says the endpoint
    is on the public internet. Drives the shell's privacy badge.
    """
    from app.services.ai_provider import is_external_endpoint
    from app.services.user_settings_service import get_extraction_engine

    urls = [get_chat_config(db).base_url, get_embed_config(db).base_url]
    if get_extraction_engine(db) == "chandra":
        urls.append(get_ocr_config(db).base_url)
    if get_verifier_settings(db).enabled:
        verifier = get_verifier_config(db)
        if verifier is not None:
            urls.append(verifier.base_url)
    return any(is_external_endpoint(u) for u in urls if u)


def verifier_for_case(
    db, case_id: str | None, groups: tuple[str, ...] | None = None
) -> tuple[VerifierConfig, tuple[str, ...]] | None:
    """The verifier and the groups it may check for ``case_id``, or None.

    None unless the switch is on, an endpoint and model are chosen, the case is
    a real case in scope (all cases, or on the allowlist; the Triage Inbox
    never is) and at least one of ``groups`` (default: every group) is
    selected. Every verifier call goes through here first: None means no call.
    """
    if not case_id or case_id == "_TRIAGE":
        return None
    vs = get_verifier_settings(db)
    if not vs.enabled:
        return None
    if not vs.all_cases and case_id not in vs.case_ids:
        return None
    wanted = VERIFIER_GROUPS if groups is None else groups
    selected = tuple(g for g in vs.groups if g in wanted)
    if not selected:
        return None
    cfg = get_verifier_config(db)
    if cfg is None:
        return None
    return cfg, selected


def save_verifier_settings(db, vs: VerifierSettings) -> None:
    """Persist the verifier switch, endpoint, model and scope (audited)."""
    from app.services.app_settings_service import _get_or_create

    settings = _get_or_create(db)
    data = dict(settings.settings_json or {})
    ai = dict(data.get("ai", {}))
    ai.update(
        {
            "verifier_enabled": vs.enabled,
            "active_verifier_id": vs.instance_id,
            "verifier_model": vs.model,
            "verifier_groups": list(vs.groups),
            "verifier_all_cases": vs.all_cases,
            "verifier_case_ids": list(vs.case_ids),
        }
    )
    data["ai"] = ai
    settings.settings_json = data
    audit_service.record(
        db,
        AuditEventType.AI_VERIFIER_CHANGED,
        payload={
            "enabled": vs.enabled,
            "instance_id": vs.instance_id,
            "model": vs.model,
            "groups": list(vs.groups),
            "all_cases": vs.all_cases,
            "case_ids": list(vs.case_ids),
        },
    )
    db.commit()


def rename_verifier_case(db, old_id: str, new_id: str | None) -> None:
    """Keep a renamed case on the verifier allowlist, or (``new_id`` None) take
    a deleted or merged-away case off it, so a later case reusing the id is not
    in scope by accident. Caller commits."""
    from app.models.database import AppSettings

    settings = db.query(AppSettings).first()
    if settings is None or not isinstance(settings.settings_json, dict):
        return
    ai = settings.settings_json.get("ai") or {}
    case_ids = ai.get("verifier_case_ids")
    if not isinstance(case_ids, list) or old_id not in case_ids:
        return
    data = dict(settings.settings_json)
    data["ai"] = {
        **ai,
        "verifier_case_ids": [
            new_id if c == old_id else c
            for c in case_ids
            if c != old_id or new_id is not None
        ],
    }
    settings.settings_json = data


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
    """Create or update an instance (matched by id).

    ``instance`` carries a plaintext ``api_key``; it is encrypted for storage.
    """
    from app.services.app_settings_service import _get_or_create

    api_key = instance.get("api_key")
    if api_key and api_key != _NO_KEY:
        instance = {**instance, "api_key": secrets_store.encrypt(api_key)}

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
