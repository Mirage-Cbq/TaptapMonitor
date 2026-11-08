#!/bin/sh
# TaptapMonitor 一键安装 / 原地转换脚本（Debian/Ubuntu，需要 root）。
#
#   curl -fsSL https://raw.githubusercontent.com/Mirage-Cbq/TaptapMonitor/main/scripts/install.sh | sudo sh
#
# 可重复执行：已存在的 data/（数据库、图片）、config.toml 与 /etc/taptap-monitor.env 永远保留。
# 若 /opt/taptap-monitor/taptap-monitor 是旧的非 git 安装，会被原地转换为 git 仓库。
set -eu

REPO_URL=${REPO_URL:-https://github.com/Mirage-Cbq/TaptapMonitor.git}
BRANCH=${BRANCH:-main}
BASE=/opt/taptap-monitor
APP=$BASE/taptap-monitor
VENV=$BASE/venv
RUN_AS=taptap-monitor
ENV_FILE=/etc/taptap-monitor.env

log() { printf '\033[32m[install]\033[0m %s\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "请使用 root 运行（sudo sh install.sh）" >&2; exit 1; }

need=""
for cmd in git curl python3; do command -v "$cmd" >/dev/null 2>&1 || need="$need $cmd"; done
python3 -c 'import ensurepip' 2>/dev/null || need="$need python3-venv"
if [ -n "$need" ]; then
    log "安装依赖:$need"
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends git curl python3 python3-venv
fi

id "$RUN_AS" >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin "$RUN_AS"
install -d -m 755 "$BASE"

g() { git -c safe.directory="$APP" -C "$APP" "$@"; }
if [ -d "$APP/.git" ]; then
    log "更新已有仓库"
    g remote set-url origin "$REPO_URL"
    g fetch --quiet origin "$BRANCH"
elif [ -d "$APP" ]; then
    log "将现有安装转换为 git 仓库（保留 data/ 与 config.toml）"
    [ -f "$APP/config.toml" ] && cp -n "$APP/config.toml" "$BASE/config.toml.bak" || true
    g init --quiet
    g remote add origin "$REPO_URL"
    g fetch --quiet origin "$BRANCH"
    g reset --hard --quiet "origin/$BRANCH"
else
    log "克隆仓库"
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP"
fi
g checkout --quiet -B "$BRANCH" "origin/$BRANCH"
g branch --quiet --set-upstream-to="origin/$BRANCH" "$BRANCH"
[ -f "$APP/config.toml" ] || { cp "$APP/config.example.toml" "$APP/config.toml"; log "已从模板创建 config.toml，请按需修改厂商配置"; }
find "$APP" -path "$APP/data" -prune -o -exec chmod a+rX {} +   # data/ 保持私有权限

[ -x "$VENV/bin/python" ] || { log "创建 Python 虚拟环境"; python3 -m venv "$VENV"; }
log "安装 Python 依赖"
"$VENV/bin/pip" install --quiet --require-hashes -r "$APP/requirements.lock"

install -d -m 750 -o "$RUN_AS" -g "$RUN_AS" "$APP/data" "$APP/data/upgrade"
if [ ! -f "$ENV_FILE" ]; then
    (umask 077; printf 'MONITOR_API_TOKEN=%s\n' "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > "$ENV_FILE")
    log "已生成管理令牌：$ENV_FILE"
fi

log "安装 systemd 服务"
install -m 644 "$APP"/deploy/systemd/taptap-monitor*.service "$APP"/deploy/systemd/taptap-monitor*.path \
    "$APP"/deploy/systemd/taptap-monitor*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet taptap-monitor taptap-monitor-update.path taptap-monitor-autoupdate.timer
systemctl restart taptap-monitor
systemctl start taptap-monitor-update.path taptap-monitor-autoupdate.timer

python3 "$APP/scripts/updater.py" check >/dev/null || true
for _ in $(seq 30); do
    if curl -fsS -m 3 http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
        log "完成！服务运行中：http://127.0.0.1:8080/ （后台 /admin，令牌见 $ENV_FILE）"
        log "公网访问请配置反向代理，可参考 $APP/deploy/nginx/taptap-monitor.conf"
        exit 0
    fi
    sleep 1
done
echo "服务未通过健康检查，请查看：journalctl -u taptap-monitor -n 100" >&2
exit 1
