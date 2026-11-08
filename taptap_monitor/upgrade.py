"""Admin-console side of the pull-based updater (scripts/updater.py).

The web service runs unprivileged, so it never touches git or systemd itself:
it writes a request file into data/upgrade/, which a root systemd path unit
picks up, and reads the updater's status.json back.
"""
import json
import os
from pathlib import Path
import re

REF_PATTERN = re.compile(r"^(latest|[0-9a-f]{7,40})$")


class UpgradeError(Exception):
    pass


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def current_version(root: Path) -> dict | None:
    data = _read(root / "VERSION.json")
    return {key: data.get(key) for key in ("commit", "message", "date", "deployed_at")} if isinstance(data, dict) else None


class Updater:
    def __init__(self, data_dir: Path, code_root: Path):
        self.state, self.code_root = data_dir / "upgrade", code_root

    @property
    def installed(self) -> bool:
        return (self.state / "status.json").exists()

    def status(self) -> dict:
        status = _read(self.state / "status.json") or {}
        auto = _read(self.state / "auto.json") or {}
        return {**status, "installed": self.installed, "version": current_version(self.code_root),
                "auto": bool(auto.get("enabled")), "pending": (self.state / "request.json").exists()}

    def _write(self, name: str, data: dict):
        if not self.installed:
            raise UpgradeError("服务器尚未安装更新服务，请在服务器上运行 scripts/install.sh")
        temporary = self.state / f".{name}.tmp"
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, self.state / name)

    def request(self, action: str, ref: str = "latest"):
        if action not in ("check", "apply") or not REF_PATTERN.fullmatch(ref):
            raise UpgradeError("无效的升级请求")
        if self.status().get("state") in ("checking", "updating"):
            raise UpgradeError("已有升级任务正在进行")
        self._write("request.json", {"action": action, "ref": ref})

    def set_auto(self, enabled: bool):
        self._write("auto.json", {"enabled": bool(enabled)})
