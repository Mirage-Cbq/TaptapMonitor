import asyncio
import time
from urllib.parse import urljoin, urlsplit

import httpx

from .config import Settings

# www.taptap.cn blocks many cloud-server IPs at its WAF (HTTP 405); m.taptap.cn serves the same webapiv2.
API_HOST = "m.taptap.cn"
ROOT = "https://" + API_HOST
ACCEPTED_HOSTS = {API_HOST, "www.taptap.cn"}
X_UA = "V=1&PN=WebApp&VN=0.1.0&LANG=zh_CN&PLT=PC"


class SourceError(Exception):
    pass


class TapTapClient:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.http = httpx.AsyncClient(
            timeout=settings.request_timeout, transport=transport, follow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Referer": "https://www.taptap.cn/",
                "Accept": "application/json",
            },
        )
        self.last_request = 0.0

    async def close(self):
        await self.http.aclose()

    @staticmethod
    def safe_url(path: str) -> str:
        url = urljoin(ROOT + "/webapiv2/", path)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc not in ACCEPTED_HOSTS or not parsed.path.startswith("/webapiv2/"):
            raise SourceError("Rejected unexpected pagination URL")
        return parsed._replace(netloc=API_HOST).geturl()

    async def get(self, path: str, params: dict | None = None) -> dict:
        url = self.safe_url(path)
        query = dict(params or {})
        query["X-UA"] = X_UA
        for attempt in range(3):
            await asyncio.sleep(max(0, self.settings.request_interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = await self.http.get(httpx.URL(url).copy_merge_params(query), timeout=self.settings.request_timeout)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        await asyncio.sleep(2 ** (attempt + 1))
                        continue
                if response.status_code != 200:
                    raise SourceError(f"TapTap HTTP {response.status_code}: {urlsplit(url).path}")
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise SourceError("TapTap returned a verification page or non-JSON response") from exc
                if payload.get("success") is not True or not isinstance(payload.get("data"), dict):
                    raise SourceError(f"TapTap API rejected request: {urlsplit(url).path}")
                return payload["data"]
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == 2:
                    raise SourceError(f"TapTap connection failed: {type(exc).__name__}") from exc
                await asyncio.sleep(2 ** (attempt + 1))
        raise SourceError("TapTap request exhausted retries")
