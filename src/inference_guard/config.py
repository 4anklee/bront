from typing import Literal

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IG_", env_file=".env", extra="ignore")

    primary_url: HttpUrl = HttpUrl("http://127.0.0.1:8001")
    fallback_url: HttpUrl = HttpUrl("http://127.0.0.1:8002")
    primary_protocol: Literal["mock", "triton"] = "mock"
    request_timeout_seconds: float = Field(default=1.0, gt=0)
    failure_threshold: int = Field(default=3, ge=1)
    recovery_timeout_seconds: float = Field(default=10.0, gt=0)
