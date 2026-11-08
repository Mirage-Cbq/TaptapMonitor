import asyncio
from dataclasses import replace
import logging
from pathlib import Path

from .client import SourceError, TapTapClient
from .config import Settings
from .content import Item, comment_item, review_item, source_reply_count, text_of, media_of
from .store import Store
from .media import MediaCache

log = logging.getLogger(__name__)


class Collector:
    def __init__(self, store: Store, client: TapTapClient, settings: Settings):
        self.store, self.client, self.settings = store, client, settings
        self.lock = asyncio.Lock()
        self.counts = {"created": 0, "updated": 0}
        self.errors = []
        self.mode = "incremental"
        self.media = MediaCache(Path(settings.database).parent / "media") if settings.cache_images else None

    def save(self, item):
        action = self.store.save_item(item)
        if action:
            self.counts[action] += 1

    async def walk(self, key, endpoint, params, consume, newest_first=True, page_limit=None):
        state = self.store.stream(key)
        cursor = state["next_url"] if state else ""
        incremental = self.mode == "incremental" and newest_first and self.completed(state)
        try:
            # Refresh the head while finishing a capped/interrupted historical scan.
            if cursor:
                await consume(await self.client.get(endpoint, params))
            seen = set()
            for _ in range(page_limit or self.settings.max_pages):
                if cursor and cursor in seen:
                    raise SourceError("Repeated pagination cursor")
                seen.add(cursor)
                data = await self.client.get(cursor or endpoint, None if cursor else params)
                if "list" not in data and data.get("total") == 0:
                    data["list"] = []
                if not isinstance(data.get("list"), list):
                    raise SourceError("List response changed shape")
                before = self.counts.copy()
                await consume(data)
                next_url = data.get("next_page") or ""
                if next_url:
                    self.client.safe_url(next_url)
                cursor = "" if incremental and self.counts == before else next_url
                self.store.save_stream(key, cursor, "pending" if cursor else "ok")
                if not cursor:
                    return
            message = f"{key}: page budget reached; continuation saved"
            self.errors.append(message)
            log.warning(message)
        except Exception as exc:
            message = f"{key}: {exc}"
            self.store.save_stream(key, cursor, "error", str(exc))
            self.errors.append(message)
            log.warning(message)

    @staticmethod
    def completed(state):
        return bool(state and state["status"] in ("ok", "restricted") and not state["next_url"])

    def needs_comments(self, item, cached, key):
        if item.kind in ("review", "topic"):
            prefix = f"replies:{item.app_id}:{item.kind}:{item.source_id}:"
            if self.store.query("SELECT 1 FROM streams WHERE substr(key,1,?)=? AND (status NOT IN ('ok','restricted') OR next_url!='') LIMIT 1", (len(prefix), prefix)):
                return True
        return (self.mode == "full" or cached is None or cached.get("source_reply_count") != item.source_reply_count
                or not self.completed(self.store.stream(key)))

    async def collect(self, developer_id=None, run_id=None, mode="incremental"):
        if mode not in ("incremental", "full"):
            raise ValueError("采集模式必须为 incremental 或 full")
        async with self.lock:
            self.mode = mode
            run_id = run_id or self.store.start_run(developer_id, mode)
            self.counts = {"created": 0, "updated": 0}
            self.errors = []
            try:
                developers = self.store.developers(enabled_only=True)
                for developer in developers:
                    if developer_id is not None and developer["id"] != developer_id:
                        continue
                    await self.developer(developer["id"])
            except asyncio.CancelledError:
                self.errors.append("Collection interrupted; saved page cursors will resume")
                raise
            except Exception as exc:
                log.exception("Collection failed")
                self.errors.append(str(exc))
            finally:
                self.store.finish_run(run_id, self.counts, self.errors)
            return {"run_id": run_id, "mode": mode, **self.counts, "errors": self.errors}

    async def developer(self, developer_id):
        try:
            data = await self.client.get("developer/v1/detail", {"developer_id": developer_id})
            self.store.save_profile(developer_id, data["developer"])
        except Exception as exc:
            self.errors.append(f"developer:{developer_id}: {exc}")

        async def games(data):
            for app in data["list"]:
                self.store.save_game(developer_id, app)

        await self.walk(f"games:{developer_id}", "app-list/v1/detail", {"developer_id": developer_id, "limit": 20}, games, newest_first=False)
        for game in self.store.games(developer_id):
            app_id = game["id"]
            try:
                detail = await self.client.get("group/v1/detail", {"app_id": app_id})
                group_id = detail["group"]["id"]
                # Keep the child app's identity even when the forum belongs to a parent collection.
                app = {**game["metadata"], **detail.get("app", {}), "id": app_id, "title": game["title"]}
                if self.media:
                    app["_media"] = await self.media.game(app)
                self.store.save_game(developer_id, app, group_id)
            except Exception as exc:
                self.errors.append(f"group:{app_id}: {exc}")
                group_id = game["group_id"]
            await self.reviews(app_id)
            if group_id:
                await self.topics(app_id, group_id)

    async def reviews(self, app_id):
        async def consume(data):
            for entry in data["list"]:
                moment = entry.get("moment")
                if not moment or not moment.get("review"):
                    raise SourceError("Unexpected review entry")
                item = review_item(app_id, moment)
                cached = self.store.get_item(app_id, "review", item.source_id)
                self.save(item)
                if self.settings.collect_comments and self.needs_comments(item, cached, f"comments:{app_id}:review:{item.source_id}"):
                    await self.comments(item, "review-comment/v1/by-review", {"review_id": item.source_id, "order": "asc", "limit": 10})

        await self.walk(f"reviews:{app_id}", "review/v2/list-by-app", {"app_id": app_id, "sort": "new", "limit": 10}, consume)

    async def topics(self, app_id, group_id):
        async def consume(data):
            for entry in data["list"]:
                moment = entry.get("moment", {})
                topic = moment.get("topic")
                if not topic or not topic.get("id_str"):
                    # Reposts are not original game feedback and have no own topic body.
                    continue
                source_id = str(topic["id_str"])
                cached = self.store.get_item(app_id, "topic", source_id)
                updated = moment.get("edited_time", moment["created_time"])
                if cached and cached["source_updated_at"] == updated:
                    count = source_reply_count(moment.get("stat"), topic.get("stat"), topic)
                    item = replace(Item(**cached), source_reply_count=count if count is not None else cached.get("source_reply_count"))
                else:
                    detail = await self.client.get("topic/v1/detail", {"id": source_id})
                    user = moment["author"]["user"]
                    item = Item(
                        app_id, "topic", source_id, topic.get("title", ""), text_of(detail["first_post"].get("contents")),
                        f"https://www.taptap.cn/moment/{moment['id_str']}", user["name"], str(user["id"]),
                        published_at=moment["created_time"], source_updated_at=updated,
                        official=moment.get("is_official", False),
                        media=media_of(detail["first_post"].get("contents")),
                        source_reply_count=source_reply_count(moment.get("stat"), topic.get("stat"), topic, detail.get("stat"), detail),
                    )
                self.save(item)
                if self.settings.collect_comments and self.needs_comments(item, cached, f"comments:{app_id}:topic:{item.source_id}"):
                    await self.comments(item, "moment-comment/v1/by-moment", {
                        "moment_id": moment["id_str"], "group_id": group_id,
                        "sort": "rank", "order": "desc", "limit": 10,
                    })

        await self.walk(f"topics:{app_id}", "feed/v7/by-group", {"group_id": group_id, "type": "feed", "sort": "created", "limit": 10}, consume)

    async def replies(self, moment_id, comment, max_pages=30, parent=None):
        params = {"id": str(comment.get("id_str") or comment["id"]), "moment_id": moment_id,
                  "from": 0, "limit": 10, "sort": "created_at", "order": "asc"}
        key = (f"replies:{parent.app_id}:{parent.kind}:{parent.source_id}:{params['id']}" if parent
               else f"replies:{moment_id}:{params['id']}")
        result = []

        async def consume(data):
            result.extend(data["list"])
            if parent:
                kind = "review_comment" if parent.kind == "review" else "topic_comment"
                for child in data["list"]:
                    self.save(comment_item(parent.app_id, kind, parent.source_id, child, parent.url))

        await self.walk(key, "moment-comment/v1/by-comment", params, consume, newest_first=False,
                        page_limit=min(max_pages, self.settings.max_pages))
        return result

    async def comments(self, parent, endpoint, params):
        kind = "review_comment" if parent.kind == "review" else "topic_comment"
        key = f"comments:{parent.app_id}:{parent.kind}:{parent.source_id}"
        restricted = []

        async def consume(data):
            for comment in data["list"]:
                item = comment_item(parent.app_id, kind, parent.source_id, comment, parent.url)
                cached = self.store.get_item(parent.app_id, kind, item.source_id)
                self.save(item)
                children = comment.get("child_posts") or []
                for child in children:
                    self.save(comment_item(parent.app_id, kind, parent.source_id, child, parent.url))
                nested_key = f"replies:{parent.app_id}:{parent.kind}:{parent.source_id}:{item.source_id}"
                fetch_nested = self.needs_comments(item, cached, nested_key)
                nested_state = self.store.stream(nested_key)
                resumed = bool(nested_state and nested_state["next_url"])
                if (item.source_reply_count or 0) > len(children) and params.get("moment_id") and fetch_nested:
                    # The list only previews nested replies; this endpoint serves all of them anonymously.
                    try:
                        children = await self.replies(params["moment_id"], comment, parent=parent)
                    except Exception as exc:
                        self.store.save_stream(nested_key, status="error", error=str(exc))
                        log.warning("nested replies of %s: %s", comment.get("id"), exc)
                elif fetch_nested and (item.source_reply_count or 0) <= len(children):
                    self.store.save_stream(nested_key)
                if fetch_nested and not resumed and (item.source_reply_count or 0) > len(children):
                    restricted.append(str(comment.get("id")))

        await self.walk(key, endpoint, params, consume, newest_first=False)
        state = self.store.stream(key)
        if restricted and state and state["status"] == "ok":
            message = "TapTap requires login to expand nested replies; collected public preview only: " + ",".join(restricted)
            self.store.save_stream(key, status="restricted", error=message)
            self.errors.append(f"{key}: {message}")
