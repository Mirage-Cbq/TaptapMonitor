# TapTap 作品监测与厂商官网

一个可部署到 Linux 云服务器的轻量系统：自动发现厂商作品，采集评价、论坛和公开回复，保存历史与修改记录，同时根据 TapTap 的介绍和图片自动生成厂商官网。Python + FastAPI + SQLite，单容器、单进程运行，不依赖浏览器、Redis 或独立数据库。

首次配置已加入 [菜篮子工作室](https://www.taptap.cn/developer/383549)。厂商和作品不写在网页代码里，可以在后台继续添加。

## 云服务器部署

服务器需要 Docker Engine 和 Docker Compose v2。将项目上传并解压后：

```sh
cd taptap-monitor
sh scripts/deploy.sh
docker compose ps
docker compose logs --tail=100 monitor
```

脚本首次运行会生成 `.env`，其中包含随机的 `MONITOR_API_TOKEN`，不会覆盖已有文件。也可以使用 `python3 scripts/init_env.py` 生成，或自行创建 `.env`。不要使用 `.env.example` 中的占位令牌。

默认绑定服务器 `127.0.0.1:8080`。通过 SSH 隧道查看：

```sh
ssh -L 8080:127.0.0.1:8080 user@your-server
```

在电脑打开：

- 官网：`http://127.0.0.1:8080/`
- 管理后台：`http://127.0.0.1:8080/admin`
- 存活检查：`http://127.0.0.1:8080/healthz`

后台使用 `.env` 里的管理令牌登录。正式域名可以通过服务器现有的反向代理转发到 `127.0.0.1:8080`；仓库提供了 `Caddyfile.example`，修改为自己的域名后可由 Caddy 配置 HTTPS。后台和反馈 API 均需要令牌，官网和图片为公开内容。

### 首次启动的行为

启动后立即同步一次，默认每 30 分钟再同步。首次同步通常需要几分钟，取决于作品和讨论数量。首次发现的旧评价、帖子和回复作为历史基线保存，默认不会推送旧内容。

`config.toml` 中可以调整间隔、每页扫描预算、是否采集回复、图片缓存、初始厂商和默认官网。已有厂商的开关与推送会话保存在数据库中；重启不会被配置文件覆盖。配置文件修改后执行：

```sh
docker compose up -d --force-recreate
```

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
2. 在 `.env` 中填写 `ASTRBOT_BASE_URL` 和 `ASTRBOT_API_KEY`。容器里的 `127.0.0.1` 指本容器；AstrBot 在同机其他容器时，请使用可达的容器服务名或网关地址。
3. 在后台填写厂商的 `target_umo`，它是 AstrBot 的目标会话标识。
4. 将 `config.toml` 的 `[digest] enabled` 设为 `true`，重建容器。默认间隔为 24 小时，自该厂商添加时间计算；简报时间标记为 UTC。

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

## 备份与更新

使用 SQLite 在线备份，不直接复制正在使用的数据库文件：

```sh
docker compose exec monitor python -m taptap_monitor backup --output /app/data/backup-20261105.sqlite3
docker compose cp monitor:/app/data/backup-20261105.sqlite3 ./backup-20261105.sqlite3
```

备份命令要求目标文件尚不存在。图片缓存可通过数据卷另行备份。当前版本保留全部历史，不自动删除记录。

更新项目文件后运行 `docker compose up -d --build`，保留原数据卷和 `.env`。`docker compose down` 不会删除命名数据卷；不要使用 `down -v`，除非确实要清空数据。

## 目录

```text
taptap_monitor/   采集、数据库、API、官网生成和投递
website/dist/    官网模板与管理后台，无构建依赖
tests/           离线行为测试
scripts/         部署与初始化脚本
config.toml      采集与部署配置、初始监测对象
requirements.lock  Python 3.12 运行依赖与校验哈希
```
