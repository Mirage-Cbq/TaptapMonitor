import asyncio
from contextlib import asynccontextmanager, suppress
import logging
import os
from pathlib import Path
from typing import Literal
import secrets
import time
import json
from html import escape
import re
from io import BytesIO
import hashlib
from PIL import Image, UnidentifiedImageError

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .client import TapTapClient
from .collector import Collector
from .config import RUNTIME_FIELDS, RuntimeUpdate, Settings
from .delivery import Delivery, digest_input, render_digest
from .store import Store
from .website import site_data
from .lock import ProcessLock
from .upgrade import Updater, UpgradeError
from .media import MediaCache
from .official import OfficialBridge, OfficialError, OfficialService
from .console import ConsoleAuthError, ConsoleError, ConsoleService
from .helper import helper_script
from .insights import analyze

log = logging.getLogger(__name__)

# Shown on a fresh install before any studio has been added.
WELCOME_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TaptapMonitor</title><meta name="robots" content="noindex"><style>
:root{color-scheme:light dark;--bg:#f4f6f5;--card:#fff;--ink:#17231c;--muted:#68766d;--accent:#1f8a6d}
@media(prefers-color-scheme:dark){:root{--bg:#0f1412;--card:#18201c;--ink:#e8efe9;--muted:#9aa8a0;--accent:#44c39b}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);font:16px/1.7 system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:440px;margin:16px;padding:32px;background:var(--card);border-radius:16px;box-shadow:0 8px 24px -16px #0004}
h1{margin:0 0 8px;font-size:22px}p{margin:0 0 20px;color:var(--muted)}
a{display:inline-block;padding:10px 18px;border-radius:10px;background:var(--accent);color:#fff;text-decoration:none;font-weight:700}
</style></head><body><main><h1>TaptapMonitor 已安装</h1><p>还没有添加要监测的 TapTap 厂商。进入管理后台，在「厂商与官网」中粘贴厂商主页链接即可开始，官网会根据厂商作品自动生成。</p><a href="/admin">进入管理后台</a></main></body></html>"""


class DeveloperCreate(BaseModel):
    id: int = Field(gt=0)
    name: str = Field(default="", max_length=120)
    target_umo: str = Field(default="", max_length=300)


class DeveloperUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
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


class ConsoleSession(BaseModel):
    cookie: str = Field(min_length=3, max_length=16384)


class HelperReport(BaseModel):
    stage: str = Field(max_length=16)
    qrcode_url: str | None = Field(None, max_length=2000)
    expires_in: float | None = None
    cookie: str | None = Field(None, max_length=16384)
    message: str | None = Field(None, max_length=500)


class UpgradeRequest(BaseModel):
    ref: str = Field(default="latest", pattern=r"^(latest|[0-9a-f]{7,40})$")


class AutoUpgrade(BaseModel):
    enabled: bool


def create_app(settings=None, *, client=None, scheduler_enabled=True, console_transport=None):
    settings = settings or Settings.load(os.environ.get("MONITOR_CONFIG", "config.toml"))
    if len(settings.api_token) < 24 or settings.api_token.startswith("replace-"):
        raise ValueError("Set MONITOR_API_TOKEN to a random token with at least 24 characters")
    store = Store(settings.database)
    defaults = getattr(settings, "_runtime_defaults", None)
    if defaults is None:
        defaults = {key: getattr(settings, key) for key in RUNTIME_FIELDS}
        settings._runtime_defaults = defaults.copy()
    overrides = RuntimeUpdate(**store.setting("runtime", {})).model_dump(exclude_none=True, exclude_unset=True)
    for key in RUNTIME_FIELDS:
        setattr(settings, key, overrides.get(key, defaults[key]))
    removed = set(store.setting("removed_developers", []))
    for developer in settings.developers:
        if developer["id"] in removed:
            continue
        store.add_developer(developer["id"], developer.get("name", ""), developer.get("target_umo", ""))
        if developer.get("logo") and "logo_url" not in store.presentation(developer["id"])["custom"]:
            store.save_presentation(developer["id"], {"logo_url": developer["logo"]})
    source = client or TapTapClient(settings)
    collector = Collector(store, source, settings)
    delivery = Delivery(store, settings)
    updater = Updater(Path(settings.database).resolve().parent, Path(__file__).resolve().parent.parent)
    official = OfficialService(store, OfficialBridge(Path(settings.database).resolve().parent))
    console = ConsoleService(store, console_transport)
    scan_task = None
    active_run = None
    pending_developers = {}

    def start_scan(developer_id=None, mode="incremental"):
        nonlocal scan_task, active_run
        if scan_task and not scan_task.done():
            raise HTTPException(409, "A collection run is already active")
        active_run = store.start_run(developer_id, mode)
        async def collect():
            result = await collector.collect(developer_id, active_run, mode=mode)
            official.sync_if_due()
            return result
        scan_task = asyncio.create_task(collect())
        return active_run

    async def schedule():
        # Resume the schedule from the last recorded run so a restart does not trigger a scan;
        # only a fresh install (no runs yet) collects right away.
        last_run = store.query("SELECT MAX(started_at) AS t FROM runs")[0]["t"]
        last_scan = last_run if last_run is not None else time.time() - settings.poll_seconds
        while True:
            try:
                if pending_developers and (not scan_task or scan_task.done()):
                    developer_id = next(iter(pending_developers))
                    start_scan(developer_id, pending_developers.pop(developer_id))
                if time.time() - last_scan >= settings.poll_seconds:
                    if not scan_task or scan_task.done():
                        start_scan()
                        last_scan = time.time()
                await delivery.tick()
                console.sync_if_due()
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
                try:
                    await source.close()
                finally:
                    await official.close()
                    await console.close()

    app = FastAPI(title="TapTap 作品监测", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.collector = collector
    app.state.settings = settings
    app.state.official = official
    app.state.console = console

    @app.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        path = request.url.path
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif path.endswith((".js", ".css", ".html")) and "cache-control" not in response.headers:
            # Revalidate page assets (cheap 304 via ETag) so a cached old admin.js never meets a new admin.html.
            response.headers["Cache-Control"] = "no-cache"
        return response

    def primary_site():
        saved = store.query("SELECT value FROM settings WHERE key='primary_site'")
        enabled = store.developers(enabled_only=True)
        return int(saved[0]["value"]) if saved else settings.site_developer_id or (enabled[0]["id"] if enabled else None)

    def primary_games_first(games, key="id"):
        primary = primary_site()
        ids = {game["id"] for game in store.games(primary)} if primary is not None else set()
        return sorted(games, key=lambda game: game[key] not in ids)

    async def authorize(authorization: str = Header(default="")):
        if not secrets.compare_digest(authorization.encode(), ("Bearer " + settings.api_token).encode()):
            raise HTTPException(401, "Valid Bearer token required", headers={"WWW-Authenticate": "Bearer"})

    api = APIRouter(prefix="/api", dependencies=[Depends(authorize)])

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.post("/api/console/pair/{code}")
    async def console_pair(code: str, body: HelperReport):
        # Called by the login helper on the admin's PC; the one-time code stands in for the admin token.
        try:
            await console.helper_report(code, **body.model_dump())
        except ConsoleAuthError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ConsoleError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @api.get("/status")
    async def status():
        return {**store.status(), "primary_developer_id": primary_site(), "collecting": bool(scan_task and not scan_task.done()), "active_run": active_run,
                "digest_enabled": settings.digest_enabled, "astrbot_configured": bool(settings.astrbot_url and settings.astrbot_key)}

    @api.get("/developers")
    async def developers():
        result = store.developers()
        for developer in result:
            developer["is_primary"] = developer["id"] == primary_site()
            developer["game_count"] = store.query("SELECT COUNT(*) n FROM developer_games WHERE developer_id=?", (developer["id"],))[0]["n"]
            developer["feedback_count"] = store.query("SELECT COUNT(*) n FROM items WHERE kind IN ('review','topic') AND app_id IN (SELECT app_id FROM developer_games WHERE developer_id=?)", (developer["id"],))[0]["n"]
            presentation = store.presentation(developer["id"])
            avatar = presentation["source"].get("avatar") or {}
            developer["logo"] = presentation["custom"].get("logo_url") or avatar.get("original_url") or avatar.get("url") or "/favicon.svg"
        return sorted(result, key=lambda developer: not developer["is_primary"])

    @api.post("/developers", status_code=201)
    async def add_developer(body: DeveloperCreate):
        if store.developer(body.id):
            raise HTTPException(409, "Developer already subscribed")
        store.add_developer(body.id, body.name, body.target_umo)
        store.save_setting("removed_developers", [d for d in store.setting("removed_developers", []) if d != body.id])
        if scheduler_enabled:
            if scan_task and not scan_task.done():
                pending_developers[body.id] = "full"
            else:
                start_scan(body.id, "full")
        return store.developer(body.id)

    @api.delete("/developers/{developer_id}")
    async def delete_developer(developer_id: int):
        if not store.developer(developer_id):
            raise HTTPException(404, "Developer not found")
        if scan_task and not scan_task.done():
            raise HTTPException(409, "A collection run is already active")
        pending_developers.pop(developer_id, None)
        return store.delete_developer(developer_id)

    @api.patch("/developers/{developer_id}")
    async def update_developer(developer_id: int, body: DeveloperUpdate):
        if not store.developer(developer_id):
            raise HTTPException(404, "Developer not found")
        fields = body.model_dump(exclude_none=True)
        store.update_developer(developer_id, **fields)
        return store.developer(developer_id)

    @api.get("/games")
    async def games(developer_id: int | None = Query(None, gt=0)):
        games = store.games(developer_id)
        return primary_games_first(games) if developer_id is None else games

    @api.get("/official")
    async def official_status():
        return official.status()

    @api.post("/official/auth", status_code=202)
    async def official_auth():
        try:
            return await official.begin_auth()
        except OfficialError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.delete("/official/auth")
    async def official_logout():
        try:
            return await official.logout()
        except OfficialError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/official/sync", status_code=202)
    async def official_sync():
        try:
            return official.start_sync()
        except OfficialError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/console")
    async def console_status():
        return console.status()

    @api.put("/console")
    async def console_configure(body: ConsoleSession):
        try:
            return await console.configure(body.cookie)
        except ConsoleError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/console/login", status_code=202)
    async def console_login():
        return await console.begin_login()

    @api.post("/console/helper")
    async def console_helper(request: Request):
        # The helper reports back to the address the admin is using; nginx passes Host and X-Forwarded-Proto.
        proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
        host = request.headers.get("host", "")
        if proto not in ("http", "https") or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,255}", host):
            raise HTTPException(400, "无法确定服务器地址")
        code = await console.begin_helper()
        script = helper_script(f"{proto}://{host}", code).encode("utf-8")
        return Response(script, media_type="application/octet-stream",
                        headers={"Content-Disposition": 'attachment; filename="TapTapLoginHelper.bat"'})

    @api.delete("/console")
    async def console_clear():
        return await console.clear()

    @api.post("/console/sync", status_code=202)
    async def console_sync(full: bool = False):
        try:
            return console.start_sync(full)
        except ConsoleError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/metrics")
    async def metrics(days: int = Query(30, ge=7, le=365), developer_id: int | None = Query(None, gt=0),
                      app_id: int | None = Query(None, gt=0)):
        result = official.metrics(days, developer_id, app_id)
        ids = [game["app_id"] for game in result["games"]]
        # Console rows use China dates, which can be a day ahead of the server's local date.
        rows = store.metrics(ids, "console", result["days"][0])
        for game in result["games"]:
            game["console_daily"] = [{key: value for key, value in row.items() if key != "app_id"}
                                     for row in rows if row["app_id"] == game["app_id"]]
        if developer_id is None:
            result["games"] = primary_games_first(result["games"], "app_id")
        return result

    @api.get("/items/{app_id}/{kind}/{source_id}/replies")
    async def replies(app_id: int, kind: Literal["review", "topic"], source_id: str,
                      limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        if store.get_item(app_id, kind, source_id) is None:
            raise HTTPException(404, "未找到对应的评价或帖子")
        return store.replies(app_id, kind, source_id, limit, offset)

    @api.get("/items")
    async def items(developer_id: int | None = None, app_id: int | None = None,
                    kind: str | None = Query(None, pattern="^(review|topic|review_comment|topic_comment)$"),
                    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        return store.items(developer_id, app_id, kind, limit, offset)

    @api.get("/items/search")
    async def search_items(developer_id: int | None = Query(None, gt=0), app_id: int | None = Query(None, gt=0),
                           kind: str | None = Query(None, pattern="^(review|topic|review_comment|topic_comment)$"),
                           score: int | None = Query(None, ge=1, le=5),
                           min_score: int | None = Query(None, ge=1, le=5), max_score: int | None = Query(None, ge=1, le=5),
                           q: str | None = Query(None, max_length=1000), since: int | None = None, until: int | None = None,
                           official: bool | None = None,
                           sort: str = Query("newest", pattern="^(newest|oldest|score_desc|score_asc)$"),
                           limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        if min_score is not None and max_score is not None and min_score > max_score:
            raise HTTPException(422, "最低评分不能高于最高评分")
        if since is not None and until is not None and since > until:
            raise HTTPException(422, "开始时间不能晚于结束时间")
        return store.search_items(developer_id, app_id, kind, score, min_score, max_score, q, since, until, official, sort, limit, offset)

    @api.get("/stats")
    async def stats(developer_id: int | None = Query(None, gt=0), app_id: int | None = Query(None, gt=0),
                    days: int = Query(30, ge=7, le=365)):
        result = store.stats(developer_id, app_id, days)
        if developer_id is None:
            result["games"] = primary_games_first(result["games"], "app_id")
        return result

    @api.get("/insights")
    async def insights(days: int = Query(30, ge=7, le=365), developer_id: int | None = Query(None, gt=0),
                       app_id: int | None = Query(None, gt=0)):
        result = analyze(store, days, developer_id, app_id)
        if developer_id is None:
            result["growth"] = primary_games_first(result["growth"], "app_id")
        return result

    def effective_settings():
        return {"values": {key: getattr(settings, key) for key in RUNTIME_FIELDS},
                "defaults": defaults, "overrides": overrides.copy(),
                "info": {"astrbot_configured": bool(settings.astrbot_url and settings.astrbot_key),
                         "database": str(Path(settings.database).resolve()), "version": app.version}}

    @api.get("/settings")
    async def get_settings():
        return effective_settings()

    @api.patch("/settings")
    async def update_settings(body: RuntimeUpdate):
        updated = overrides.copy()
        for key, value in body.model_dump(exclude_unset=True).items():
            if value is None:
                updated.pop(key, None)
            else:
                updated[key] = value
        # Create the cache before persisting or changing the shared settings object.
        cache = collector.media
        if updated.get("cache_images", defaults["cache_images"]) and cache is None:
            cache = MediaCache(Path(settings.database).parent / "media")
        store.save_setting("runtime", updated)
        overrides.clear()
        overrides.update(updated)
        for key in RUNTIME_FIELDS:
            setattr(settings, key, overrides.get(key, defaults[key]))
        collector.media = cache if settings.cache_images else None
        return effective_settings()

    @api.get("/system")
    async def system_status():
        return updater.status()

    def upgrade_call(action, *args):
        try:
            action(*args)
        except UpgradeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return updater.status()

    @api.post("/system/check", status_code=202)
    async def system_check():
        return upgrade_call(updater.request, "check")

    @api.post("/system/upgrade", status_code=202)
    async def system_upgrade(body: UpgradeRequest):
        return upgrade_call(updater.request, "apply", body.ref)

    @api.put("/system/auto")
    async def system_auto(body: AutoUpgrade):
        return upgrade_call(updater.set_auto, body.enabled)

    @api.get("/events")
    async def events(after: int = Query(0, ge=0), developer_id: int | None = None,
                     limit: int = Query(100, ge=1, le=500), include_baseline: bool = False):
        rows = store.events(after, developer_id, limit, include_baseline)
        return {"events": rows, "next_cursor": rows[-1]["id"] if rows else after, "has_more": bool(rows and store.events(rows[-1]["id"], developer_id, 1, include_baseline))}

    @api.post("/scan", status_code=202)
    async def scan(developer_id: int | None = None, mode: Literal["incremental", "full"] = "incremental"):
        if developer_id is not None and not store.developer(developer_id):
            raise HTTPException(404, "Developer not found")
        return {"run_id": start_scan(developer_id, mode), "mode": mode}

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
            structured_data = {
                "@context": "https://schema.org",
                "@graph": [
                    {"@type": "Organization", "name": data["name"], "logo": data["logo"],
                     "url": f"/studio/{developer_id}", "description": data["intro"]},
                    {"@type": "ItemList", "numberOfItems": len(data["games"]), "itemListElement": [
                        {"@type": "ListItem", "position": position, "item": {
                            "@type": "VideoGame", "name": game["title"], "image": game["cover"],
                            "url": f"/studio/{developer_id}/games/{game['id']}"}}
                        for position, game in enumerate(data["games"], 1)
                    ]},
                ],
            }
            replacements = {
                "__PAGE_TITLE__": escape(title), "__PAGE_DESCRIPTION__": escape(description[:200], quote=True),
                "__STUDIO_NAME__": escape(data["name"]), "__STUDIO_TAGLINE__": escape(data["tagline"]),
                "__STUDIO_LOGO__": escape(data["logo"], quote=True),
                "__OG_IMAGE__": escape((selected["cover"] if selected else "") or data["logo"], quote=True),
                "__SITE_DATA__": json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026"),
                "__JSON_LD__": json.dumps(structured_data, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026"),
            }
            template = re.sub("|".join(replacements), lambda match: replacements[match[0]], template)
            return HTMLResponse(template, headers={"Cache-Control": "no-cache"})

        @app.get("/", response_class=HTMLResponse)
        async def home():
            if primary_site() is None:
                return HTMLResponse(WELCOME_PAGE, headers={"Cache-Control": "no-cache"})
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
