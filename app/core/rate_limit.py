"""Shared rate-limiter instance.

Defined here (not in app.main) so routers can import it without creating
a circular dependency: app.main → app.api → routers → app.main.
"""

import os

from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["600/minute"])


# Per-token budget for the MCP endpoint. slowapi keys on client IP, which is
# the wrong identity for an agent: many tokens can share one IP and one token
# can roam. Uses the `limits` package slowapi is built on (in-memory, which is
# right for the single uvicorn process; with several workers each gets its
# own budget, so move the storage to Redis if the deployment ever does).
MCP_TOKEN_RATE_LIMIT = parse(os.getenv("MCP_TOKEN_RATE_LIMIT", "120/minute"))
_token_limiter = FixedWindowRateLimiter(MemoryStorage())


def hit_token_limit(token_id: int) -> bool:
    """Count one call for ``token_id``; False once its window budget is spent."""
    return _token_limiter.hit(MCP_TOKEN_RATE_LIMIT, "mcp-token", str(token_id))


# Per-client-IP budgets for the unauthenticated OAuth endpoints (/register,
# /token): machine-to-machine calls with no session to key on.
OAUTH_REGISTER_LIMIT = parse("20/hour")
OAUTH_TOKEN_LIMIT = parse("120/minute")
_ip_limiter = FixedWindowRateLimiter(MemoryStorage())


def hit_ip_limit(name: str, rate, ip: str) -> bool:
    """Count one call to endpoint ``name`` from ``ip``; False once over budget."""
    return _ip_limiter.hit(rate, name, ip)
