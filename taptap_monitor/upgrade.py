"""System upgrades driven by the GitHub Actions deploy workflow.

The server never pulls code itself: the admin console asks GitHub to run the
deploy workflow, which tests the chosen commit and pushes it to this server.
"""
import json
from pathlib import Path
import re

import httpx

API = "https://api.github.com"
REF_PATTERN = re.compile(r"^(main|[0-9a-f]{7,40})$")


class UpgradeError(Exception):
    pass


def current_version(root: Path) -> dict | None:
    """VERSION.json is written by the deploy workflow next to the code."""
    try:
        data = json.loads((root / "VERSION.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {key: data.get(key) for key in ("commit", "message", "date", "deployed_at", "run_url")}


class GitHub:
    def __init__(self, repo: str, workflow: str, branch: str, token: str, transport=None):
        self.repo, self.workflow, self.branch, self.token = repo, workflow, branch, token
        self.http = httpx.AsyncClient(base_url=API, timeout=15, transport=transport, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "TaptapMonitor", **({"Authorization": f"Bearer {token}"} if token else {}),
        })

    @property
    def configured(self) -> bool:
        return bool(self.repo and self.token)

    async def close(self):
        await self.http.aclose()

    async def _request(self, method, path, **kwargs):
        if not self.configured:
            raise UpgradeError("未配置 GitHub 仓库或访问令牌")
        try:
            response = await self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise UpgradeError("无法连接 GitHub，请稍后重试") from exc
        if response.status_code in (401, 403):
            raise UpgradeError("GitHub 令牌无效或权限不足")
        if response.status_code == 404:
            raise UpgradeError("找不到 GitHub 仓库或部署流程")
        if response.status_code >= 400:
            raise UpgradeError(f"GitHub 返回错误 {response.status_code}")
        return response

    async def latest(self) -> dict:
        commit = (await self._request("GET", f"/repos/{self.repo}/commits/{self.branch}")).json()
        runs = (await self._request("GET", f"/repos/{self.repo}/actions/workflows/{self.workflow}/runs",
                                    params={"per_page": 10})).json().get("workflow_runs", [])
        return {
            "latest": {"commit": commit["sha"], "message": commit["commit"]["message"].split("\n")[0],
                       "date": commit["commit"]["committer"]["date"]},
            "runs": [{key: run.get(key) for key in ("id", "status", "conclusion", "head_sha", "created_at",
                                                     "updated_at", "html_url", "event", "display_title")}
                     for run in runs],
        }

    async def dispatch(self, ref: str):
        if not REF_PATTERN.fullmatch(ref):
            raise UpgradeError("版本号格式不正确")
        await self._request("POST", f"/repos/{self.repo}/actions/workflows/{self.workflow}/dispatches",
                            json={"ref": self.branch, "inputs": {"ref": ref}})
