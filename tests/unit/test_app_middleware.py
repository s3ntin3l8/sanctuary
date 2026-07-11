"""Tests for app-level middleware wiring (FastAPI middleware stack)."""

import logging

from app.main import app


def test_slowapi_middleware_is_registered():
    from slowapi.middleware import SlowAPIMiddleware

    middleware_classes = [m.cls for m in app.user_middleware]
    assert SlowAPIMiddleware in middleware_classes, (
        "SlowAPIMiddleware not registered — `default_limits=['20/minute']` "
        "set on app.state.limiter is inert without the middleware. "
        "Add `app.add_middleware(SlowAPIMiddleware)` in app/main.py."
    )


def test_access_log_middleware_is_registered_outermost():
    """AccessLogMiddleware must be the outermost user middleware.

    `app.add_middleware` prepends (Starlette inserts at index 0), so the
    *last* `add_middleware` call in app/main.py ends up first in
    `app.user_middleware` -- i.e. outermost, right inside
    ServerErrorMiddleware and outside every other layer (AuthGate,
    OriginGuard, add_request_id, ...). It must stay outermost: it exists
    specifically to observe every response that reaches the client even
    when an inner layer's own logging is bypassed (see issue #98).
    """
    from app.main import AccessLogMiddleware

    middleware_classes = [m.cls for m in app.user_middleware]
    assert middleware_classes[0] is AccessLogMiddleware, (
        "AccessLogMiddleware must be the last app.add_middleware(...) call "
        "in app/main.py so it stays outermost."
    )


def test_access_log_middleware_logs_5xx_independent_of_inner_layers(caplog):
    """AccessLogMiddleware must log a 5xx response even when no inner layer does.

    Regression test for issue #98: `/triage/confirm` intermittently returned
    a real, app-rendered 500 in CI with *zero* server-side log line -- not
    from `add_request_id` (only fires on a raised exception or once
    call_next returns) and not from `server_error_handler` (only fires when
    Starlette's ExceptionMiddleware actually dispatches to it). Whatever
    inner layer produced that response, it never went through either path.

    AccessLogMiddleware must not depend on any inner layer's control flow:
    it reads the status straight off the raw `http.response.start` ASGI
    message via a `send` wrapper, so it logs the 500 regardless of *how*
    the inner stack produced it -- which is exactly what was missing.
    """
    import anyio

    from app.main import AccessLogMiddleware

    async def inner_app_bypasses_everything(scope, receive, send):
        # Simulates a response that reaches the client without going
        # through add_request_id's except-block or server_error_handler --
        # exactly the unexplained gap from #98. No exception is raised
        # here; a 500 is simply sent, the same as what CI observed.
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send(
            {
                "type": "http.response.body",
                "body": b"<html>500 - Server Error</html>",
                "more_body": False,
            }
        )

    async def run():
        middleware = AccessLogMiddleware(inner_app_bypasses_everything)
        scope = {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/triage/confirm",
            "headers": [(b"host", b"testserver")],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        messages = []

        async def send(message):
            messages.append(message)

        await middleware(scope, receive, send)
        return messages

    with caplog.at_level("ERROR"):
        messages = anyio.run(run)

    # The response itself must still reach the client untouched.
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 500

    # And the middleware must have logged it independently.
    matching = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR
        and "POST" in r.message
        and "/triage/confirm" in r.message
        and "500" in r.message
    ]
    assert matching, (
        "AccessLogMiddleware did not log the 5xx response. Log records: "
        f"{[r.message for r in caplog.records]}"
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
