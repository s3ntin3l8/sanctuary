"""Tests for app-level middleware wiring (FastAPI middleware stack)."""

from app.main import app


def test_slowapi_middleware_is_registered():
    from slowapi.middleware import SlowAPIMiddleware

    middleware_classes = [m.cls for m in app.user_middleware]
    assert SlowAPIMiddleware in middleware_classes, (
        "SlowAPIMiddleware not registered — `default_limits=['20/minute']` "
        "set on app.state.limiter is inert without the middleware. "
        "Add `app.add_middleware(SlowAPIMiddleware)` in app/main.py."
    )
