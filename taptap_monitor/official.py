"""Bridge to TapTap's official open API through the official MCP server.

TapTap ships its open API (agent.tapapis.cn) as the npm package
``@taptap/instant-games-open-mcp``; request signing lives in a native module
inside that package. We run the package unmodified as a long-lived child
process (stdio transport, raw JSON tools enabled) and talk MCP JSON-RPC to it.
Authorization is TapTap's device-code flow: the admin scans a QR code with the
TapTap app; the resulting token is stored by the package in our data dir.
"""
import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_SERVER = "/opt/taptap-monitor/official/node_modules/@taptap/instant-games-open-mcp/dist/server.js"
PROTOCOL = "2025-06-18"


class OfficialError(Exception):
    pass


class OfficialBridge:
    def __init__(self, data_dir: Path, server: str | None = None, node: str | None = None, timeout: float = 60):
        self.home = data_dir / "official"
        self.server = server or os.environ.get("TAPTAP_OFFICIAL_SERVER") or DEFAULT_SERVER
        self.node = node or os.environ.get("TAPTAP_OFFICIAL_NODE") or shutil.which("node") or "node"
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
            self.process = await asyncio.create_subprocess_exec(
                self.node, self.server, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=self.environment(), cwd=str(self.home), limit=16 * 1024 * 1024)
            self.reader = asyncio.create_task(self.read_loop(self.process))
            await self.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                          "clientInfo": {"name": "taptap-monitor", "version": "1"}})
            await self.notify("notifications/initialized")

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
        await self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            message = await asyncio.wait_for(future, timeout or self.timeout)
        except asyncio.TimeoutError as exc:
            self.pending.pop(request_id, None)
            raise OfficialError(f"官方接口响应超时：{method}") from exc
        if "error" in message:
            raise OfficialError(message["error"].get("message") or str(message["error"]))
        return message.get("result") or {}

    async def call(self, tool, arguments=None, timeout=None):
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
            message = data.get("message") or data.get("error") if isinstance(data, dict) else None
            raise OfficialError(str(message or f"{tool} 调用失败")[:500])
        return data

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        if self.reader:
            self.reader.cancel()
        self.process = None
