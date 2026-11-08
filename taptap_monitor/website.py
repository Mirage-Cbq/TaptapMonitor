import math

from .content import text_of


def _number(value, *, positive=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and (number > 0 if positive else number >= 0) else None


def _stats(app):
    stat = app.get("stat") or {}
    rating = stat.get("rating") or {}
    votes = stat.get("vote_info") or {}
    return {
        "rating": {key: _number(rating.get(key), positive=True) for key in ("score", "max")},
        "vote_info": {str(star): int(_number(votes.get(str(star))) or 0) for star in range(1, 6)},
        **{key: int(value) if (value := _number(stat.get(key))) is not None else None
           for key in ("review_count", "topic_count", "fans_count", "hits_total")},
    }


def site_data(store, developer_id, include_hidden=False):
    developer = store.developer(developer_id)
    if not developer:
        return None
    presentation = store.presentation(developer_id)
    custom, source = presentation["custom"], presentation["source"]
    if not include_hidden and not custom.get("published", True):
        return None
    options = store.game_options(developer_id)
    games = []
    for row in store.games(developer_id):
        app, override = row["metadata"], options.get(row["id"], {})
        if not include_hidden and not override.get("visible", True):
            continue
        image = app.get("ad_banner") or app.get("cover") or app.get("icon") or {}
        icon = app.get("icon") or {}
        media = app.get("_media") or {}
        games.append({
            "id": row["id"], "title": override.get("title") or row["title"],
            "description": override.get("description") or text_of(app.get("description")),
            "cover": media.get("cover") or image.get("original_url") or image.get("url", ""),
            "icon": media.get("icon") or icon.get("medium_url") or icon.get("url", ""),
            "tags": [tag["value"] for tag in app.get("tags", []) if tag.get("value")],
            "url": f"https://www.taptap.cn/app/{row['id']}",
            "forum_url": f"https://www.taptap.cn/app/{row['id']}/topic",
            "updated_at": row["updated_at"],
            "stats": _stats(app),
            "visible": override.get("visible", True), "featured": override.get("featured", False),
            "sort_order": override.get("sort_order", 0), "overrides": override if include_hidden else None,
            "source": {"title": row["title"], "description": text_of(app.get("description"))} if include_hidden else None,
        })
    games.sort(key=lambda game: (not game["featured"], game["sort_order"], -game["id"]))
    avatar = source.get("avatar") or {}
    return {
        "developer_id": developer_id,
        "name": custom.get("display_name") or developer["name"],
        "logo": custom.get("logo_url") or avatar.get("original_url") or avatar.get("url") or "/favicon.svg",
        "tagline": custom.get("tagline") or "小小的灵感，好玩的世界。",
        "intro": custom.get("intro") or text_of({"text": source.get("intro", "")}) or "从日常里寻找灵感，把想法变成可以亲手体验的游戏。",
        "about": custom.get("about") or "收集、成长、探索。在不同的游戏里，找到属于自己的节奏。欢迎在 TapTap 分享你的体验与建议。",
        "taptap_url": f"https://www.taptap.cn/developer/{developer_id}",
        "games": games, "custom": custom if include_hidden else None,
        "synced_at": store.last_synced(developer_id),
    }
