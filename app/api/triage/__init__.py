"""Triage API package — 24 route handlers split across 5 focused sub-modules."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.triage import bundle_ops, document_ops, feed, fragments, subgroups

router = APIRouter(tags=["pages"])
router.include_router(feed.router)
router.include_router(bundle_ops.router)
router.include_router(document_ops.router)
router.include_router(subgroups.router)
router.include_router(fragments.router)
