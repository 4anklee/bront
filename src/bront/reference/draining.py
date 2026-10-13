"""Gracefully retire HTTP/1.1 connections before Kubernetes stops a gateway."""

from pathlib import Path

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def is_draining(path: Path | None) -> bool:
    return path is not None and path.is_file()


class DrainMiddleware:
    def __init__(self, app: ASGIApp, drain_file: Path | None = None) -> None:
        self.app = app
        self.drain_file = drain_file

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.drain_file is None:
            await self.app(scope, receive, send)
            return

        async def send_response(message: Message) -> None:
            if message["type"] == "http.response.start" and is_draining(self.drain_file):
                # Finish this response, then let the client reconnect through the
                # Service to a ready pod. Also covers requests already in flight.
                MutableHeaders(scope=message)["Connection"] = "close"
            await send(message)

        await self.app(scope, receive, send_response)
