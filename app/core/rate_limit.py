"""Shared rate-limiter instance.

Defined here (not in app.main) so routers can import it without creating
a circular dependency: app.main → app.api → routers → app.main.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["600/minute"])
