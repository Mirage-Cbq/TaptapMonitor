"""Developer-console figures the official open API does not expose.

The TapTap developer console (developer.taptap.cn) shows the store exposure
funnel, mini-game active devices/retention and estimated ad revenue. These
come from the console's own JSON endpoints, authenticated by the admin's
browser session cookie. The cookie is pasted in the admin UI, stored only in
the local database and never returned to the browser.

These are internal endpoints: they may change without notice. Every failure is
recorded and an expired session is reported so the admin can paste a new one.
"""
import asyncio
import base64
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from io import BytesIO
import logging
import math
import re
import time
from urllib.parse import parse_qs, urlsplit
import uuid

import httpx
import segno

log = logging.getLogger(__name__)

ROOT = "https://developer.taptap.cn"
ACCOUNTS = "https://accounts.taptap.cn"
HOST = "developer.taptap.cn"
BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
CHINA = timezone(timedelta(hours=8))  # console dates are China calendar days
SYNC_SECONDS = 2 * 3600
WINDOW_DAYS = 30
RETENTION = (("ret_new_active_device_rate_a1d", "retention_1d", 1),
             ("ret_new_active_device_rate_a3d", "retention_3d", 3),
             ("ret_new_active_device_rate_a7d", "retention_7d", 7))


class ConsoleError(Exception):
    pass


class ConsoleAuthError(ConsoleError):
    pass


class LoginStep(ConsoleError):
    """An accounts.taptap.cn answer that is not success, e.g. authorization_pending."""

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


def qr_data_uri(text, size=200):
    qr = segno.make(text, micro=False)
    svg = BytesIO()
    qr.save(svg, kind="svg", scale=max(1, round(size / qr.symbol_size()[0])), light="white", dark="black")
    return "data:image/svg+xml;base64," + base64.b64encode(svg.getvalue()).decode()


def serves(cookie_domain, host=HOST):
    domain = cookie_domain.lstrip(".")
    return host == domain or host.endswith("." + domain)


def clean_cookie(value):
    """Accept a bare cookie string or a pasted `Cookie: ...` header line."""
    value = re.sub(r"^\s*cookie\s*:\s*", "", str(value or ""), flags=re.I).strip()
    value = re.sub(r"[\r\n]+", " ", value)
    if not value or "=" not in value or len(value) > 16384:
        raise ConsoleError("请粘贴开发者后台请求里的完整 Cookie")
    return value


def number(value):
    """Console figures arrive as ints, "9.53%" strings or "82.05" strings."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    text = str(value).strip().replace(",", "")
    try:
        result = float(text[:-1]) / 100 if text.endswith("%") else float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def rows(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    values = data.get("list") if isinstance(data, dict) else None
    return [row for row in values or [] if isinstance(row, dict) and isinstance(row.get("date"), str)]


def daily_figures(funnel=(), pages=(), devices=(), retention=(), revenue=(), today=None):
    """Merge each endpoint's rows into {day: {metric: value}}."""
    days = {}

    def put(day, key, value):
        if value is not None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            days.setdefault(day, {})[key] = value

    for row in funnel:
        put(row["date"], "impressions", number(row.get("impression_cnt")))
        put(row["date"], "detail_visits", number(row.get("click_cnt")))
        put(row["date"], "click_rate", number(row.get("click_rate")))
        put(row["date"], "launches", number(row.get("convert_detail_cnt")))
        put(row["date"], "launch_rate", number(row.get("convert_detail_rate")))
    for row in pages:
        put(row["date"], "store_page_views", number(row.get("pv_from_total")))
    for row in devices:
        put(row["date"], "active_devices", number(row.get("active_device_num")))
        put(row["date"], "new_devices", number(row.get("new_active_device_num")))
    today = today or datetime.now(CHINA).date()
    for row in retention:
        for source, key, lag in RETENTION:
            # A cohort's N-day retention is 0% until N days have passed; skip immature cells.
            with suppress(ValueError):
                if (today - datetime.strptime(row["date"], "%Y-%m-%d").date()).days > lag:
                    put(row["date"], key, number(row.get(source)))
    for row in revenue:
        put(row["date"], "ad_revenue", number(row.get("revenue")))
    return days


class ConsoleClient:
    def __init__(self, cookie, transport=None, timeout=20):
        self.http = httpx.AsyncClient(base_url=ROOT, timeout=timeout, transport=transport, follow_redirects=False, headers={
            "User-Agent": BROWSER, "Accept": "application/json", "Referer": ROOT + "/", "Cookie": cookie})

    async def close(self):
        await self.http.aclose()

    async def get(self, path, **params):
        try:
            response = await self.http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ConsoleError(f"无法连接开发者后台：{type(exc).__name__}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = None
        error = payload.get("data") if isinstance(payload, dict) and payload.get("success") is False else None
        error = error if isinstance(error, dict) else {}
        message = error.get("msg") or error.get("error")
        if response.status_code in (401, 403) or error.get("error") == "access_denied":
            raise ConsoleAuthError("开发者后台登录已失效，请重新粘贴 Cookie")
        if response.status_code >= 400 or not isinstance(payload, dict) or payload.get("success") is False:
            raise ConsoleError(f"开发者后台接口异常（HTTP {response.status_code}{'：' + str(message) if message else ''}）")
        return payload

    async def account(self):
        me = await self.get("/api/user/v1/me")
        user = (me.get("data") or {}).get("user") or {}
        listing = await self.get("/api/developer/v1/list")
        data = listing.get("data")
        developers = data if isinstance(data, list) else (data or {}).get("list", [])
        found = []
        for developer in developers or []:
            with suppress(AttributeError, TypeError, ValueError):
                found.append({"id": int(developer.get("id") or developer.get("developer_id")),
                              "name": str(developer.get("name") or developer.get("developer_name") or "")})
        return {"name": user.get("name") or user.get("nickname"), "developers": found}

    async def apps(self, developer_id):
        result, page = [], 1
        while page <= 20:
            payload = await self.get("/api/app/v2/list", developer_id=developer_id, page=page, pagesize=40)
            data = payload.get("data") or {}
            batch = [app for app in data.get("list") or [] if isinstance(app, dict) and app.get("id")]
            result += [{"app_id": int(app["id"]), "title": str(app.get("title") or app["id"])} for app in batch]
            if len(batch) < 40 or len(result) >= int(data.get("total") or 0):
                return result
            page += 1
        return result

    async def figures(self, developer_id, app_id, start, end, today=None):
        scope = {"developer_id": developer_id, "app_id": app_id}
        window = {**scope, "start_date": start.isoformat(), "end_date": end.isoformat()}
        funnel = rows(await self.get("/api/dashboard/v2/stats-by-day/position/cn", **window))
        pages = rows(await self.get("/api/dashboard/v3/stats-by-day/pv/cn", **window))

        async def optional(path, **params):
            # Mini-game and ad endpoints only apply to some apps; their absence must not drop the funnel.
            try:
                return await self.get(path, **params)
            except ConsoleAuthError:
                raise
            except ConsoleError:
                return {}

        devices = rows(await optional("/api/mini-app/v1/stats-by-day/device", **window))
        retention = rows(await optional("/api/mini-app/v1/stats-by-day/ret", **window))
        revenue = []
        ad = await optional("/api/mini-app/v1/ad/info", **scope)
        if (ad.get("data") or {}).get("is_enabled"):  # only the flag is read; the rest holds payee details
            revenue = rows(await self.get("/api/mini-app/v1/ad/payout-report-data", **scope,
                                          start_time=start.isoformat(), end_time=end.isoformat()))
        return daily_figures(funnel, pages, devices, retention, revenue, today)


class TapLogin:
    """TapTap web login by QR code, then the console's own OAuth handshake.

    1. accounts device code -> QR scanned and confirmed in the TapTap app -> accounts session cookies
    2. console /api/auth/login/url -> accounts authorize parameters (state bound to DC_XSRF_TOKEN)
    3. accounts /api/oauth2/auth/v2 (approve/v2 on first use) -> redirect_uri with code
    4. console /api/auth/login with code -> console session cookies
    Steps 2-4 also renew an expired console session from a still-valid accounts session.
    """

    def __init__(self, transport=None, jar=()):
        self.session_id = str(uuid.uuid4())
        self.xua = f"V=1&PN=Accounts&LANG=zh_CN&VN_CODE=10&LOC=CN&PLT=PC&DS=Android&UID={uuid.uuid4()}&OS=Windows&OSV=10&DT=PC"
        self.http = httpx.AsyncClient(timeout=20, transport=transport, follow_redirects=False,
                                      headers={"User-Agent": BROWSER, "Accept": "application/json"})
        for cookie in jar or ():
            self.http.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie.get("path") or "/")

    async def close(self):
        await self.http.aclose()

    @staticmethod
    def parse(response, what):
        try:
            payload = response.json()
        except ValueError:
            raise ConsoleError(f"{what}返回了非 JSON 内容（HTTP {response.status_code}）") from None
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and payload.get("success") is True:
            return data if isinstance(data, dict) else {}
        data = data if isinstance(data, dict) else {}
        raise LoginStep(str(data.get("error") or f"http_{response.status_code}"), str(data.get("msg") or ""))

    async def accounts(self, method, path, **kwargs):
        params = {**kwargs.pop("params", {}), "X-UA": self.xua}
        headers = {"X-UA": self.xua, "x-requested-with": "XMLHttpRequest", "Origin": ACCOUNTS, "Referer": ACCOUNTS + "/login"}
        try:
            response = await self.http.request(method, ACCOUNTS + path, params=params, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ConsoleError(f"无法连接 TapTap 账号服务：{type(exc).__name__}") from exc
        return self.parse(response, "TapTap 账号服务")

    async def console(self, method, path, **kwargs):
        try:
            response = await self.http.request(method, ROOT + path, **kwargs)
        except httpx.HTTPError as exc:
            raise ConsoleError(f"无法连接开发者后台：{type(exc).__name__}") from exc
        return self.parse(response, "开发者后台")

    async def device_code(self):
        data = await self.accounts("POST", "/api/device-code/get", data={"session_id": self.session_id, "session_type": "taptap"})
        if not data.get("device_code") or not data.get("qrcode_url"):
            raise ConsoleError("TapTap 未返回登录二维码")
        return data

    async def poll(self, device_code):
        await self.accounts("POST", "/api/device-code/login",
                            data={"device_code": device_code, "session_id": self.session_id, "session_type": "taptap"})

    def cookie(self, name):
        return next((cookie.value for cookie in self.http.cookies.jar if cookie.name == name and serves(cookie.domain)), None)

    async def console_session(self):
        login = await self.console("GET", "/api/auth/login/url", params={"host": HOST, "refer": "/v3/", "path_prefix": "/v3"})
        query = urlsplit(str(login.get("url") or "")).query
        if not query:
            raise ConsoleError("开发者后台未返回授权地址")
        try:
            grant = await self.accounts("GET", "/api/oauth2/auth/v2", params={"parameters": query})
        except LoginStep as exc:
            if exc.code in ("access_denied", "http_401", "unauthorized"):
                raise ConsoleAuthError("TapTap 账号登录已失效，请重新扫码") from exc
            raise
        redirect = grant.get("redirect_uri")
        if not redirect:  # first authorization of the developer console for this account
            client = grant.get("client") or {}
            approved = await self.accounts("POST", "/api/oauth2/approve/v2", data={
                "grant_type": grant.get("grant_type") or "authorization_code", "client_id": client.get("id") or "",
                "scopes": "public_profile", "continuation_code": grant.get("continuation_code") or "", "parameters": query})
            redirect = approved.get("redirect_uri")
        answer = parse_qs(urlsplit(str(redirect or "")).query)
        if not answer.get("code"):
            raise ConsoleError("TapTap 未返回开发者后台授权码")
        await self.console("POST", "/api/auth/login", headers={"X-DC-XSRF-TOKEN": self.cookie("DC_XSRF_TOKEN") or ""},
                           json={"host": HOST, "state": answer.get("state", [""])[0], "code": answer["code"][0], "path_prefix": "/v3"})
        header = "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.http.cookies.jar if serves(cookie.domain))
        jar = [{"name": cookie.name, "value": cookie.value, "domain": cookie.domain, "path": cookie.path}
               for cookie in self.http.cookies.jar if serves(cookie.domain, "accounts.taptap.cn") or serves(cookie.domain)]
        return header, jar


EMPTY = dict(cookie=None, jar=[], method=None, valid=False, account=None, developers=[], apps=[], synced_at=None, error=None)
SECRET = ("cookie", "jar")


class ConsoleService:
    """Holds the console session and syncs monitored games into metrics(source='console')."""

    def __init__(self, store, transport=None):
        self.store, self.transport = store, transport
        saved = store.setting("console", {})
        self.state = {key: saved.get(key, default) for key, default in EMPTY.items()}
        self.auth = self.idle_auth()
        self.task = self.login_task = None
        self.lock = asyncio.Lock()

    @staticmethod
    def idle_auth():
        return dict(status="idle", qr=None, expires_at=None, message=None)

    def persist(self):
        self.store.save_setting("console", self.state)

    def status(self):
        monitored = {game["id"] for game in self.store.games()}
        public = {key: value for key, value in self.state.items() if key not in SECRET}
        return {**public, "configured": bool(self.state["cookie"]), "syncing": self.syncing, "auth": self.auth.copy(),
                "apps": [{**app, "monitored": app["app_id"] in monitored} for app in self.state["apps"]]}

    @property
    def syncing(self):
        return self.lock.locked() or bool(self.task and not self.task.done())

    def client(self, cookie=None):
        return ConsoleClient(cookie or self.state["cookie"], self.transport)

    async def adopt(self, cookie, jar=(), method="cookie"):
        client = self.client(cookie)
        try:
            account = await client.account()
            apps = []
            for developer in account["developers"]:
                apps += [{**app, "developer_id": developer["id"], "developer_name": developer["name"]}
                         for app in await client.apps(developer["id"])]
        except ConsoleAuthError as exc:
            hint = "，请在已登录的开发者后台重新复制 Cookie" if method == "cookie" else "，请重新扫码"
            raise ConsoleError("登录信息无效或已过期" + hint) from exc
        finally:
            await client.close()
        self.state.update(cookie=cookie, jar=list(jar), method=method, valid=True, account={"name": account["name"]},
                          developers=account["developers"], apps=apps, error=None, synced_at=None)
        self.persist()
        if not self.syncing:
            self.start_sync()
        return self.status()

    async def configure(self, cookie):
        return await self.adopt(clean_cookie(cookie))

    async def begin_login(self):
        if self.login_task and not self.login_task.done():
            return self.status()
        login = TapLogin(self.transport)
        try:
            data = await login.device_code()
        except Exception as exc:
            await login.close()
            self.auth = {**self.idle_auth(), "status": "failed", "message": f"获取登录二维码失败：{exc}"[:200]}
            return self.status()
        expires = float(data.get("expires_in") or 300)
        self.auth = dict(status="waiting", qr=qr_data_uri(data["qrcode_url"]), expires_at=time.time() + expires,
                         message="请使用 TapTap App 扫码登录")
        interval = max(0.01, float(data.get("interval") or 2))
        self.login_task = asyncio.create_task(self.poll_login(login, data["device_code"], interval))
        return self.status()

    async def poll_login(self, login, device_code, interval):
        try:
            while time.time() < self.auth["expires_at"]:
                await asyncio.sleep(interval)
                try:
                    await login.poll(device_code)
                except LoginStep as exc:
                    if exc.code == "authorization_pending":
                        continue
                    if exc.code == "authorization_waiting":
                        self.auth.update(status="scanned", message="扫码成功，请在手机上确认登录")
                        continue
                    if exc.code == "slow_down":
                        interval = min(10, interval * 2)
                        continue
                    if exc.code == "access_denied":
                        self.auth = {**self.idle_auth(), "status": "failed", "message": "已在手机上取消登录"}
                        return
                    if exc.code in ("invalid_grant_code", "expired_token"):
                        break
                    raise
                self.auth.update(status="connecting", qr=None, message="登录成功，正在连接开发者后台…")
                cookie, jar = await login.console_session()
                await self.adopt(cookie, jar, method="qr")
                self.auth = {**self.idle_auth(), "status": "success", "message": "开发者后台已连接"}
                return
            self.auth = {**self.idle_auth(), "status": "expired", "message": "二维码已过期，请重新获取"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Console QR login failed: %s", exc)
            self.auth = {**self.idle_auth(), "status": "failed", "message": f"扫码登录失败：{exc}"[:200]}
        finally:
            await login.close()

    async def renew(self):
        """Mint a fresh console session from the stored accounts session; False if that is gone too."""
        if not self.state["jar"]:
            return False
        login = TapLogin(self.transport, self.state["jar"])
        try:
            self.state["cookie"], self.state["jar"] = await login.console_session()
            self.persist()
            return True
        except ConsoleError as exc:
            log.info("Console session renewal failed: %s", exc)
            return False
        finally:
            await login.close()

    async def clear(self):
        for task in (self.task, self.login_task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self.state = dict(EMPTY)
        self.auth = self.idle_auth()
        self.persist()
        return self.status()

    def start_sync(self):
        if not self.state["cookie"] or not self.state["valid"]:
            raise ConsoleError("请先扫码登录开发者后台")
        if self.syncing:
            raise ConsoleError("开发者后台数据正在同步")
        self.task = asyncio.create_task(self.sync())
        return self.status()

    def sync_if_due(self):
        if self.state["cookie"] and self.state["valid"] and not self.syncing \
                and time.time() - (self.state["synced_at"] or 0) >= SYNC_SECONDS:
            self.start_sync()

    async def collect(self, errors):
        client = self.client()
        today = datetime.now(CHINA).date()
        # Console dates are China calendar days; end_date may not be in the future, and "today" is included.
        start, end = today - timedelta(days=WINDOW_DAYS - 1), today
        try:
            monitored = {game["id"] for game in self.store.games()}
            for app in self.state["apps"]:
                if app["app_id"] not in monitored:
                    continue
                try:
                    days = await client.figures(app["developer_id"], app["app_id"], start, end, today)
                except ConsoleAuthError:
                    raise
                except ConsoleError as exc:
                    errors.append(f"{app['title']}：{exc}")
                    continue
                for day, values in days.items():
                    self.store.save_metrics(app["app_id"], "console", values, day)
        finally:
            await client.close()

    async def sync(self):
        async with self.lock:
            errors = []
            try:
                try:
                    await self.collect(errors)
                except ConsoleAuthError:
                    if not await self.renew():
                        raise
                    errors.clear()
                    await self.collect(errors)
            except ConsoleAuthError:
                self.state["valid"] = False
                again = "请重新扫码" if self.state["method"] == "qr" else "请重新粘贴 Cookie"
                errors.append("开发者后台登录已失效，" + again)
            except Exception as exc:  # keep the scheduler alive on unexpected payloads
                log.exception("Console sync failed")
                errors.append(f"同步失败：{type(exc).__name__}")
            self.state.update(synced_at=time.time(), error="；".join(errors)[:500] or None)
            self.persist()

    async def close(self):
        for task in (self.task, self.login_task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
