"""Auth request/response models."""

from __future__ import annotations

from pydantic import BaseModel


class AuthConfig(BaseModel):
    """What the public sign-in screens need to know before anyone is logged in."""

    first_run: bool
    signup_enabled: bool
    oidc_enabled: bool
    oidc_provider_name: str


class LoginRequest(BaseModel):
    email: str
    password: str
    next: str = "/"


class SignupRequest(BaseModel):
    email: str
    password: str
    password_confirm: str
    display_name: str = ""


class SessionStarted(BaseModel):
    """A session cookie was set; ``next`` is the same-site path to navigate to."""

    next: str
