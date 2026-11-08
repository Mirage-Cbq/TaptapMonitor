# TaptapMonitor

TapTap 厂商作品监测与官网生成系统。系统会自动发现厂商的全部作品，采集玩家评价、论坛帖子和公开回复，保存历史记录与修改记录，并根据 TapTap 上的介绍和图片自动生成厂商官网，同时提供一个可视化管理后台。

技术栈为 Python 3.12、FastAPI 和 SQLite，单进程运行，不依赖浏览器、Redis 或独立数据库。页面是纯静态的 HTML/CSS/JS，不需要构建，也不引用外部 CDN，可以直接部署在国内服务器上。

- **管理后台**：数据概览（趋势、评分分布、作品排行）、反馈检索与导出、厂商与官网编辑、采集运行记录、系统设置，还能在线升级系统。
- **厂商官网**：每天从管理系统同步作品数据。评分统一按 10 分制显示，官网不公开玩家评论。
- **AI 与推送**：提供结构化的 AI 输入接口，也可以通过 AstrBot 定时推送事实简报。

## 一键安装（推荐）

适用于 Debian/Ubuntu 服务器，需要 root 权限：

```sh
git clone https://github.com/Mirage-Cbq/TaptapMonitor.git /tmp/TaptapMonitor && sudo sh /tmp/TaptapMonitor/scripts/install.sh
```

> 国内服务器通常无法访问 `raw.githubusercontent.com`，但可以访问 `github.com`，所以这里用 `git clone` 获取安装脚本。如果 PyPI 下载较慢，可以先执行 `pip config --global set global.index-url https://mirrors.aliyun.com/pypi/simple/` 写入 `/etc/pip.conf`。依赖带有哈希校验，换成镜像源同样安全，之后的在线升级也会使用这个源。

安装脚本可以重复执行，会依次完成以下工作：

1. 安装依赖，并创建低权限用户 `taptap-monitor`。
2. 将仓库克隆到 `/opt/taptap-monitor/taptap-monitor`。
3. 创建虚拟环境，按 `requirements.lock` 安装依赖，并校验哈希。
4. 从 `config.example.toml` 生成 `config.toml`。
5. 在 `/etc/taptap-monitor.env` 中生成随机的管理令牌。
6. 安装并启动 systemd 服务。

如果服务器上已有旧的非 git 安装，脚本会把它原地转换为 git 仓库。转换时 `data/`（数据库、图片缓存）、`config.toml` 和令牌文件都会保留。

服务默认只监听 `127.0.0.1:8080`。可以先通过 SSH 隧道访问：

```sh
ssh -L 8080:127.0.0.1:8080 root@your-server
```

在电脑上打开以下地址：

- 官网：`http://127.0.0.1:8080/`
- 管理后台：`http://127.0.0.1:8080/admin`，用 `/etc/taptap-monitor.env` 中的 `MONITOR_API_TOKEN` 登录
- 存活检查：`http://127.0.0.1:8080/healthz`

### 公网访问

仓库里提供了 nginx 配置示例，可以把官网和后台都通过 80 端口公开：

```sh
sudo apt-get install -y nginx
sudo cp /opt/taptap-monitor/taptap-monitor/deploy/nginx/taptap-monitor.conf /etc/nginx/sites-available/taptap-monitor
sudo ln -sf /etc/nginx/sites-available/taptap-monitor /etc/nginx/sites-enabled/taptap-monitor
sudo rm -f /etc/nginx/sites-enabled/default && sudo nginx -t && sudo systemctl reload nginx
```

公开后，访问 `http://服务器IP/admin` 并输入管理令牌即可登录。这份配置有以下处理：

- `/api/` 按来源 IP 限速，超出的请求返回 429。
- 后台页面不会被搜索引擎收录。
- 请求体上限放宽到 10 MB，以支持上传厂商图标。

服务端口 8080 只监听本机，安全组只需要放行 22 和 80（启用 HTTPS 后再放行 443）。**建议尽快配置 HTTPS**，例如 `certbot --nginx -d 你的域名`，否则令牌会以明文传输。国内服务器使用域名前需要先完成 ICP 备案。

如果只想公开官网，可以取消配置中「仅公开官网」两行的注释，后台改用 SSH 隧道访问：`ssh -L 8080:127.0.0.1:8080 root@服务器`。

### 在线升级

后台「系统设置 → 系统升级」可以检查更新、一键升级到最新版、回滚到任意历史版本，还可以开启「每天凌晨自动升级」。

升级采用拉取方式，服务器不需要开放任何入口，也不需要 GitHub 令牌：

```text
后台（低权限）──写入请求──▶ data/upgrade/request.json
                                  │ systemd path 单元监测到文件
                                  ▼
             scripts/updater.py（root，一次性任务）
             git fetch → 备份数据库 → 切换版本 → 依赖有变化才重新安装
             → 重启服务 → 健康检查 → 未通过则自动回滚
                                  │
后台轮询 ◀──── data/upgrade/status.json（进度、结果、日志）
```

只有官方分支历史中的提交才能被部署，后台无法借此执行任意代码。每次升级前都会备份数据库到 `/opt/taptap-monitor/backups/`，默认保留 14 份。命令行也可以直接操作：

```sh
sudo python3 /opt/taptap-monitor/taptap-monitor/scripts/updater.py check
sudo python3 /opt/taptap-monitor/taptap-monitor/scripts/updater.py apply              # 升级到最新版
sudo python3 /opt/taptap-monitor/taptap-monitor/scripts/updater.py apply --ref 1a2b3c4  # 切换到指定版本
```

如果使用自己的 fork，可以用 `sudo REPO_URL=https://github.com/你/TaptapMonitor.git sh install.sh` 安装。之后服务器就会从你的仓库拉取更新。

### 配置

| 位置 | 内容 |
| --- | --- |
| `config.toml` | 采集间隔、翻页预算、是否采集回复、图片缓存、初始厂商和默认官网。修改后需要执行 `systemctl restart taptap-monitor` |
| `/etc/taptap-monitor.env` | `MONITOR_API_TOKEN`，以及可选的 `ASTRBOT_BASE_URL` 和 `ASTRBOT_API_KEY` |
| 后台「系统设置」 | 采集间隔、请求间隔与超时、翻页上限、图片缓存和简报推送。保存后立即生效，保存在数据库中 |

`config.toml` 不纳入版本库，升级时不会被覆盖。仓库中只提供模板 `config.example.toml`。模板里默认监测的是[菜篮子工作室](https://www.taptap.cn/developer/383549)，你可以改成自己的厂商 ID，也可以在后台继续添加厂商。

> **网络说明**：TapTap 网页版 `www.taptap.cn` 对部分云服务器 IP 启用了 WAF 拦截（返回 405 验证页）。本项目的数据接口改走移动版 `m.taptap.cn`，这条线路在云服务器上可以正常访问。

### 使用 Docker（可选）

需要 Docker Engine 和 Docker Compose v2：

```sh
git clone https://github.com/Mirage-Cbq/TaptapMonitor.git && cd TaptapMonitor
sh scripts/deploy.sh
```

首次运行会生成 `config.toml` 和 `.env`（`.env` 中包含随机的管理令牌）。以后要更新时，执行 `git pull && docker compose up -d --build`。Docker 方式不支持后台的在线升级功能。

### 首次启动的行为

启动后会立即同步一次，之后默认每 30 分钟同步一次，首次同步通常需要几分钟。首次发现的旧评价、帖子和回复会作为历史基线保存，不会推送这些旧内容。已有厂商的开关与推送会话保存在数据库中，重启时不会被配置文件覆盖。

## 后台与自动生成

后台 `/admin` 支持：

1. 输入 TapTap 厂商主页链接或 ID，添加监测对象并自动安排首次同步。
2. 自动更新其作品列表、标题、介绍、标签和宣传图，新作品自动出现在官网。
3. 编辑工作室名称、标语、简介、关于文案和厂商图标，单独控制官网是否公开。
4. 设置作品展示/隐藏、精选置顶、排序、自定义标题和介绍。
5. 查看玩家评价、帖子、公开回复、采集状态与运行记录，导出 AI 汇总数据。
6. 设置各厂商的 AstrBot 接收会话，切换哪个厂商作为默认官网。

每个厂商都有独立页面：`/studio/{厂商ID}`。每个作品自动生成详情入口：`/studio/{厂商ID}/games/{游戏ID}`，支持分享链接。

官网采用模板与数据库内容生成，页面标题与描述由服务端填入。人工文案与采集数据分别保存，同步不会覆盖人工修改。自定义标题/介绍清空后，恢复跟随 TapTap。作品默认按精选、排序值（越小越靠前）、游戏 ID 排序；隐藏的作品不再通过公开详情页访问，但继续保留历史数据。

图片优先使用 TapTap 原始宣传图，采集时自动缓存到数据卷中的 `media/`，避免每次访问都请求 TapTap。图片 URL 变化会产生新的缓存文件。

首屏采用纯色背景与独立的厂商图标，不使用游戏背景大图。后台可为每个厂商上传 PNG、JPEG 或 WebP 图标（最大 8 MB、每边不超过 4096 像素），上传的透明 PNG 会保留原始透明度，也可以恢复采用 TapTap 厂商图标。首次附带的菜篮子工作室图标在 `website/dist/brand/studio-logo-transparent.png`，通过 `config.toml` 的厂商 `logo` 字段初始化；后续后台修改优先，不会被重启或同步覆盖。

## 采集范围与可靠性

- 厂商作品列表、游戏评价、论坛帖子、评价回复、论坛公开评论与页面提供的楼中楼预览。
- 评价和论坛接口每页最多 10 条，程序跟随 `next_page` 翻页。
- 每个数据源的翻页进度保存在 SQLite。失败或达到 `max_pages` 后，下一轮先检查最新第一页，再继续上次进度。
- 用来源 ID 去重，用正文、标题、评分等内容识别修改；点赞量、浏览量变化不会生成“内容修改”事件。
- HTTP 失败、验证页面、结构异常、未完成分页会显示为异常或待处理，不会伪装成“没有更新”。
- 单数据库只允许一个服务或采集进程写入。请使用一个副本、一个 Uvicorn worker；手动同步使用后台按钮或 API。
- 图片、来源内容和历史记录保存在数据卷中，容器更新和重启不会清空。

**访问限制：**目前只采集匿名可公开获取的数据。TapTap 对部分楼中楼要求登录，返回的只是预览；系统会继续收集其他可读内容，并将对应数据源标为 `restricted`。不保证获取登录后、折叠或已删除内容。上游接口不是稳定承诺的开放 API，将来变更时可能需要调整适配器。云服务器出口需要能正常访问 TapTap；若返回验证页面，后台会记录失败。

## AstrBot 与 AI 接入

第一版提供**结构化 AI 输入和不经模型的事实简报**。尚未内置 AI 模型归纳。这样可以先稳定收集数据，再由 AstrBot 或其他 AI 系统按自己的提示词生成总结。

推荐让外部系统持久化一个事件游标，按下面顺序工作：读取新增事件 → 总结 → 发送成功 → 保存 `next_cursor`。生成或发送失败时保留旧游标，重试同一批数据。

```sh
curl -H "Authorization: Bearer $MONITOR_API_TOKEN" \
  'http://127.0.0.1:8080/api/digest-input?developer_id=383549&after=0&limit=100'
```

返回 `counts`、带原文链接的 `events`、建议提示词、`markdown`、`next_cursor` 和 `has_more`。`has_more=true` 时继续分页。默认排除历史基线，需要回顾旧内容时传 `include_baseline=true`。这些读取接口不会修改游标，外部消费者可以各自维护进度。

若需要先通过 AstrBot 定时发送事实简报：

1. 使用支持 HTTP API 的 AstrBot（官方文档为 v4.18.0 起），创建具有 IM 消息发送权限的 API Key。
2. 在 `/etc/taptap-monitor.env`（Docker 方式为 `.env`）中填写 `ASTRBOT_BASE_URL` 和 `ASTRBOT_API_KEY`。容器里的 `127.0.0.1` 指本容器；AstrBot 在同机其他容器时，请使用可达的容器服务名或网关地址。
3. 在后台填写厂商的 `target_umo`，它是 AstrBot 的目标会话标识。
4. 在后台「系统设置」开启简报推送（或将 `config.toml` 的 `[digest] enabled` 设为 `true` 后重启）。默认间隔为 24 小时，自该厂商添加时间计算；简报时间标记为 UTC。

发送通过 `/api/v1/im/message`。发送记录和分段进度持久化，失败自动重试，整批成功后才推进游标；没有新增内容时不发送。网络超时可能发生“对方收到但本地没有收到确认”，因此采用至少一次投递，极端情况下可能重复一段。第一次连接时请在目标平台确认是否允许主动发消息。

参考：[AstrBot HTTP API](https://docs.astrbot.app/dev/openapi.html)。项目没有自带平台账号、模型密钥或实际发送目标。

## 主要 API

除 `/healthz`、`/public/site`、官网与图片外，均需要 `Authorization: Bearer <令牌>`。

| 接口 | 用途 |
| --- | --- |
| `GET /api/status` | 数量、采集状态、异常数据源 |
| `GET/POST /api/developers` | 列出/添加厂商 |
| `PATCH /api/developers/{id}` | 修改启停与接收会话 |
| `POST /api/scan?developer_id={id}` | 异步启动采集，返回运行 ID；已有任务时返回 409 |
| `GET /api/games?developer_id={id}` | 来源作品与元数据 |
| `GET /api/items?developer_id={id}&kind=review` | 查询反馈；支持 `limit`、`offset` |
| `GET /api/events?after={cursor}` | 按单调递增游标读取新增/修改事件 |
| `GET /api/digest-input?developer_id={id}` | 提供 AI 输入与事实简报 |
| `GET /api/runs`、`GET /api/deliveries` | 运行与投递记录 |
| `GET/PATCH /api/sites/{id}` | 官网配置 |
| `PUT/DELETE /api/sites/{id}/logo` | 上传二进制图标/恢复自动图标 |
| `PATCH /api/sites/{id}/games/{app_id}` | 作品展示配置 |
| `PUT /api/site-default/{id}` | 设置默认官网 |
| `GET /api/stats`、`GET /api/items/search` | 后台概览统计与反馈检索 |
| `GET/PATCH /api/settings` | 运行参数 |
| `GET /api/system` | 版本与升级状态 |
| `POST /api/system/check`、`POST /api/system/upgrade` | 请求检查更新、升级/回滚（`{"ref": "latest"}` 或提交号） |
| `PUT /api/system/auto` | 开关自动升级 |
| `GET /api/openapi.json` | 带鉴权的完整接口描述 |

## 本地开发与检查

Python 3.12+：

```sh
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv/Scripts/Activate.ps1
python -m pip install --require-hashes -r requirements.lock
python -m pip install pytest
python scripts/init_env.py
cp config.example.toml config.toml   # Windows: copy config.example.toml config.toml
# 将 .env 的 MONITOR_API_TOKEN 设置为当前终端环境变量后运行
python -m taptap_monitor serve
```

`serve` 不会自动读取 `.env`；Docker Compose 会读取。在 PowerShell 可使用 `$env:MONITOR_API_TOKEN='自己的令牌'`，Linux 使用 `export MONITOR_API_TOKEN='自己的令牌'`。

无需启动服务即可手动采集或查看状态：

```sh
python -m taptap_monitor collect
python -m taptap_monitor status
python -m pytest -q
node --check website/dist/site.js
node --check website/dist/admin.js
```

`collect` 在部分数据受限或失败时返回退出码 1，但已获取的数据仍会保存，详情可查看运行记录。页面无构建步骤，无需安装 Node 依赖；Node 只用于可选的 JavaScript 语法检查。

## 备份

一键安装方式下，每次升级前都会自动备份。需要手动备份时，可以使用 SQLite 在线备份，不要直接复制正在使用的数据库文件：

```sh
sudo -u taptap-monitor /opt/taptap-monitor/venv/bin/python -m taptap_monitor backup   --output /opt/taptap-monitor/backups/manual-$(date +%Y%m%d).sqlite3
# Docker：docker compose exec monitor python -m taptap_monitor backup --output /app/data/backup.sqlite3
```

备份命令要求目标文件尚不存在。系统会保留全部历史记录，不会自动删除。

## 目录

```text
taptap_monitor/   采集、数据库、API、官网生成和投递
website/dist/    官网模板与管理后台，无构建依赖
tests/           离线行为测试
scripts/         install.sh 一键安装、updater.py 在线升级、Docker 部署脚本
deploy/          systemd 单元与 nginx 配置示例
config.example.toml  配置模板（复制为 config.toml 使用）
requirements.lock  Python 3.12 运行依赖与校验哈希
```
