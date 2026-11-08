#!/usr/bin/env python3
"""Pull-based self-updater for TaptapMonitor.

Runs as root under systemd (see deploy/systemd/). The web service itself is
unprivileged: it only drops a request file into data/upgrade/, and this script
does the git/pip/systemctl work and reports back through status.json.

Only commits reachable from origin/<branch> can be deployed, so the admin
console can upgrade or roll back but never run arbitrary code.

Usage: updater.py check | apply [--ref latest|<sha>] | request | auto
"""
import argparse
import datetime
import fcntl
import json
import os
import pwd
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(os.environ.get("TAPTAP_APP_DIR", "/opt/taptap-monitor/taptap-monitor"))
VENV = Path(os.environ.get("TAPTAP_VENV", "/opt/taptap-monitor/venv"))
BACKUPS = Path(os.environ.get("TAPTAP_BACKUPS", "/opt/taptap-monitor/backups"))
SERVICE = os.environ.get("TAPTAP_SERVICE", "taptap-monitor")
RUN_AS = os.environ.get("TAPTAP_USER", "taptap-monitor")
BRANCH = os.environ.get("TAPTAP_BRANCH", "main")
BASE_URL = os.environ.get("TAPTAP_LOCAL_URL", "http://127.0.0.1:8080")
STATE = APP / "data" / "upgrade"
REQUEST, STATUS, AUTO = STATE / "request.json", STATE / "status.json", STATE / "auto.json"
LOCK = Path(os.environ.get("TAPTAP_LOCK", "/run/taptap-monitor-update.lock"))
SHA = re.compile(r"^[0-9a-f]{7,40}$")
KEEP_BACKUPS = 14


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def git(*args, check=True):
    result = subprocess.run(["git", "-c", f"safe.directory={APP}", "-C", str(APP), *args],
                            capture_output=True, text=True, timeout=600)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"git {args[0]} failed")
    return result


def info(ref):
    sha, date, subject = git("log", "-1", "--format=%H%x00%cI%x00%s", ref).stdout.strip().split("\x00")
    return {"commit": sha, "date": date, "message": subject}


def commits(range_or_ref, limit):
    out = git("log", f"-{limit}", "--format=%H%x00%cI%x00%s", range_or_ref).stdout.strip()
    return [dict(zip(("commit", "date", "message"), line.split("\x00"))) for line in out.splitlines() if line]


def load_status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(**fields):
    status = load_status()
    if "log" in fields:
        fields["log"] = (status.get("log", []) + fields["log"])[-80:]
    status.update(fields, updated_at=now())
    STATE.mkdir(parents=True, exist_ok=True)
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    user = pwd.getpwnam(RUN_AS)
    os.chown(temporary, user.pw_uid, user.pw_gid)
    os.chmod(temporary, 0o640)
    temporary.replace(STATUS)


def log(message):
    print(message, flush=True)
    save(log=[f"{datetime.datetime.now().strftime('%m-%d %H:%M:%S')} {message}"])


def write_version():
    version = {**info("HEAD"), "deployed_at": now()}
    (APP / "VERSION.json").write_text(json.dumps(version, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(APP / "VERSION.json", 0o644)
    return version


def fetch():
    for attempt in range(3):
        result = git("fetch", "--quiet", "--prune", "origin", BRANCH, check=False)
        if result.returncode == 0:
            return
        time.sleep(3 * (attempt + 1))
    raise RuntimeError("无法从 GitHub 拉取更新：" + (result.stderr.strip() or "网络错误"))


def refresh(do_fetch=True):
    if do_fetch:
        fetch()
    remote = f"origin/{BRANCH}"
    behind = commits(f"HEAD..{remote}", 50)
    save(current=info("HEAD"), latest=info(remote), behind=behind, history=commits(remote, 20),
         branch=BRANCH, checked_at=now())
    return behind


def resolve(ref):
    remote = f"origin/{BRANCH}"
    if ref in (None, "", "latest"):
        target = git("rev-parse", remote).stdout.strip()
    elif SHA.fullmatch(ref):
        target = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").stdout.strip()
    else:
        raise RuntimeError("版本号格式不正确")
    if git("merge-base", "--is-ancestor", target, remote, check=False).returncode:
        raise RuntimeError("只能部署官方分支历史中的版本")
    return target


def healthy():
    for _ in range(45):
        try:
            for path in ("/healthz", "/"):
                with urllib.request.urlopen(BASE_URL + path, timeout=5) as response:
                    if response.status != 200:
                        raise OSError(response.status)
            return True
        except OSError:
            time.sleep(1)
    return False


def backup():
    database = APP / "data" / "monitor.sqlite3"
    if not database.exists():
        return
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    user = pwd.getpwnam(RUN_AS)
    os.chown(BACKUPS, user.pw_uid, user.pw_gid)
    target = BACKUPS / f"monitor-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.sqlite3"
    # Run as the service user so SQLite never creates root-owned WAL files.
    subprocess.run(["runuser", "-u", RUN_AS, "--", str(VENV / "bin/python"), "-m", "taptap_monitor", "backup",
                    "--output", str(target)], cwd=APP, check=True, capture_output=True, timeout=600)
    for old in sorted(BACKUPS.glob("monitor-*.sqlite3"), reverse=True)[KEEP_BACKUPS:]:
        old.unlink()
    log(f"已备份数据库：{target.name}")


def switch(target, previous):
    git("reset", "--hard", "--quiet", target)
    if git("diff", "--quiet", previous, target, "--", "requirements.lock", check=False).returncode:
        log("依赖有变化，正在安装")
        subprocess.run([str(VENV / "bin/pip"), "install", "--quiet", "--require-hashes", "-r",
                        str(APP / "requirements.lock")], check=True, capture_output=True, timeout=1200)
    # Code must be readable by the service user; data/ keeps its private permissions.
    subprocess.run(["find", str(APP), "-path", str(APP / "data"), "-prune", "-o", "-exec", "chmod", "a+rX", "{}", "+"], check=True)
    write_version()
    subprocess.run(["systemctl", "restart", SERVICE], check=True, timeout=120)


def apply(ref):
    save(state="checking", message="正在检查更新…")
    refresh()
    target, previous = resolve(ref), git("rev-parse", "HEAD").stdout.strip()
    if target == previous:
        save(state="idle", result="up_to_date", message="已是该版本，无需升级", finished_at=now())
        return
    save(state="updating", target=info(target), message=f"正在升级到 {target[:7]}…", started_at=now())
    log(f"开始升级 {previous[:7]} → {target[:7]}")
    backup()
    switch(target, previous)
    if healthy():
        log(f"升级完成，当前版本 {target[:7]}")
        refresh(do_fetch=False)
        save(state="idle", result="success", message=f"已升级到 {target[:7]}", finished_at=now())
        return
    log("健康检查失败，正在回滚")
    switch(previous, target)
    ok = healthy()
    refresh(do_fetch=False)
    save(state="idle", result="rolled_back" if ok else "failed", finished_at=now(),
         message=f"新版本启动失败，已回滚到 {previous[:7]}" if ok else "升级与回滚均未通过健康检查，请登录服务器排查")


def auto_enabled():
    try:
        return bool(json.loads(AUTO.read_text(encoding="utf-8")).get("enabled"))
    except (OSError, ValueError):
        return False


def run(action, ref=None):
    try:
        if action == "check":
            save(state="checking", message="正在检查更新…")
            behind = refresh()
            save(state="idle", result="checked", finished_at=now(),
                 message=f"有 {len(behind)} 个新版本可升级" if behind else "已是最新版本")
        elif action == "apply":
            apply(ref)
        elif action == "auto":
            behind = refresh()
            if behind and auto_enabled():
                log("自动升级：发现新版本")
                apply("latest")
            else:
                save(state="idle", message=f"有 {len(behind)} 个新版本可升级" if behind else "已是最新版本")
    except Exception as exc:  # report every failure to the admin console
        log(f"失败：{exc}")
        save(state="idle", result="failed", message=str(exc)[:500], finished_at=now())
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "apply", "request", "auto"])
    parser.add_argument("--ref", default="latest")
    args = parser.parse_args()
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another update is running")
            return 0
        if args.action == "request":
            try:
                request = json.loads(REQUEST.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                request = {}
            REQUEST.unlink(missing_ok=True)
            action, ref = request.get("action"), str(request.get("ref") or "latest")
            if action not in ("check", "apply") or not (ref == "latest" or SHA.fullmatch(ref)):
                save(state="idle", result="failed", message="无效的升级请求")
                return 1
            return run(action, ref)
        if args.action == "check" and not (APP / "VERSION.json").exists():
            write_version()
        return run(args.action, args.ref)


if __name__ == "__main__":
    sys.exit(main())
