#!/bin/sh
# 由 GitHub Actions 部署流程在服务器上以 root 执行。
# 新代码已同步到 $STAGING；本脚本备份数据库、替换代码、重启并做健康检查，失败时自动回滚。
# data/（数据库、图片缓存）与 /etc/taptap-monitor.env（令牌）永远不会被覆盖。
set -eu

BASE=/opt/taptap-monitor
APP=$BASE/taptap-monitor
STAGING=$BASE/staging
PREVIOUS=$BASE/previous
BACKUPS=$BASE/backups
VENV=$BASE/venv
SERVICE=taptap-monitor
RUN_AS=taptap-monitor
KEEP_BACKUPS=14

log() { printf '[update] %s\n' "$*"; }
healthy() {
    for _ in $(seq 40); do
        if curl -fsS -m 3 http://127.0.0.1:8080/healthz >/dev/null 2>&1 \
            && curl -fsS -m 5 -o /dev/null http://127.0.0.1:8080/; then
            return 0
        fi
        sleep 1
    done
    return 1
}

[ -f "$STAGING/taptap_monitor/app.py" ] || { echo "staging directory is empty" >&2; exit 1; }

# 1. 在线备份数据库（以服务用户身份运行，避免产生 root 所有的 WAL 文件）
install -d -m 700 -o "$RUN_AS" -g "$RUN_AS" "$BACKUPS"
if [ -f "$APP/data/monitor.sqlite3" ]; then
    target="$BACKUPS/monitor-$(date +%Y%m%d-%H%M%S).sqlite3"
    (cd "$APP" && runuser -u "$RUN_AS" -- "$VENV/bin/python" -m taptap_monitor backup --output "$target" >/dev/null)
    log "database backup: $target"
    ls -1t "$BACKUPS"/monitor-*.sqlite3 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f
fi

# 2. 保存当前代码，用于回滚
rsync -a --delete --exclude /data/ "$APP/" "$PREVIOUS/"

# 3. 依赖有变化时才重新安装
if ! cmp -s "$STAGING/requirements.lock" "$APP/requirements.lock"; then
    log "requirements changed, installing"
    "$VENV/bin/pip" install --quiet --require-hashes -r "$STAGING/requirements.lock"
fi

# 4. 替换代码并重启
rsync -a --delete --exclude /data/ "$STAGING/" "$APP/"
chmod -R a+rX "$APP"
systemctl restart "$SERVICE"

if healthy; then
    log "healthy: $(cat "$APP/VERSION.json" 2>/dev/null || echo unknown)"
    exit 0
fi

# 5. 健康检查失败：回滚到上一版本
log "health check failed, rolling back" >&2
journalctl -u "$SERVICE" -n 40 --no-pager >&2 || true
rsync -a --delete --exclude /data/ "$PREVIOUS/" "$APP/"
systemctl restart "$SERVICE"
if healthy; then log "rolled back to previous version" >&2; else log "rollback also unhealthy, check the server" >&2; fi
exit 1
