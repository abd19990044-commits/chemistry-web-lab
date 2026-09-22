"""Small ASGI safety middleware used by the native FastAPI surface."""
from __future__ import annotations

from starlette.responses import JSONResponse


class NativeApiBodyLimitMiddleware:
    """Reject oversized native-API bodies before JSON/form parsing.

    Flask has its own upload limit, but requests handled by FastAPI never pass
    through Flask.  The middleware buffers only native API request bodies (the
    framework would buffer those JSON bodies anyway), counts chunked requests
    as well as Content-Length requests, and then replays the body downstream.
    """

    _PREFIXES = (
        "/api/v1/analysis",
        "/api/v1/compound",
        "/api/v1/kaggle",
        "/api/v1/local-agent",
        "/api/v1/orca",
    )

    def __init__(self, app, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max(1024, int(max_bytes))

    async def __call__(self, scope, receive, send) -> None:
        if (scope.get("type") != "http"
                or scope.get("method") in {"GET", "HEAD", "OPTIONS"}
                or not str(scope.get("path") or "").startswith(self._PREFIXES)):
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                await self._reject(scope, receive, send, "invalid Content-Length")
                return

        body = bytearray()
        disconnected = False
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
                break
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay_receive():
            nonlocal delivered
            if disconnected:
                return {"type": "http.disconnect"}
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

    async def _reject(self, scope, receive, send, message: str | None = None) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "ok": False,
                "error": {
                    "code": "PAYLOAD_TOO_LARGE",
                    "message": message or (
                        "request body exceeds the native API limit of "
                        f"{self.max_bytes // (1024 * 1024)} MB"
                    ),
                },
            },
        )
        await response(scope, receive, send)
