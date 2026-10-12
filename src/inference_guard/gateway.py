from contextlib import asynccontextmanager
from socket import gethostname

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from inference_guard.backends import BackendRouter, BackendsUnavailable
from inference_guard.config import Settings
from inference_guard.draining import DrainMiddleware, is_draining
from inference_guard.metrics import Metrics
from inference_guard.schemas import InferenceRequest, InferenceResponse


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or Settings()
    metrics = Metrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, transport=transport, trust_env=False
        ) as client:
            app.state.router = BackendRouter(client, settings, metrics)
            app.state.ready = True
            try:
                yield
            finally:
                app.state.ready = False

    app = FastAPI(title="InferenceGuard", version="0.1.0", lifespan=lifespan)
    app.add_middleware(DrainMiddleware, drain_file=settings.drain_file)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready(request: Request) -> dict[str, str]:
        # Backend failures must not remove the gateway from service: traffic
        # drives circuit recovery and can still be served by the fallback.
        if not getattr(request.app.state, "ready", False) or is_draining(settings.drain_file):
            raise HTTPException(status_code=503, detail="Gateway is not ready")
        return {"status": "ready"}

    @app.get("/status")
    async def status(request: Request) -> dict:
        return {
            "instance": gethostname(),
            "backends": request.app.state.router.status(),
            "configuration": {
                "primary_protocol": settings.primary_protocol,
                "request_timeout_seconds": settings.request_timeout_seconds,
                "failure_threshold": settings.failure_threshold,
                "recovery_timeout_seconds": settings.recovery_timeout_seconds,
            },
        }

    @app.post("/v1/infer", response_model=InferenceResponse)
    async def infer(payload: InferenceRequest, request: Request) -> InferenceResponse:
        try:
            return await request.app.state.router.infer(payload)
        except BackendsUnavailable as exc:
            raise HTTPException(status_code=503, detail="No inference backend available") from exc

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        return Response(
            generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST}
        )

    return app


app = create_app()
