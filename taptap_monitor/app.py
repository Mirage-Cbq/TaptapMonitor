import asyncio
from contextlib import asynccontextmanager, suppress
import logging
import os
from pathlib import Path
import secrets
import time
import json
from html import escape
import re
from io import BytesIO
import hashlib
from PIL import Image, UnidentifiedImageError

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .client import TapTapClient
from .collector import Collector
from .config import Settings
from .delivery import Delivery, digest_input, render_digest
from .store import Store
from .website import site_data
from .lock import ProcessLock

log = logging.getLogger(__name__)


class DeveloperCreate(BaseModel):
    id: int = Field(gt=0)
    name: str = Field(default="", max_length=120)
    target_umo: str = Field(default="", max_length=300)


class DeveloperUpdate(BaseModel):
    enabled: bool | None = None
    target_umo: str | None = Field(default=None, max_length=300)


class SiteUpdate(BaseModel):
    published: bool | None = None
    display_name: str | None = Field(default=None, max_length=120)
    tagline: str | None = Field(default=None, max_length=120)
    intro: str | None = Field(default=None, max_length=1000)
    about: str | None = Field(default=None, max_length=2000)


class GameUpdate(BaseModel):
    visible: bool | None = None
    featured: bool | None = None
    sort_order: int | None = Field(default=None, ge=-100000, le=100000)
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=10000)


def create_app(settings=None, *, client=None, scheduler_enabled=True):
    settings = settings or Settings.load(os.environ.get("MONITOR_CONFIG", "config.toml"))
    if len(settings.api_token) < 24 or settings.api_token.startswith("replace-"):
        raise ValueError("Set MONITOR_API_TOKEN to a random token with at least 24 characters")
    store = Store(settings.database)
    for developer in settings.developers:
        store.add_developer(developer["id"], developer.get("name", ""), developer.get("target_umo", ""))
        if developer.get("logo") and "logo_url" not in store.presentation(developer["id"])["custom"]:
            store.save_presentation(developer["id"], {"logo_url": developer["logo"]})
    source = client or TapTapClient(settings)
    collector = Collector(store, source, settings)
    delivery = Delivery(store, settings)
    scan_task = None
    active_run = None
    pending_developers = set()

    def start_scan(developer_id=None):
        nonlocal scan_task, active_run
        if scan_task and not scan_task.done():
            raise HTTPException(409, "A collection run is already active")
        active_run = store.start_run(developer_id)
        scan_task = asyncio.create_task(collector.collect(developer_id, active_run))
        return active_run

    async def schedule():
        last_scan = time.monotonic() - settings.poll_seconds
        while True:
            try:
                if pending_developers and (not scan_task or scan_task.done()):
                    start_scan(pending_developers.pop())
                if time.monotonic() - last_scan >= settings.poll_seconds:
                    if not scan_task or scan_task.done():
                        start_scan()
                        last_scan = time.monotonic()
                await delivery.tick()
            except Exception:
                log.exception("Scheduler iteration failed")
            await asyncio.sleep(15)

    @asynccontextmanager
    async def lifespan(app):
        with ProcessLock(settings.database):
            store.recover_interrupted()
            task = asyncio.create_task(schedule()) if scheduler_enabled else None
            try:
                yield
            finally:
                for owned in (task, scan_task):
                    if owned and not owned.done():
                        owned.cancel()
                        with suppress(asyncio.CancelledError):
                            await owned
                await source.close()

    app = FastAPI(title="TapTap 作品监测", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.collector = collector

    @app.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def primary_site():
        saved = store.query("SELECT value FROM settings WHERE key='primary_site'")
        enabled = store.developers(enabled_only=True)
        return int(saved[0]["value"]) if saved else settings.site_developer_id or (enabled[0]["id"] if enabled else None)

    async def authorize(authorization: str = Header(default="")):
        if not secrets.compare_digest(authorization.encode(), ("Bearer " + settings.api_token).encode()):
            raise HTTPException(401, "Valid Bearer token required", headers={"WWW-Authenticate": "Bearer"})

    api = APIRouter(prefix="/api", dependencies=[Depends(authorize)])

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @api.get("/status")
    async def status():
        return {**store.status(), "collecting": bool(scan_task and not scan_task.done()), "active_run": active_run,
                "digest_enabled": settings.digest_enabled, "astrbot_configured": bool(settings.astrbot_url and settings.astrbot_key)}

    @api.get("/developers")
    async def developers():
        return store.developers()

    @api.post("/developers", status_code=201)
    async def add_developer(body: DeveloperCreate):
        if store.developer(body.id):
            raise HTTPException(409, "Developer already subscribed")
        store.add_developer(body.id, body.name, body.target_umo)
        if scheduler_enabled:
            if scan_task and not scan_task.done():
                pending_developers.add(body.id)
            else:
                start_scan(body.id)
        return store.developer(body.id)

    @api.patch("/developers/{developer_id}")
    async def update_developer(developer_id: int, body: DeveloperUpdate):
        if not store.developer(developer_id):
            raise HTTPException(404, "Developer not found")
        fields = body.model_dump(exclude_none=True)
        store.update_developer(developer_id, **fields)
        return store.developer(developer_id)

    @api.get("/games")
    async def games(developer_id: int | None = Query(None, gt=0)):
        return store.games(developer_id)

    @api.get("/items")
    async def items(developer_id: int | None = None, app_id: int | None = None,
                    kind: str | None = Query(None, pattern="^(review|topic|review_comment|topic_comment)$"),
                    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        return store.items(developer_id, app_id, kind, limit, offset)

    @api.get("/events")
    async def events(after: int = Query(0, ge=0), developer_id: int | None = None,
                     limit: int = Query(100, ge=1, le=500), include_baseline: bool = False):
        rows = store.events(after, developer_id, limit, include_baseline)
        return {"events": rows, "next_cursor": rows[-1]["id"] if rows else after, "has_more": bool(rows and store.events(rows[-1]["id"], developer_id, 1, include_baseline))}

    @api.post("/scan", status_code=202)
    async def scan(developer_id: int | None = None):
        if developer_id is not None and not store.developer(developer_id):
            raise HTTPException(404, "Developer not found")
        return {"run_id": start_scan(developer_id)}

    @api.get("/runs")
    async def runs(limit: int = Query(20, ge=1, le=100)):
        return store.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))

    @api.get("/digest-input")
    async def summary(developer_id: int, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), include_baseline: bool = False):
        developer = store.developer(developer_id)
        if not developer:
            raise HTTPException(404, "Developer not found")
        events = store.events(after, developer_id, limit, include_baseline)
        next_cursor = events[-1]["id"] if events else after
        return {**digest_input(developer, events), "next_cursor": next_cursor,
                "has_more": bool(events and store.events(next_cursor, developer_id, 1, include_baseline)),
                "markdown": render_digest(developer, events) if events else "本次没有新增反馈。"}

    @api.get("/deliveries")
    async def deliveries(limit: int = Query(20, ge=1, le=100)):
        return store.query("SELECT id,developer_id,from_cursor,to_cursor,status,attempts,retry_at,created_at,sent_at,error,sent_parts FROM deliveries ORDER BY id DESC LIMIT ?", (limit,))

    @api.get("/openapi.json")
    async def schema():
        return app.openapi()

    @api.get("/sites/{developer_id}")
    async def site_settings(developer_id: int):
        data = site_data(store, developer_id, include_hidden=True)
        if not data:
            raise HTTPException(404, "Developer not found")
        return {**data, "is_primary": primary_site() == developer_id}

    @api.patch("/sites/{developer_id}")
    async def update_site(developer_id: int, body: SiteUpdate):
        if not store.developer(developer_id):
            raise HTTPException(404, "Developer not found")
        store.save_presentation(developer_id, body.model_dump(exclude_none=True))
        return site_data(store, developer_id, include_hidden=True)

    @api.put("/sites/{developer_id}/logo")
    async def upload_logo(developer_id: int, request: Request):
        if not store.developer(developer_id):
            raise HTTPException(404, "Developer not found")
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 8 * 1024 * 1024:
                raise HTTPException(413, "Logo must be smaller than 8 MB")
        try:
            with Image.open(BytesIO(content)) as picture:
                if picture.format not in {"PNG", "JPEG", "WEBP"} or max(picture.size) > 4096:
                    raise HTTPException(422, "Use PNG, JPEG or WebP, up to 4096 pixels per side")
                suffix = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[picture.format]
                picture.verify()
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError) as exc:
            raise HTTPException(422, "Invalid logo image") from exc
        filename = "logo-" + hashlib.sha256(content).hexdigest()[:24] + "." + suffix
        destination = media_dir / filename
        if not destination.exists():
            temporary = destination.with_suffix(".tmp")
            temporary.write_bytes(content)
            temporary.replace(destination)
        store.save_presentation(developer_id, {"logo_url": "/media/" + filename})
        return {"logo_url": "/media/" + filename}

    @api.delete("/sites/{developer_id}/logo")
    async def reset_logo(developer_id: int):
        if not store.developer(developer_id):
            raise HTTPException(404, "Developer not found")
        store.save_presentation(developer_id, {"logo_url": ""})
        return {"status": "ok"}

    @api.patch("/sites/{developer_id}/games/{app_id}")
    async def update_game(developer_id: int, app_id: int, body: GameUpdate):
        if app_id not in {g["id"] for g in store.games(developer_id)}:
            raise HTTPException(404, "Game not found for developer")
        store.save_presentation(developer_id, body.model_dump(exclude_none=True), app_id)
        return {"status": "ok"}

    @api.put("/site-default/{developer_id}")
    async def set_primary(developer_id: int):
        if not store.developer(developer_id):
            raise HTTPException(404, "Developer not found")
        with store.connect() as db:
            db.execute("INSERT INTO settings VALUES('primary_site',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(developer_id),))
        return {"developer_id": developer_id}

    app.include_router(api)
    static_dir = Path(__file__).resolve().parent.parent / "website" / "dist"
    media_dir = Path(settings.database).resolve().parent / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=media_dir), name="media")

    @app.get("/public/site")
    async def public_site(developer_id: int | None = Query(None, gt=0)):
        data = site_data(store, developer_id or primary_site())
        if not data:
            raise HTTPException(404, "Site not found")
        return data

    if static_dir.exists():
        def homepage(developer_id, game_id=None):
            data = site_data(store, developer_id)
            if not data:
                raise HTTPException(404, "Site not found")
            selected = next((g for g in data["games"] if g["id"] == game_id), None)
            if game_id and not selected:
                raise HTTPException(404, "Game not found")
            data["selected_game"] = game_id
            template = (static_dir / "index.html").read_text(encoding="utf-8")
            title = (selected["title"] + " · " if selected else "") + data["name"]
            description = selected["description"] if selected else data["intro"]
            replacements = {
                "__PAGE_TITLE__": escape(title), "__PAGE_DESCRIPTION__": escape(description[:200], quote=True),
                "__STUDIO_NAME__": escape(data["name"]), "__STUDIO_TAGLINE__": escape(data["tagline"]),
                "__STUDIO_LOGO__": escape(data["logo"], quote=True),
                "__SITE_DATA__": json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026"),
            }
            template = re.sub("|".join(replacements), lambda match: replacements[match[0]], template)
            return HTMLResponse(template, headers={"Cache-Control": "no-cache"})

        @app.get("/", response_class=HTMLResponse)
        async def home():
            return homepage(primary_site())

        @app.get("/studio/{developer_id}", response_class=HTMLResponse)
        async def studio(developer_id: int):
            return homepage(developer_id)

        @app.get("/studio/{developer_id}/games/{game_id}", response_class=HTMLResponse)
        async def game_page(developer_id: int, game_id: int):
            return homepage(developer_id, game_id)

        @app.get("/admin")
        async def admin():
            return FileResponse(static_dir / "admin.html", headers={"Cache-Control": "no-store"})

        app.mount("/", StaticFiles(directory=static_dir, html=True), name="website")
    return app
