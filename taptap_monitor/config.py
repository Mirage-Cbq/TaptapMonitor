from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib


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
        if settings.poll_seconds < 60 or settings.request_interval < 0:
            raise ValueError("poll_seconds must be >= 60 and request_interval >= 0")
        if settings.max_pages < 1 or settings.request_timeout <= 0:
            raise ValueError("max_pages and request_timeout must be positive")
        if settings.digest_interval < 60 or not 1 <= settings.digest_batch_size <= 500:
            raise ValueError("Invalid digest interval or batch size")
        return settings
