"""Admin: user management."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import UserRole


class AdminUser(BaseModel):
    id: int
    email: str
    display_name: str | None
    role: UserRole
    is_active: bool
    created_at: datetime | None
    last_login_at: datetime | None
    owned_case_count: int


class AdminUsersView(BaseModel):
    users: list[AdminUser]
    signup_enabled: bool


class AdminUserCreate(BaseModel):
    email: str
    password: str = Field(min_length=8)
    role: UserRole = UserRole.USER


class AdminActiveUpdate(BaseModel):
    is_active: bool


class AdminRoleUpdate(BaseModel):
    role: UserRole


class AdminPasswordReset(BaseModel):
    new_password: str = Field(min_length=8)


class AdminReassignCases(BaseModel):
    new_owner_id: int


class SignupToggle(BaseModel):
    enabled: bool
