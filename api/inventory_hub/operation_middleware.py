"""Request metadata only. Route templates prevent leaking customer identifiers."""
import asyncio
import time
from uuid import uuid4

from inventory_hub import operation_log


class OperationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started, status, failed = time.monotonic(), 500, False
        request_id = uuid4().hex

        async def respond(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {**message, "headers": [*(message.get("headers") or []), (b"x-request-id", request_id.encode())]}
            await send(message)

        with operation_log.context(request_id=request_id):
            try:
                await self.app(scope, receive, respond)
            except BaseException:
                failed = True
                raise
            finally:
                route = getattr(scope.get("route"), "path", "/unmatched")
                if route not in ("/logs/events", "/health", "/healthz", "/data", "/"):
                    await asyncio.to_thread(operation_log.emit, "http_request", component="api", direction="inbound",
                        method=scope.get("method"), target=route, status_code=status,
                        level="error" if failed or status >= 500 else "warning" if status >= 400 else "info",
                        duration_ms=round((time.monotonic() - started) * 1000),
                        **({"error_code": "request_interrupted"} if failed else {}))
