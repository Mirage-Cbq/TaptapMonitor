"""Feedback analysis; Wilson/MAD references and product thresholds are documented in README."""
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
import math
import re
from statistics import mean, median


METHODS = [
    {"key": "bayesian", "name": "贝叶斯评分", "description": "以全部监测评价的历史均分为先验，权重20，减轻小样本评分波动；无历史评分时先验为3.5星。"},
    {"key": "wilson", "name": "好评率置信下界", "description": "4–5星为好评、1–2星为差评，3星为中评；Wilson 95%下界体现好评率的不确定性。"},
    {"key": "rating_alert", "name": "评分下降提醒", "description": "近7天与前7天各有至少5条评分且均分下降≥0.5星，或近7天至少5条评分且差评率≥40%时提醒。"},
    {"key": "mad", "name": "反馈量异常", "description": "以此前14天的中位数和MAD计算稳健Z分数（MAD下限1），当分数≥3.5且当日数量≥5时标记。"},
    {"key": "keywords", "name": "口碑关键词", "description": "统计评价中2–4字中文短语和英文词的文档频次，至少3条评价出现；结合正负评价间的提升度排序，不代表因果。"},
    {"key": "response", "name": "官方响应", "description": "按已采集的官方评价回复计算回复率与首次响应耗时中位数；未采集到的回复不会计入。"},
    {"key": "growth", "name": "指标净变化", "description": "窗口内同一字段末值减首值，优先官方日指标，否则使用公开快照；这是观测值变化，不等同于新增用户。"},
]
STOPWORDS = ("的了是我也就都很还在有和不这那个们吗吧啊呢 游戏 一个 没有 什么 可以 感觉 还是 就是 但是 因为 所以 这个 那个 这种 一种 真的 非常 自己 你们 我们 他们 以及 已经 如果 只是 不是 这样 那样 比较 怎么 为什么"
             " 时候 有点 就能 开始 时间 现在 一下 一些 有些 还有 然后 之后 以后 之前 出来 起来 东西 地方 希望 觉得 作者 大家 一直 一样 不过 而且 虽然 可能 应该 需要 能够 不能 不会 还能 而已 一点 很多 太多 这么 那么 其实 确实 直接 一次 每次 目前 没什么").split()
FUNCTION_CHARS = set(STOPWORDS[0])
STOP_PATTERN = re.compile("(?:" + "|".join(sorted(STOPWORDS[1:] + list(FUNCTION_CHARS), key=len, reverse=True)) + ")+")


def average(values):
    return mean(values) if values else None


def scored(item):
    score = item.get("score")
    return item["kind"] == "review" and type(score) in (int, float) and 1 <= score <= 5


def wilson(positive, total):
    if not total:
        return None
    p, z = positive / total, 1.96
    return max(0, (p + z * z / (2 * total) - z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))) / (1 + z * z / total))


def terms(body):
    result = set(re.findall(r"[a-z]{3,}", body.lower()))
    for phrase in re.findall(r"[\u4e00-\u9fff]+", body):
        for size in (2, 3, 4):
            result.update(phrase[start:start + size] for start in range(len(phrase) - size + 1))
    return {term for term in result if not STOP_PATTERN.fullmatch(term)}


def keywords(reviews):
    positive = [review for review in reviews if review["score"] >= 4]
    negative = [review for review in reviews if review["score"] <= 2]
    pos, neg = (Counter(term for review in group for term in terms(review.get("body", ""))) for group in (positive, negative))

    def ranked(counts, other, total, other_total):
        candidates = {term: count for term, count in counts.items() if count >= 3}
        removed = set()
        lengths = sorted({len(term) for term in candidates})
        for longer in sorted(candidates, key=lambda term: (-len(term), term)):
            if longer in removed:
                continue
            for size in lengths:
                if size >= len(longer):
                    break
                for start in range(len(longer) - size + 1):
                    term = longer[start:start + size]
                    if term in candidates and candidates[longer] >= 0.8 * candidates[term]:
                        removed.add(term)
        rows = [{"term": term, "count": count, "lift": (count / total) / ((other[term] + 1) / (other_total + 1))}
                for term, count in candidates.items() if term not in removed]
        # Only terms clearly more typical of this side than the other are informative.
        rows = [row for row in rows if row["lift"] >= 1.2]
        return sorted(rows, key=lambda row: (-row["count"] * math.log1p(row["lift"]), -row["count"], row["term"]))[:20]

    return {"negative": ranked(neg, pos, len(negative), len(positive)),
            "positive": ranked(pos, neg, len(positive), len(negative))}


def growth(store, games, since, until):
    ids = [game["id"] for game in games]
    sources = {source: defaultdict(list) for source in ("official", "public")}
    for source, grouped in sources.items():
        for row in store.metrics(ids, source, since):
            if row["day"] <= until:
                grouped[row["app_id"]].append(row)

    def delta(rows, fields):
        for field in fields:
            values = [row[field] for row in rows if type(row.get(field)) in (int, float) and math.isfinite(row[field])]
            if len(values) >= 2:
                return values[-1] - values[0]
        return None

    result = []
    for game in games:
        official = sources["official"][game["id"]]
        source = "official" if any(delta(official, [field]) is not None for field in ("page_view_count", "download_count", "download_request_count")) else "public"
        rows = sources[source][game["id"]]
        result.append({"app_id": game["id"], "title": game["title"], "source": source,
                       "fans_delta": delta(rows, ["favorite_count"] if source == "official" else ["fans_count"]),
                       "hits_delta": delta(rows, ["page_view_count"] if source == "official" else ["hits_total"]),
                       "reviews_delta": delta(rows, ["review_count"]),
                       "downloads_delta": delta(rows, ["download_count", "download_request_count"]),
                       "days": (date.fromisoformat(rows[-1]["day"]) - date.fromisoformat(rows[0]["day"])).days + 1 if rows else 0})
    return result


def analyze(store, days=30, developer_id=None, app_id=None):
    today = date.today()
    start, end = today - timedelta(days=days - 1), today + timedelta(days=1)
    since, until = (int(datetime.combine(day, time()).timestamp()) for day in (start, end))
    lookback = int(datetime.combine(start - timedelta(days=14), time()).timestamp())
    where, args = store.item_scope(developer_id, app_id)
    clause = " AND ".join(where) + " AND i.kind IN ('review','topic')"
    rows = store.query("SELECT i.*,g.title game_title," + store.reply_count_sql() + " reply_count FROM items i "
                       "LEFT JOIN games g ON g.id=i.app_id WHERE " + clause +
                       " AND json_extract(i.payload,'$.published_at')>=? AND json_extract(i.payload,'$.published_at')<?",
                       (*args, lookback, until))
    history = [store.item_row(row) for row in rows]
    items = [item for item in history if item["published_at"] >= since]
    reviews = [item for item in items if item["kind"] == "review"]
    ratings = [item for item in reviews if scored(item)]
    prior = store.query("SELECT AVG(json_extract(payload,'$.score')) mean FROM items WHERE kind='review' "
                        "AND json_extract(payload,'$.score') BETWEEN 1 AND 5 AND app_id IN (SELECT app_id FROM developer_games)")[0]["mean"]
    prior = prior if prior is not None else 3.5
    scores = [item["score"] for item in ratings]
    n = len(scores)
    recent_start = int(datetime.combine(today - timedelta(days=6), time()).timestamp())
    previous_start = int(datetime.combine(today - timedelta(days=13), time()).timestamp())
    recent = [item["score"] for item in history if scored(item) and item["published_at"] >= recent_start]
    previous = [item["score"] for item in history if scored(item) and previous_start <= item["published_at"] < recent_start]
    recent_mean, previous_mean = average(recent), average(previous)
    alerts = []
    if len(recent) >= 5 and len(previous) >= 5 and previous_mean - recent_mean >= 0.5:
        alerts.append(f"近7天均分较前7天下降 {previous_mean - recent_mean:.2f} 星")
    if len(recent) >= 5 and sum(score <= 2 for score in recent) / len(recent) >= 0.4:
        alerts.append("近7天差评率达到40%或以上，请关注玩家反馈")
    rating = {"count": n, "mean": average(scores), "bayesian": (20 * prior + sum(scores)) / (20 + n) if n else None,
              "prior_mean": prior, "prior_weight": 20, "positive_rate": sum(score >= 4 for score in scores) / n if n else None,
              "negative_rate": sum(score <= 2 for score in scores) / n if n else None,
              "wilson_lower": wilson(sum(score >= 4 for score in scores), n),
              "recent_mean": recent_mean, "previous_mean": previous_mean, "alert": "；".join(alerts) or None}
    weeks = defaultdict(list)
    daily_volume, daily_negative = Counter(), Counter()
    for item in history:
        day = datetime.fromtimestamp(item["published_at"]).date()
        daily_volume[day] += 1
        if scored(item) and item["score"] <= 2:
            daily_negative[day] += 1
        if item["published_at"] >= since:
            weeks[day - timedelta(days=day.weekday())].append(item)
    weekly = []
    week = start - timedelta(days=start.weekday())
    while week <= today:
        group = weeks[week]
        values = [item["score"] for item in group if scored(item)]
        weekly.append({"week_start": week.isoformat(), "reviews": sum(item["kind"] == "review" for item in group),
                       "topics": sum(item["kind"] == "topic" for item in group), "mean": average(values),
                       "positive_rate": sum(score >= 4 for score in values) / len(values) if values else None,
                       "negative_rate": sum(score <= 2 for score in values) / len(values) if values else None})
        week += timedelta(days=7)
    anomalies = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        for kind, series in (("volume", daily_volume), ("negative", daily_negative)):
            baseline = median(series[day - timedelta(days=before)] for before in range(1, 15))
            mad = median(abs(series[day - timedelta(days=before)] - baseline) for before in range(1, 15))
            score = 0.6745 * (series[day] - baseline) / max(1, mad)
            if score >= 3.5 and series[day] >= 5:
                anomalies.append({"day": day.isoformat(), "type": kind, "count": series[day], "baseline": baseline, "score": score})
    comments = store.query("SELECT c.app_id,json_extract(c.payload,'$.parent_id') parent_id,"
                           "json_extract(c.payload,'$.published_at') published_at FROM items c JOIN items i ON i.app_id=c.app_id "
                           "AND i.source_id=json_extract(c.payload,'$.parent_id') AND i.kind='review' WHERE " + clause +
                           " AND json_extract(i.payload,'$.published_at')>=? AND json_extract(i.payload,'$.published_at')<?"
                           " AND c.kind='review_comment' AND json_extract(c.payload,'$.official')=1"
                           " AND json_extract(c.payload,'$.published_at')<?", (*args, since, until, until))
    replies = defaultdict(list)
    for comment in comments:
        replies[(comment["app_id"], comment["parent_id"])].append(comment["published_at"])
    durations, replied = [], 0
    for review in reviews:
        timestamps = replies[(review["app_id"], review["source_id"])]
        if timestamps:
            replied += 1
            valid = [stamp for stamp in timestamps if stamp >= review["published_at"]]
            if valid:
                durations.append((min(valid) - review["published_at"]) / 3600)
    hot = sorted(items, key=lambda item: (-item["reply_count"], -item["published_at"], item["app_id"], item["source_id"]))[:10]
    games = [game for game in store.games(developer_id) if app_id is None or game["id"] == app_id]
    return {"window": {"days": days, "since": since, "until": until}, "rating": rating, "weekly": weekly,
            "anomalies": anomalies, "keywords": keywords(ratings),
            "response": {"total": len(reviews), "replied": replied, "official_reply_rate": replied / len(reviews) if reviews else None,
                         "median_hours": median(durations) if durations else None},
            "hot_topics": [{key: item[key] for key in ("app_id", "source_id", "title", "kind", "reply_count", "source_reply_count", "url", "published_at", "game_title")} | {"excerpt": (item.get("body") or "")[:80]} for item in hot],
            "growth": growth(store, games, start.isoformat(), today.isoformat()), "methods": METHODS}
