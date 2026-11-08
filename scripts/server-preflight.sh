#!/bin/sh
# Read-only server inspection; does not read service environments or credentials.
set -u

section() { printf '\n[%s]\n' "$1"; }
section system
if [ -r /etc/os-release ]; then
    awk -F= '/^(NAME|VERSION_ID)=/ {print}' /etc/os-release
else
    echo 'os-release=unavailable'
fi
printf 'uid='; id -u
printf 'user='; id -un
if command -v python3 >/dev/null 2>&1; then python3 --version 2>&1; else echo 'python3=unavailable'; fi
section disk
if command -v df >/dev/null 2>&1; then df -h / /opt; else echo 'df=unavailable'; fi
section memory
if command -v free >/dev/null 2>&1; then free -h; else echo 'free=unavailable'; fi
section listening_tcp
if command -v ss >/dev/null 2>&1; then ss -lnt; else echo 'ss=unavailable'; fi
section docker
if command -v docker >/dev/null 2>&1; then
    docker --version 2>&1
    docker compose version 2>&1 || echo 'compose=unavailable'
    echo 'containers (name | image | published ports):'
    docker ps -a --format '{{.Names}} | {{.Image}} | {{.Ports}}' 2>&1 || echo 'container_list=unavailable'
else
    echo 'docker=unavailable'
fi
section services
if command -v systemctl >/dev/null 2>&1; then
    for service in astrbot nginx caddy; do
        printf '%s:\n' "$service"
        systemctl show "$service.service" --property=LoadState --property=ActiveState --property=SubState --property=User --property=WorkingDirectory 2>&1 || echo 'status=unavailable'
    done
else
    echo 'systemctl=unavailable'
fi
section taptap
if command -v python3 >/dev/null 2>&1; then
python3 - <<'PY'
import json
import urllib.error
import urllib.parse
import urllib.request

base = 'https://www.taptap.cn/webapiv2/'
headers = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Referer': 'https://www.taptap.cn/',
    'Accept': 'application/json',
}
for path, params in (
    ('developer/v1/detail', {'developer_id': 383549}),
    ('app-list/v1/detail', {'developer_id': 383549, 'limit': 20}),
):
    params['X-UA'] = 'V=1&PN=WebApp&VN=0.1.0&LANG=zh_CN&PLT=PC'
    url = base + path + '?' + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
            status = response.status
            try:
                payload = json.load(response)
                ok = isinstance(payload, dict) and payload.get('success') is True and isinstance(payload.get('data'), dict)
                print('{}: HTTP {} api_ok={}'.format(path, status, str(ok).lower()))
            except (ValueError, UnicodeDecodeError):
                print('{}: HTTP {} non_json'.format(path, status))
    except urllib.error.HTTPError as exc:
        print('{}: HTTP {}'.format(path, exc.code))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print('{}: request_failed ({})'.format(path, type(exc).__name__))
PY
else
    echo 'taptap_check=unavailable (python3 missing)'
fi
