from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib

from pydantic import BaseModel, ConfigDict, Field


class RuntimeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    poll_seconds: int | None = Field(None, ge=60)
    request_interval: float | None = Field(None, ge=0, le=60)
    request_timeout: float | None = Field(None, gt=0, le=120)
    max_pages: int | None = Field(None, ge=1, le=1000)
    collect_comments: bool | None = None
    cache_images: bool | None = None
    digest_enabled: bool | None = None
    digest_interval: int | None = Field(None, ge=60)
    digest_batch_size: int | None = Field(None, ge=1, le=500)


RUNTIME_FIELDS = tuple(RuntimeUpdate.model_fields)


@dataclass
class Settings:
    database: str = "data/monitor.sqlite3"
    poll_seconds: int = 1800
    request_interval: float = 1.0
    request_timeout: float = 30.0
    max_pages: int = 100
    collect_comments: bool = True
    cache_images: bool = True
    developers: list[dict] = field(default_factory=list)
    digest_enabled: bool = False
    digest_interval: int = 86400
    digest_batch_size: int = 100
    astrbot_url: str = ""
    astrbot_key: str = ""
    api_token: str = ""
    site_developer_id: int | None = None

    @classmethod
    def load(cls, path: str = "config.toml"):
        with Path(path).open("rb") as f:
            data = tomllib.load(f)
        monitor = data.get("monitor", {})
        digest = data.get("digest", {})
        settings = cls(
            **monitor,
            developers=data.get("developers", []),
            digest_enabled=digest.get("enabled", False),
            digest_interval=digest.get("interval_seconds", 86400),
            digest_batch_size=digest.get("batch_size", 100),
            astrbot_url=os.environ.get("ASTRBOT_BASE_URL") or data.get("astrbot", {}).get("base_url", ""),
            astrbot_key=os.environ.get("ASTRBOT_API_KEY", ""),
            api_token=os.environ.get("MONITOR_API_TOKEN", ""),
            site_developer_id=data.get("site", {}).get("primary_developer_id"),
        )
        RuntimeUpdate(**{key: getattr(settings, key) for key in RUNTIME_FIELDS})
        if any(getattr(settings, key) is None for key in RUNTIME_FIELDS):
            raise ValueError("Runtime defaults cannot be null")
        return settings
