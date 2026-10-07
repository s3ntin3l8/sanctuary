"""The rail's 🔔 panel: what needs the caller across every case they may see."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

NotificationKind = Literal[
    "overdue_deadline",
    "upcoming_deadline",
    "hearing",
    "pending_triage",
    "overdue_cost",
]


class NotificationItem(BaseModel):
    id: int
    kind: NotificationKind
    title: str
    detail: str | None
    amount: float | None
    """Open amount in EUR for cost rows; the client formats it."""
    due_at: datetime | None
    case_id: str | None
    case_title: str | None
    link: str
    """SPA path the row navigates to."""


class NotificationGroup(BaseModel):
    kind: NotificationKind
    count: int
    """Full count; ``items`` holds at most the first few."""
    items: list[NotificationItem]


class NotificationsView(BaseModel):
    total: int
    groups: list[NotificationGroup]
    generated_at: datetime
