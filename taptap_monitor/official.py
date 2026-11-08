"""Bridge to TapTap's official open API through the official MCP server.

TapTap ships its open API (agent.tapapis.cn) as the npm package
``@taptap/instant-games-open-mcp``; request signing lives in a native module
inside that package. We run the package unmodified as a long-lived child
process (stdio transport, raw JSON tools enabled) and talk MCP JSON-RPC to it.
Authorization is TapTap's device-code flow: the admin scans a QR code with the
TapTap app; the resulting token is stored by the package in our data dir.
"""
import asyncio
import base64
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import json
import logging
import math
import os
import re
import shutil
from pathlib import Path
import time

import segno

log = logging.getLogger(__name__)

DEFAULT_SERVER = "/opt/taptap-monitor/official/node_modules/@taptap/instant-games-open-mcp/dist/server.js"
PROTOCOL = "2025-06-18"


class OfficialError(Exception):
    pass


class OfficialBridge:
    def __init__(self, data_dir: Path, server: str | None = None, node: str | None = None, timeout: float = 60):
        self.home = Path(data_dir).resolve() / "official"
        self.server = str(Path(server or os.environ.get("TAPTAP_OFFICIAL_SERVER") or DEFAULT_SERVER).resolve())
        executable = node or os.environ.get("TAPTAP_OFFICIAL_NODE") or "node"
        self.node = shutil.which(executable) or (str(Path(executable).resolve()) if Path(executable).is_file() else executable)
        self.timeout = timeout
        self.process = None
        self.reader = None
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 0
        self.lock = asyncio.Lock()  # tool calls share the package's "selected app" state
        self.start_lock = asyncio.Lock()

    @property
    def installed(self) -> bool:
        return Path(self.server).is_file() and bool(shutil.which(self.node) or Path(self.node).is_file())

    def environment(self):
        for name in ("cache", "temp", "logs"):
            (self.home / name).mkdir(parents=True, exist_ok=True)
        keep = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG") if key in os.environ}
        return {**keep, "HOME": str(self.home), "USERPROFILE": str(self.home),
                "TAPTAP_MCP_TRANSPORT": "stdio", "TAPTAP_MCP_ENABLE_RAW_TOOLS": "true",
                "TAPTAP_MCP_CACHE_DIR": str(self.home / "cache"), "TAPTAP_MCP_TEMP_DIR": str(self.home / "temp"),
                "TAPTAP_MCP_LOG_ROOT": str(self.home / "logs"), "TAPTAP_MCP_WORKSPACE_ROOT": str(self.home)}

    async def start(self):
        async with self.start_lock:
            if self.process and self.process.returncode is None:
                return
            if not self.installed:
                raise OfficialError("官方接口组件未安装")
            if self.reader:
                await self.reader
            try:
                self.process = await asyncio.create_subprocess_exec(
                    self.node, self.server, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env=self.environment(), cwd=str(self.home), limit=16 * 1024 * 1024)
            except OSError as exc:
                raise OfficialError("无法启动官方接口组件") from exc
            self.reader = asyncio.create_task(self.read_loop(self.process))
            try:
                await self.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                              "clientInfo": {"name": "taptap-monitor", "version": "1"}})
                await self.notify("notifications/initialized")
            except BaseException:
                await self.close()
                raise

    async def read_loop(self, process):
        try:
            while line := await process.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                future = self.pending.pop(message.get("id"), None) if isinstance(message, dict) else None
                if future and not future.done():
                    future.set_result(message)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(OfficialError("官方接口组件已退出"))
            self.pending.clear()

    async def send(self, message):
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.process.stdin.drain()

    async def notify(self, method, params=None):
        await self.send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    async def rpc(self, method, params, timeout=None):
        self.next_id += 1
        request_id, future = self.next_id, asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            message = await asyncio.wait_for(future, timeout or self.timeout)
        except asyncio.TimeoutError as exc:
            # A timed-out select/report must not keep running while the next request changes apps.
            await self.close()
            raise OfficialError(f"官方接口响应超时：{method}") from exc
        except (OSError, RuntimeError) as exc:
            raise OfficialError("官方接口组件连接中断") from exc
        finally:
            self.pending.pop(request_id, None)
        if "error" in message:
            raise OfficialError(message["error"].get("message") or str(message["error"]))
        return message.get("result") or {}

    async def call(self, tool, arguments=None, timeout=None):
        async with self.lock:
            await self.start()
            result = await self.rpc("tools/call", {"name": tool, "arguments": arguments or {}}, timeout)
        data = result.get("structuredContent")
        if data is None:
            text = "".join(part.get("text", "") for part in result.get("content", []) if part.get("type") == "text")
            try:
                data = json.loads(text)
            except ValueError:
                data = {"ok": not result.get("isError"), "message": text}
        if result.get("isError") or (isinstance(data, dict) and data.get("ok") is False):
            message = (data.get("message") or data.get("error")) if isinstance(data, dict) else None
            raise OfficialError(str(message or f"{tool} 调用失败")[:500])
        return data

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        if self.reader:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader
        self.process = None
        self.reader = None


SUMMARY_FIELDS = ("page_view_count", "download_count", "reserve_count", "download_request_count", "rating_score",
                  "positive_review_count", "neutral_review_count", "negative_review_count", "topic_count",
                  "topic_page_view_count", "favorite_count", "feed_count")
REPORT_KINDS = ("store_overview", "review_overview", "community_overview", "store_snapshot")
DATE_KEYS = ("date", "day", "stat_date", "dt", "time", "timestamp")


def metric_day(value):
    try:
        value = str(value)
        if re.fullmatch(r"\d{8}", value):
            return datetime.strptime(value, "%Y%m%d").date().isoformat()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value).isoformat()
        stamp = float(value)
        return datetime.fromtimestamp(stamp / 1000 if stamp > 1e11 else stamp, timezone.utc).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def daily_metrics(data):
    for key, rows in data.items():
        if key != "trend" and not key.endswith("_trend"):
            continue
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            day = next((parsed for name in DATE_KEYS if (parsed := metric_day(row.get(name))) is not None), None)
            values = {(f"{key}_{name}" if name in {"value", "count", "num"} else name): value
                      for name, value in row.items() if name not in DATE_KEYS and type(value) in (int, float) and math.isfinite(value)}
            if day and values:
                yield day, values


def report_data(payload):
    return payload.get("data", payload) if isinstance(payload, dict) else {}


def account_apps(payload):
    """The package merges level/non-level lists into list[].apps (levels/crafts are aliases)."""
    data = report_data(payload)
    developers = data if isinstance(data, list) else data.get("list", data.get("developers", []))
    apps = {}
    for developer in developers or []:
        if not isinstance(developer, dict):
            continue
        for app in developer.get("apps") or developer.get("levels") or developer.get("crafts") or []:
            try:
                developer_id = int(app.get("developer_id") or developer.get("developer_id") or developer.get("id"))
                app_id = int(app.get("app_id") or app.get("id"))
            except (AttributeError, TypeError, ValueError):
                continue
            apps[(developer_id, app_id)] = {"developer_id": developer_id,
                "developer_name": str(developer.get("developer_name") or developer.get("name") or ""),
                "app_id": app_id, "title": str(app.get("app_title") or app.get("display_app_title") or app.get("title") or app.get("name") or app_id)}
    return list(apps.values())


class OfficialService:
    def __init__(self, store, bridge):
        self.store, self.bridge = store, bridge
        saved = store.setting("official", {})
        self.state = {key: saved.get(key, default) for key, default in
                      dict(authorized=False, account=None, apps=[], synced_at=None, error=None).items()}
        self.auth = self.idle_auth()
        self.auth_task = self.sync_task = None
        self.control = asyncio.Lock()
        self.sync_lock = asyncio.Lock()

    @staticmethod
    def idle_auth():
        return dict(status="idle", qr=None, auth_url=None, expires_at=None, message=None)

    def persist(self):
        self.store.save_setting("official", self.state)

    def status(self):
        monitored = {game["id"] for game in self.store.games()}
        return {"installed": self.bridge.installed, **self.state, "auth": self.auth.copy(),
                "apps": [{**app, "monitored": app["app_id"] in monitored} for app in self.state["apps"]],
                "syncing": self.sync_lock.locked() or bool(self.sync_task and not self.sync_task.done())}

    def failure(self, exc):
        message = str(exc)
        if re.search(r"unauthori[sz]ed|\b401\b|(?:invalid|expired).{0,20}token|token.{0,20}(invalid|expired|missing|not configured)|未授权|未认证|认证失败|token.*(失效|过期)|(?:请|需要).*授权", message, re.I):
            self.state["authorized"] = False
            message = "官方授权已失效，请重新扫码授权"
        self.state["error"] = message[:500]
        self.persist()

    async def refresh_account(self):
        payload = await self.bridge.call("list_developers_and_apps_raw")
        data = report_data(payload)
        account = (data.get("account") or data.get("user")) if isinstance(data, dict) else None
        self.state.update(authorized=True, apps=account_apps(payload), account=None, error=None)
        if isinstance(account, dict):
            avatar = account.get("avatar")
            if isinstance(avatar, dict):
                avatar = avatar.get("original_url") or avatar.get("url")
            self.state["account"] = {"name": account.get("name") or account.get("nickname"),
                                     "avatar": avatar if isinstance(avatar, str) else None}
        self.persist()

    async def begin_auth(self):
        async with self.control:
            if not self.bridge.installed:
                raise OfficialError("官方接口组件未安装")
            if self.auth_task and not self.auth_task.done():
                return self.status()
            try:
                data = await self.bridge.call("start_oauth_authorization_raw")
                if data.get("authorized"):
                    await self.refresh_account()
                    self.auth = {**self.idle_auth(), "status": "success", "message": "官方账号已授权"}
                else:
                    self.state.update(authorized=False, account=None, apps=[], error=None)
                    self.persist()
                    qr = segno.make(data["qrcode_url"], micro=False)
                    svg = BytesIO()
                    qr.save(svg, kind="svg", scale=max(1, round(200 / qr.symbol_size()[0])), light="white", dark="black")
                    self.auth = dict(status="waiting", qr="data:image/svg+xml;base64," + base64.b64encode(svg.getvalue()).decode(),
                                     auth_url=data.get("auth_url"), expires_at=time.time() + float(data.get("expires_in", 300)),
                                     message="请使用 TapTap App 扫码授权")
                    self.auth_task = asyncio.create_task(self.poll_auth(max(0.01, float(data.get("interval", 2)))))
            except Exception as exc:
                self.failure(exc)
                self.auth = {**self.idle_auth(), "status": "failed", "message": "获取官方授权失败，请重试"}
            return self.status()

    async def poll_auth(self, interval):
        try:
            while (remaining := self.auth["expires_at"] - time.time()) > 0:
                await asyncio.sleep(min(interval, remaining))
                remaining = self.auth["expires_at"] - time.time()
                if remaining <= 0:
                    break
                try:
                    data = await self.bridge.call("complete_oauth_authorization_raw", timeout=min(remaining, 150))
                except OfficialError as exc:
                    # Upstream gA polls for two minutes per call; its pending state survives that timeout.
                    if time.time() >= self.auth["expires_at"]:
                        break
                    if re.search(r"pending|waiting|授权超时", str(exc), re.I):
                        continue
                    raise
                if data.get("authorized"):
                    await self.refresh_account()
                    self.auth = {**self.idle_auth(), "status": "success", "message": "官方账号授权成功"}
                    return
                if data.get("error") not in (None, "authorization_pending", "authorization_waiting"):
                    raise OfficialError(data.get("message") or data["error"])
            self.auth = {**self.idle_auth(), "status": "expired", "message": "授权二维码已过期，请重新扫码"}
        except Exception as exc:
            self.failure(exc)
            expired = bool(re.search(r"expired|过期", str(exc), re.I))
            self.auth = {**self.idle_auth(), "status": "expired" if expired else "failed",
                         "message": "授权二维码已过期，请重新扫码" if expired else "官方授权失败，请重新扫码"}

    async def cancel_tasks(self):
        for task in (self.auth_task, self.sync_task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def logout(self):
        async with self.control:
            await self.cancel_tasks()
            # Abort outstanding upstream polls before clearing disk credentials.
            await self.bridge.close()
            try:
                if self.bridge.installed:
                    await self.bridge.call("clear_auth_data_raw", {"clear_token": True, "clear_cache": True})
            except Exception as exc:
                self.failure(exc)
                self.auth = {**self.idle_auth(), "status": "failed", "message": "清除官方授权失败，请重试"}
                raise OfficialError("清除官方授权失败，请重试") from exc
            finally:
                # clear_auth_data does not clear the package's in-memory pending device code.
                await self.bridge.close()
            self.state = dict(authorized=False, account=None, apps=[], synced_at=None, error=None)
            self.auth = self.idle_auth()
            self.persist()
            return self.status()

    def start_sync(self):
        if not self.bridge.installed or not self.state["authorized"]:
            raise OfficialError("请先安装官方组件并扫码授权")
        if self.control.locked() or self.sync_lock.locked() or (self.sync_task and not self.sync_task.done()):
            raise OfficialError("官方数据正在同步")
        self.sync_task = asyncio.create_task(self.sync())
        return self.status()

    def sync_if_due(self):
        if (self.bridge.installed and self.state["authorized"] and not self.control.locked() and not self.sync_lock.locked()
                and not (self.sync_task and not self.sync_task.done())
                and time.time() - (self.state["synced_at"] or 0) >= 6 * 3600):
            self.start_sync()

    async def sync(self):
        async with self.sync_lock:
            if not self.bridge.installed or not self.state["authorized"]:
                return
            errors = []
            try:
                await self.refresh_account()
                monitored = {game["id"] for game in self.store.games()}
                today = date.today()
                window = {"start_date": (today - timedelta(days=29)).isoformat(), "end_date": today.isoformat()}
                seen = set()
                for app in self.state["apps"]:
                    app_id = app["app_id"]
                    if app_id not in monitored or app_id in seen:
                        continue
                    seen.add(app_id)
                    try:
                        await self.bridge.call("select_app_raw", {"developer_id": app["developer_id"], "app_id": app_id})
                    except Exception as exc:
                        self.failure(exc)
                        errors.append(f"游戏 {app_id}：{self.state['error']}")
                        if not self.state["authorized"]:
                            break
                        continue
                    for kind in REPORT_KINDS:
                        try:
                            payload = await self.bridge.call(f"get_current_app_{kind}_raw", {} if kind == "store_snapshot" else window)
                            self.store.save_report(app_id, kind, payload)
                            for day, values in daily_metrics(report_data(payload)):
                                self.store.save_metrics(app_id, "official", values, day)
                        except Exception as exc:
                            self.failure(exc)
                            errors.append(f"游戏 {app_id} {kind}：{self.state['error']}")
                            if not self.state["authorized"]:
                                break
                    if not self.state["authorized"]:
                        break
                # TODO official_reviews: add get_current_app_reviews_raw after verifying its review envelope.
            except Exception as exc:
                self.failure(exc)
                errors.append(self.state["error"])
            self.state.update(synced_at=time.time(), error="；".join(errors)[:500] or None)
            self.persist()

    def metrics(self, days=30, developer_id=None, app_id=None):
        today = date.today()
        window = [(today - timedelta(days=offset)).isoformat() for offset in reversed(range(days))]
        games = [game for game in self.store.games(developer_id) if app_id is None or game["id"] == app_id]
        ids = [game["id"] for game in games]
        public = self.store.metrics(ids, "public", window[0])
        official = self.store.metrics(ids, "official", window[0])
        entitled = {app["app_id"] for app in self.state["apps"]} if self.state["authorized"] else set()
        result = []
        for game in games:
            reports = self.store.reports(game["id"]) if game["id"] in entitled else {}
            summary = {key: None for key in SUMMARY_FIELDS} if reports else {}
            for kind in REPORT_KINDS:
                data = report_data(reports.get(kind, {}).get("data", {}))
                summary.update({key: data[key] for key in SUMMARY_FIELDS if data.get(key) is not None})
            icon = game["metadata"].get("icon")
            if isinstance(icon, dict):
                icon = icon.get("original_url") or icon.get("url")
            def rows(source):
                return [{key: value for key, value in row.items() if key != "app_id"}
                        for row in source if row["app_id"] == game["id"] and row["day"] <= window[-1]]
            result.append(dict(app_id=game["id"], title=game["title"], icon=icon if isinstance(icon, str) else None, official=bool(reports),
                               official_fetched_at=max((report["fetched_at"] for report in reports.values()), default=None),
                               summary=summary, official_daily=rows(official) if reports else [], public_daily=rows(public)))
        return {"days": window, "games": result}

    async def close(self):
        await self.cancel_tasks()
        await self.bridge.close()
