import hashlib
import logging
from pathlib import Path
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)


class MediaCache:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    async def cache(self, url, client):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".tapimg.com") or parsed.port not in (None, 443):
            raise ValueError("Unexpected TapTap image host")
        stem = hashlib.sha256(url.encode()).hexdigest()[:24]
        for ext in ("jpg", "png", "webp"):
            if (self.directory / f"{stem}.{ext}").exists():
                return f"/media/{stem}.{ext}"
        body = bytearray()
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 8 * 1024 * 1024:
                    raise ValueError("Image exceeds 8 MB")
        if body.startswith(b"\xff\xd8\xff"):
            ext = "jpg"
        elif body.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = "png"
        elif body[:4] == b"RIFF" and body[8:12] == b"WEBP":
            ext = "webp"
        else:
            raise ValueError("Unsupported image data")
        destination = self.directory / f"{stem}.{ext}"
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(body)
        temporary.replace(destination)
        return f"/media/{destination.name}"

    async def game(self, app):
        choices = {
            "cover": (app.get("ad_banner") or app.get("cover") or {}),
            "icon": app.get("icon") or {},
        }
        cached = dict(app.get("_media") or {})
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for role, source in choices.items():
                url = (source.get("medium_url") if role == "icon" else source.get("original_url")) or source.get("url")
                if not url:
                    continue
                try:
                    cached[role] = await self.cache(url, client)
                except (ValueError, httpx.HTTPError):
                    log.warning("Could not cache %s image for game %s", role, app["id"])
        return cached
