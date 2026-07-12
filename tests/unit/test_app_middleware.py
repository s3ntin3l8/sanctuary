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


def test_access_log_middleware_logs_before_app_returns(caplog):
    """The 5xx log line must be written at send-time, not after self.app() returns.

    Follow-up to issue #98's second recurrence: even with
    test_access_log_middleware_logs_5xx_independent_of_inner_layers passing
    (i.e. #99 shipped), a second CI run reproduced the exact same
    traceback-less 500 -- this time on a branch that *had* that fix. Root
    cause: the original implementation captured the status in `send_wrapper`
    but only emitted the log line after `await self.app(...)` returned. This
    repo's real confirm route dispatches a `CELERY_TASK_ALWAYS_EAGER`
    background task immediately after responding
    (triage_confirmation.reset_and_reenrich) -- work that runs *after* the
    response is sent but *before* the ASGI callable returns. If that window
    is long, or the process is killed inside it, the log line never gets
    written even though the client already has its 500.

    This test simulates exactly that window: the log assertion runs from
    *inside* the fake app, between sending the response and returning,
    proving the line exists by then rather than only after the whole call
    completes.
    """
    import anyio

    from app.main import AccessLogMiddleware

    async def inner_app_does_work_after_sending(scope, receive, send):
        await send({"type": "http.response.start", "status": 500, "headers": []})

        # Simulates reset_and_reenrich's post-response dispatch: some
        # arbitrary work that happens after the response has gone out but
        # before this ASGI callable returns control to the middleware.
        assert any(
            r.levelno >= logging.ERROR and "POST" in r.message and "500" in r.message
            for r in caplog.records
        ), (
            "5xx not logged until after the app's own post-response work -- "
            "should be logged the instant http.response.start is sent."
        )

        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def run():
        middleware = AccessLogMiddleware(inner_app_does_work_after_sending)
        scope = {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/triage/confirm",
            "headers": [(b"host", b"testserver")],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            pass

        await middleware(scope, receive, send)

    with caplog.at_level("ERROR"):
        anyio.run(run)


def test_access_log_middleware_logs_cancelled_error(caplog):
    """A cancelled request must still be logged, not silently swallowed.

    `except Exception` (the original guard) does not catch
    `asyncio.CancelledError` -- a `BaseException` subclass since Python 3.8 --
    so a request cancelled under load (e.g. the 8-worker xdist concurrency
    CI's e2e job runs under) could reach neither this log line nor a
    response, with nothing in server.log to show for it. The guard must be
    `except BaseException` so this case is logged (and still re-raised, so
    cancellation semantics are preserved) instead of vanishing.
    """
    import asyncio

    import anyio
    import pytest

    from app.main import AccessLogMiddleware

    async def inner_app_gets_cancelled(scope, receive, send):
        raise asyncio.CancelledError()

    async def run():
        middleware = AccessLogMiddleware(inner_app_gets_cancelled)
        scope = {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/triage/confirm",
            "headers": [(b"host", b"testserver")],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            pass

        await middleware(scope, receive, send)

    with caplog.at_level("ERROR"), pytest.raises(asyncio.CancelledError):
        anyio.run(run)

    assert any(
        r.levelno >= logging.ERROR
        and "POST" in r.message
        and "/triage/confirm" in r.message
        for r in caplog.records
    ), (
        "CancelledError reaching the outermost middleware must still be "
        f"logged. Log records: {[r.message for r in caplog.records]}"
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
