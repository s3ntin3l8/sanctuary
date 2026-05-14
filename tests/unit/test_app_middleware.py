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


def test_cross_origin_mutating_request_is_blocked():
    import anyio

    from app.main import OriginGuardMiddleware

    async def app_never_called(scope, receive, send):
        raise AssertionError("inner app should not be called")

    async def run():
        messages = []
        middleware = OriginGuardMiddleware(app_never_called)
        scope = {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/api/settings/theme",
            "headers": [
                (b"host", b"testserver"),
                (b"origin", b"https://attacker.example"),
            ],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await middleware(scope, receive, send)
        return messages

    messages = anyio.run(run)
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 403
