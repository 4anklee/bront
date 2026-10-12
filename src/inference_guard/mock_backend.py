import asyncio
import math

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from inference_guard.schemas import BackendResponse, InferenceRequest


class FaultConfig(BaseModel):
    unavailable: bool = False
    delay_seconds: float = Field(default=0, ge=0, le=30)


def create_app() -> FastAPI:
    app = FastAPI(title="InferenceGuard mock backend", version="0.1.0")
    app.state.faults = FaultConfig()

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/infer", response_model=BackendResponse)
    async def infer(payload: InferenceRequest) -> BackendResponse:
        faults = app.state.faults
        if faults.delay_seconds:
            await asyncio.sleep(faults.delay_seconds)
        if faults.unavailable:
            raise HTTPException(status_code=503, detail="Injected backend failure")
        outputs = [value * 2 for value in payload.inputs]
        if not all(math.isfinite(value) for value in outputs):
            raise HTTPException(status_code=422, detail="Output exceeds numeric range")
        return BackendResponse(model=payload.model, outputs=outputs)

    @app.put("/faults", response_model=FaultConfig)
    async def set_faults(payload: FaultConfig) -> FaultConfig:
        """Replace fault settings. Local demo endpoint; no authentication."""
        app.state.faults = payload
        return payload

    return app


app = create_app()
