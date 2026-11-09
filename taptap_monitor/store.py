import datetime
from contextlib import contextmanager
from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3
import time

from .content import Item


def _figure(value):
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def public_snapshot(stat):
    """Daily figures any visitor can see on the game page; recorded so trends exist without login."""
    rating = stat.get("rating") or {}
    snapshot = {key: _figure(stat.get(key)) for key in ("review_count", "topic_count", "fans_count", "hits_total", "reserve_count", "download_count")}
    snapshot["rating"] = _figure(rating.get("score"))
    snapshot["votes"] = {str(star): _figure((stat.get("vote_info") or {}).get(str(star))) or 0 for star in range(1, 6)}
    return {key: value for key, value in snapshot.items() if value is not None}


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS developers (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    target_umo TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                    digest_cursor INTEGER NOT NULL DEFAULT 0, digest_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS games (
                    id INTEGER PRIMARY KEY, title TEXT NOT NULL, group_id INTEGER,
                    metadata TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS developer_games (
                    developer_id INTEGER REFERENCES developers(id), app_id INTEGER REFERENCES games(id),
                    PRIMARY KEY (developer_id, app_id)
                );
                CREATE TABLE IF NOT EXISTS sites (
                    developer_id INTEGER PRIMARY KEY REFERENCES developers(id),
                    custom TEXT NOT NULL DEFAULT '{}', source TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS game_settings (
                    developer_id INTEGER NOT NULL, app_id INTEGER NOT NULL,
                    custom TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(developer_id,app_id)
                );
                CREATE TABLE IF NOT EXISTS items (
                    app_id INTEGER NOT NULL, kind TEXT NOT NULL, source_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                    first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                    PRIMARY KEY (app_id, kind, source_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER NOT NULL,
                    kind TEXT NOT NULL, source_id TEXT NOT NULL, action TEXT NOT NULL,
                    baseline INTEGER NOT NULL, observed_at REAL NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_app_id ON events(app_id, id);
                CREATE TABLE IF NOT EXISTS streams (
                    key TEXT PRIMARY KEY, next_url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                    last_attempt REAL NOT NULL, last_success REAL, error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, developer_id INTEGER,
                    mode TEXT NOT NULL DEFAULT 'incremental',
                    started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
                    new_items INTEGER NOT NULL DEFAULT 0, updated_items INTEGER NOT NULL DEFAULT 0,
                    errors TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, developer_id INTEGER NOT NULL,
                    from_cursor INTEGER NOT NULL, to_cursor INTEGER NOT NULL, target_umo TEXT NOT NULL,
                    body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, sent_at REAL,
                    error TEXT NOT NULL DEFAULT '', sent_parts INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(developer_id, from_cursor, to_cursor)
                );
                CREATE TABLE IF NOT EXISTS metrics (
                    app_id INTEGER NOT NULL, source TEXT NOT NULL, day TEXT NOT NULL,
                    data TEXT NOT NULL, captured_at REAL NOT NULL,
                    PRIMARY KEY (app_id, source, day)
                );
                CREATE TABLE IF NOT EXISTS official_reports (
                    app_id INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, fetched_at REAL NOT NULL,
                    PRIMARY KEY (app_id, kind)
                );
                CREATE INDEX IF NOT EXISTS items_parent ON items(app_id,kind,json_extract(payload,'$.parent_id'));
                CREATE INDEX IF NOT EXISTS items_published ON items(app_id,kind,json_extract(payload,'$.published_at'));
            """)
            if "mode" not in {row["name"] for row in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'incremental'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.create_function("casefold", 1, lambda value: value.casefold(), deterministic=True)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def query(self, sql: str, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def add_developer(self, developer_id: int, name: str = "", target_umo: str = ""):
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO developers(id,name,target_umo,created_at,digest_at,digest_cursor) VALUES(?,?,?,?,?,(SELECT COALESCE(MAX(id),0) FROM events))",
                (developer_id, name or str(developer_id), target_umo, now, now),
            )
            db.execute("INSERT OR IGNORE INTO sites(developer_id) VALUES(?)", (developer_id,))

    def developers(self, enabled_only=False):
        return self.query("SELECT * FROM developers" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id")

    def developer(self, developer_id):
        rows = self.query("SELECT * FROM developers WHERE id=?", (developer_id,))
        return rows[0] if rows else None

    def update_developer(self, developer_id, **fields):
        if not fields:
            return
        if not set(fields) <= {"name", "enabled", "target_umo"}:
            raise ValueError("Unsupported developer setting")
        with self.connect() as db:
            db.execute("UPDATE developers SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?", (*fields.values(), developer_id))
            if "name" in fields:
                db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (f"developer_name:{developer_id}", json.dumps(fields["name"], ensure_ascii=False)))

    def delete_developer(self, developer_id):
        """Remove a developer, its site and the games no other developer monitors (with their data)."""
        with self.connect() as db:
            owned = [row[0] for row in db.execute(
                """SELECT app_id FROM developer_games WHERE developer_id=? AND app_id NOT IN
                   (SELECT app_id FROM developer_games WHERE developer_id!=?)""", (developer_id, developer_id))]
            removed = {"games": len(owned), "items": 0}
            for table in ("developer_games", "sites", "game_settings", "deliveries"):  # links first: foreign keys are on
                db.execute(f"DELETE FROM {table} WHERE developer_id=?", (developer_id,))
            for app_id in owned:
                removed["items"] += db.execute("SELECT COUNT(*) FROM items WHERE app_id=? AND kind IN ('review','topic')", (app_id,)).fetchone()[0]
                for table in ("items", "events", "metrics", "official_reports", "game_settings"):
                    db.execute(f"DELETE FROM {table} WHERE app_id=?", (app_id,))
                db.execute("DELETE FROM games WHERE id=?", (app_id,))
                for prefix in (f"reviews:{app_id}", f"topics:{app_id}", f"comments:{app_id}:", f"replies:{app_id}:"):
                    db.execute("DELETE FROM streams WHERE key=? OR substr(key,1,?)=?", (prefix, len(prefix), prefix))
            db.execute("DELETE FROM streams WHERE key=?", (f"games:{developer_id}",))
            db.execute("DELETE FROM settings WHERE key=?", (f"developer_name:{developer_id}",))
            db.execute("DELETE FROM settings WHERE key='primary_site' AND value=?", (str(developer_id),))
            db.execute("DELETE FROM developers WHERE id=?", (developer_id,))
        # Remember the removal so a developer listed in config.toml is not re-seeded on restart.
        self.save_setting("removed_developers", sorted({*self.setting("removed_developers", []), developer_id}))
        return removed

    def save_game(self, developer_id: int, app: dict, group_id=None):
        # Keep only public game metadata, without analytics/tracking payloads.
        metadata = {k: app[k] for k in ("id", "title", "icon", "cover", "ad_banner", "description", "stat", "is_level", "tags", "_media") if k in app}
        with self.connect() as db:
            db.execute("""INSERT INTO games(id,title,group_id,metadata,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                group_id=COALESCE(excluded.group_id,games.group_id), metadata=excluded.metadata, updated_at=excluded.updated_at""",
                (app["id"], app["title"], group_id, json.dumps(metadata, ensure_ascii=False), time.time()))
            db.execute("INSERT OR IGNORE INTO developer_games VALUES(?,?)", (developer_id, app["id"]))
        if isinstance(app.get("stat"), dict):
            self.save_metrics(app["id"], "public", public_snapshot(app["stat"]))

    def save_metrics(self, app_id, source, data, day=None):
        """Merge one day's figures for a game; source is 'public' (anonymous) or 'official' (open API)."""
        day = day or datetime.date.today().isoformat()
        with self.connect() as db:
            row = db.execute("SELECT data FROM metrics WHERE app_id=? AND source=? AND day=?", (app_id, source, day)).fetchone()
            merged = {**(json.loads(row[0]) if row else {}), **data}
            db.execute("INSERT INTO metrics VALUES(?,?,?,?,?) ON CONFLICT(app_id,source,day) DO UPDATE SET data=excluded.data,captured_at=excluded.captured_at",
                       (app_id, source, day, json.dumps(merged, ensure_ascii=False), time.time()))

    def delete_metrics(self, app_id, source, before):
        with self.connect() as db:
            db.execute("DELETE FROM metrics WHERE app_id=? AND source=? AND day<?", (app_id, source, before))

    def metrics(self, app_ids, source, since=None):
        if not app_ids:
            return []
        marks = ",".join("?" * len(app_ids))
        rows = self.query(f"SELECT app_id,day,data FROM metrics WHERE source=? AND app_id IN ({marks}) AND day>=? ORDER BY day",
                          (source, *app_ids, since or ""))
        return [{"app_id": row["app_id"], "day": row["day"], **json.loads(row["data"])} for row in rows]

    def save_report(self, app_id, kind, payload):
        with self.connect() as db:
            db.execute("INSERT INTO official_reports VALUES(?,?,?,?) ON CONFLICT(app_id,kind) DO UPDATE SET payload=excluded.payload,fetched_at=excluded.fetched_at",
                       (app_id, kind, json.dumps(payload, ensure_ascii=False), time.time()))

    def reports(self, app_id):
        return {row["kind"]: {"data": json.loads(row["payload"]), "fetched_at": row["fetched_at"]}
                for row in self.query("SELECT * FROM official_reports WHERE app_id=?", (app_id,))}

    def save_profile(self, developer_id, profile):
        source = {k: profile[k] for k in ("name", "intro", "avatar", "website") if k in profile}
        with self.connect() as db:
            db.execute("""UPDATE developers SET name=COALESCE(
                (SELECT json_extract(value,'$') FROM settings WHERE key=?),?) WHERE id=?""",
                       (f"developer_name:{developer_id}", profile["name"], developer_id))
            db.execute("UPDATE sites SET source=? WHERE developer_id=?", (json.dumps(source, ensure_ascii=False), developer_id))

    def presentation(self, developer_id):
        rows = self.query("SELECT custom,source FROM sites WHERE developer_id=?", (developer_id,))
        return {"custom": json.loads(rows[0]["custom"]), "source": json.loads(rows[0]["source"])} if rows else {"custom": {}, "source": {}}

    def save_presentation(self, developer_id, fields, app_id=None):
        with self.connect() as db:
            if app_id is None:
                row = db.execute("SELECT custom FROM sites WHERE developer_id=?", (developer_id,)).fetchone()
                custom = json.loads(row[0])
                custom.update(fields)
                db.execute("UPDATE sites SET custom=? WHERE developer_id=?", (json.dumps(custom, ensure_ascii=False), developer_id))
            else:
                row = db.execute("SELECT custom FROM game_settings WHERE developer_id=? AND app_id=?", (developer_id, app_id)).fetchone()
                custom = json.loads(row[0]) if row else {}
                custom.update(fields)
                db.execute("INSERT INTO game_settings VALUES(?,?,?) ON CONFLICT(developer_id,app_id) DO UPDATE SET custom=excluded.custom",
                           (developer_id, app_id, json.dumps(custom, ensure_ascii=False)))

    def game_options(self, developer_id):
        return {row["app_id"]: json.loads(row["custom"]) for row in self.query("SELECT * FROM game_settings WHERE developer_id=?", (developer_id,))}

    def games(self, developer_id=None):
        if developer_id is None:
            rows = self.query("SELECT * FROM games ORDER BY id DESC")
        else:
            rows = self.query("SELECT g.* FROM games g JOIN developer_games d ON g.id=d.app_id WHERE d.developer_id=? ORDER BY g.id DESC", (developer_id,))
        for row in rows:
            row["metadata"] = json.loads(row["metadata"])
        return rows

    def get_item(self, app_id, kind, source_id):
        rows = self.query("SELECT payload FROM items WHERE app_id=? AND kind=? AND source_id=?", (app_id, kind, str(source_id)))
        return json.loads(rows[0]["payload"]) if rows else None

    def save_item(self, item: Item):
        now, fingerprint = time.time(), item.fingerprint()
        payload = json.dumps(item.payload(), ensure_ascii=False)
        with self.connect() as db:
            old = db.execute("SELECT fingerprint FROM items WHERE app_id=? AND kind=? AND source_id=?", (item.app_id, item.kind, item.source_id)).fetchone()
            action = "created" if old is None else "updated" if old["fingerprint"] != fingerprint else None
            db.execute("""INSERT INTO items VALUES(?,?,?,?,?,?,?) ON CONFLICT(app_id,kind,source_id)
                DO UPDATE SET fingerprint=excluded.fingerprint,payload=excluded.payload,last_seen=excluded.last_seen""",
                (item.app_id, item.kind, item.source_id, fingerprint, payload, now, now))
            if action:
                subscribed = db.execute("""SELECT MIN(d.created_at) FROM developers d
                    JOIN developer_games g ON g.developer_id=d.id WHERE g.app_id=?""", (item.app_id,)).fetchone()[0]
                baseline = action == "created" and bool(subscribed and item.published_at <= subscribed)
                db.execute("INSERT INTO events(app_id,kind,source_id,action,baseline,observed_at,payload) VALUES(?,?,?,?,?,?,?)",
                           (item.app_id, item.kind, item.source_id, action, int(baseline), now, payload))
        return action

    def items(self, developer_id=None, app_id=None, kind=None, limit=100, offset=0):
        where, args = self.item_scope(developer_id, app_id, kind)
        rows = self.query("SELECT i.*,g.title game_title," + self.reply_count_sql() + " reply_count FROM items i LEFT JOIN games g ON g.id=i.app_id WHERE "
                          + " AND ".join(where) + " ORDER BY json_extract(i.payload,'$.published_at') DESC,i.source_id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
        return [self.item_row(row) for row in rows]

    @staticmethod
    def reply_count_sql():
        return """(SELECT COUNT(*) FROM items c WHERE c.app_id=i.app_id
            AND c.kind=CASE i.kind WHEN 'review' THEN 'review_comment' WHEN 'topic' THEN 'topic_comment' END
            AND json_extract(c.payload,'$.parent_id')=i.source_id)"""

    @staticmethod
    def item_row(row):
        return {"source_reply_count": None, **json.loads(row["payload"]), "reply_count": row["reply_count"],
                "game_title": row["game_title"], "first_seen": row["first_seen"], "last_seen": row["last_seen"]}

    def replies(self, app_id, kind, source_id, limit=50, offset=0):
        comment_kind = "review_comment" if kind == "review" else "topic_comment"
        where = "i.app_id=? AND i.kind=? AND json_extract(i.payload,'$.parent_id')=?"
        args = (app_id, comment_kind, str(source_id))
        with self.connect() as db:
            db.execute("BEGIN")
            total = db.execute("SELECT COUNT(*) FROM items i WHERE " + where, args).fetchone()[0]
            rows = db.execute("SELECT i.*,g.title game_title,0 reply_count FROM items i LEFT JOIN games g ON g.id=i.app_id WHERE "
                              + where + " ORDER BY json_extract(i.payload,'$.published_at'),i.source_id LIMIT ? OFFSET ?",
                              (*args, limit, offset)).fetchall()
        return {"total": total, "items": [self.item_row(row) for row in rows]}

    def setting(self, key, default=None):
        rows = self.query("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def save_setting(self, key, value):
        with self.connect() as db:
            db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, json.dumps(value, ensure_ascii=False)))

    @staticmethod
    def item_scope(developer_id=None, app_id=None, kind=None):
        where, args = ["1=1"], []
        if developer_id is not None:
            where.append("i.app_id IN (SELECT app_id FROM developer_games WHERE developer_id=?)")
            args.append(developer_id)
        if app_id is not None:
            where.append("i.app_id=?")
            args.append(app_id)
        if kind is not None:
            where.append("i.kind=?")
            args.append(kind)
        return where, args

    def search_items(self, developer_id=None, app_id=None, kind=None, score=None,
                     min_score=None, max_score=None, q=None, since=None, until=None,
                     official=None, sort="newest", limit=100, offset=0):
        where, args = self.item_scope(developer_id, app_id, kind)
        if kind is None:
            where.append("i.kind IN ('review','topic')")
        for value, expression in (
            (score, "i.kind='review' AND json_extract(i.payload,'$.score')=?"),
            (min_score, "i.kind='review' AND json_extract(i.payload,'$.score')>=?"),
            (max_score, "i.kind='review' AND json_extract(i.payload,'$.score')<=?"),
            (since, "json_extract(i.payload,'$.published_at')>=?"),
            (until, "json_extract(i.payload,'$.published_at')<=?"),
            (official, "COALESCE(json_extract(i.payload,'$.official'),0)=?"),
        ):
            if value is not None:
                where.append(expression)
                args.append(value)
        if q:
            where.append("(" + " OR ".join(
                f"instr(casefold(COALESCE(json_extract(i.payload,'$.{key}'),'')),?)>0"
                for key in ("body", "title", "author")) + ")")
            args.extend([q.casefold()] * 3)
        ordering = {
            "newest": "json_extract(i.payload,'$.published_at') DESC",
            "oldest": "json_extract(i.payload,'$.published_at') ASC",
            "score_desc": "CASE WHEN i.kind='review' THEN json_extract(i.payload,'$.score') END DESC NULLS LAST",
            "score_asc": "CASE WHEN i.kind='review' THEN json_extract(i.payload,'$.score') END ASC NULLS LAST",
        }[sort]
        clause = " AND ".join(where)
        # Count and page use one snapshot so concurrent collection cannot split them.
        with self.connect() as db:
            db.execute("BEGIN")
            total = db.execute("SELECT COUNT(*) FROM items i WHERE " + clause, args).fetchone()[0]
            rows = db.execute("SELECT i.*,g.title AS game_title," + self.reply_count_sql() + " reply_count FROM items i LEFT JOIN games g ON g.id=i.app_id WHERE "
                              + clause + " ORDER BY " + ordering + ",i.app_id,i.kind,i.source_id LIMIT ? OFFSET ?",
                              (*args, limit, offset)).fetchall()
        return {"items": [self.item_row(row) for row in rows], "total": total}

    def stats(self, developer_id=None, app_id=None, days=30):
        kinds = ("review", "topic", "review_comment", "topic_comment")
        where, args = self.item_scope(developer_id, app_id)
        clause = " AND ".join(where)
        counts = {kind: 0 for kind in kinds}
        counts.update({row["kind"]: row["n"] for row in self.query(
            "SELECT i.kind,COUNT(*) n FROM items i WHERE " + clause + " GROUP BY i.kind", args)})
        score_sql = "i.kind='review' AND json_extract(i.payload,'$.score') BETWEEN 1 AND 5"
        average = self.query("SELECT AVG(json_extract(i.payload,'$.score')) average_score FROM items i WHERE "
                             + clause + " AND " + score_sql, args)[0]["average_score"]
        distribution = {str(score): 0 for score in range(1, 6)}
        distribution.update({str(row["score"]): row["n"] for row in self.query(
            "SELECT json_extract(i.payload,'$.score') score,COUNT(*) n FROM items i WHERE "
            + clause + " AND " + score_sql + " GROUP BY score", args)})
        today = date.today()
        start = today - timedelta(days=days - 1)
        daily_rows = self.query("SELECT date(json_extract(i.payload,'$.published_at'),'unixepoch','localtime') day,"
                                "i.kind,COUNT(*) n FROM items i WHERE " + clause +
                                " AND json_extract(i.payload,'$.published_at')>=CAST(strftime('%s',?,'utc') AS INTEGER)"
                                " AND json_extract(i.payload,'$.published_at')<CAST(strftime('%s',?,'utc') AS INTEGER)"
                                " GROUP BY day,i.kind", (*args, start.isoformat(), (today + timedelta(days=1)).isoformat()))
        daily = {(row["day"], row["kind"]): row["n"] for row in daily_rows}
        series = [{"date": (start + timedelta(days=n)).isoformat(),
                   **{kind: daily.get(((start + timedelta(days=n)).isoformat(), kind), 0) for kind in kinds}}
                  for n in range(days)]
        recent = self.query("SELECT COUNT(*) n FROM items i WHERE " + clause + " AND i.kind IN ('review','topic')" +
                            " AND json_extract(i.payload,'$.published_at')>=CAST(strftime('%s',?,'utc') AS INTEGER)"
                            " AND json_extract(i.payload,'$.published_at')<CAST(strftime('%s',?,'utc') AS INTEGER)",
                            (*args, (today - timedelta(days=6)).isoformat(), (today + timedelta(days=1)).isoformat()))[0]["n"]
        recent_replies = self.query("SELECT COUNT(*) n FROM items i WHERE " + clause + " AND i.kind IN ('review_comment','topic_comment')"
                            " AND json_extract(i.payload,'$.published_at')>=CAST(strftime('%s',?,'utc') AS INTEGER)"
                            " AND json_extract(i.payload,'$.published_at')<CAST(strftime('%s',?,'utc') AS INTEGER)",
                            (*args, (today - timedelta(days=6)).isoformat(), (today + timedelta(days=1)).isoformat()))[0]["n"]
        previous = self.query("SELECT COUNT(*) n FROM items i WHERE " + clause + " AND i.kind IN ('review','topic')" +
                              " AND json_extract(i.payload,'$.published_at')>=CAST(strftime('%s',?,'utc') AS INTEGER)"
                              " AND json_extract(i.payload,'$.published_at')<CAST(strftime('%s',?,'utc') AS INTEGER)",
                              (*args, (today - timedelta(days=13)).isoformat(), (today - timedelta(days=6)).isoformat()))[0]["n"]
        game_where, game_args = [], []
        if developer_id is not None:
            game_where.append("g.id IN (SELECT app_id FROM developer_games WHERE developer_id=?)")
            game_args.append(developer_id)
        if app_id is not None:
            game_where.append("g.id=?")
            game_args.append(app_id)
        games = self.query("SELECT g.id app_id,g.title,g.metadata," + ",".join(
            f"COUNT(CASE WHEN i.kind='{kind}' THEN 1 END) AS {kind}" for kind in kinds) +
            ",AVG(CASE WHEN " + score_sql + " THEN json_extract(i.payload,'$.score') END) average_score"
            " FROM games g LEFT JOIN items i ON i.app_id=g.id" +
            (" WHERE " + " AND ".join(game_where) if game_where else "") + " GROUP BY g.id ORDER BY COUNT(i.source_id) DESC,g.id", game_args)
        per_game = [{"app_id": g["app_id"], "title": g["title"], "counts": {k: g[k] for k in kinds},
                     "main_count": g["review"] + g["topic"], "reply_count": g["review_comment"] + g["topic_comment"],
                     "review_count": g["review"], "average_score": g["average_score"], **self._game_brief(g["metadata"])} for g in games]
        run_args = (developer_id,) if developer_id is not None else ()
        runs = self.query("SELECT *,json_array_length(errors) error_count FROM runs" +
                          (" WHERE developer_id=? OR developer_id IS NULL" if developer_id is not None else "") +
                          " ORDER BY id DESC LIMIT 20", run_args)
        finished = [r for r in runs if r["status"] != "running"]
        return {"totals": {"developers": len(self.developers()) if developer_id is None else int(self.developer(developer_id) is not None),
                           "games": len(games), "items": counts["review"] + counts["topic"], "replies": counts["review_comment"] + counts["topic_comment"],
                           "recent_replies_7_days": recent_replies, "recent_7_days": recent, "previous_7_days": previous, "average_score": average},
                "counts": counts, "games": per_game, "score_distribution": distribution, "daily": series,
                "runs": {"items": runs, "success_rate": sum(r["status"] == "ok" for r in finished) / len(finished) if finished else None,
                         "scope": "developer_and_global" if developer_id is not None else "global"}}

    @staticmethod
    def _game_brief(metadata):
        """Icon and TapTap rating (10-point) for dashboard display."""
        app = json.loads(metadata or "{}")
        icon = app.get("icon") or {}
        try:
            rating = float(((app.get("stat") or {}).get("rating") or {}).get("score") or 0) or None
        except (TypeError, ValueError):
            rating = None
        return {"icon": (app.get("_media") or {}).get("icon") or icon.get("medium_url") or icon.get("url") or "", "rating": rating}

    def last_synced(self, developer_id):
        return self.query("SELECT MAX(g.updated_at) t FROM games g JOIN developer_games d ON d.app_id=g.id WHERE d.developer_id=?",
                          (developer_id,))[0]["t"]

    def events(self, after=0, developer_id=None, limit=100, include_baseline=False, through=None):
        where, args = ["e.id>?"], [after]
        if developer_id is not None:
            where.append("e.app_id IN (SELECT app_id FROM developer_games WHERE developer_id=?)")
            args.append(developer_id)
        if not include_baseline:
            where.append("e.baseline=0")
        if through is not None:
            where.append("e.id<=?")
            args.append(through)
        rows = self.query("SELECT e.*,g.title AS game_title FROM events e LEFT JOIN games g ON g.id=e.app_id WHERE " + " AND ".join(where) + " ORDER BY e.id LIMIT ?", (*args, limit))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def high_water(self):
        return self.query("SELECT COALESCE(MAX(id),0) AS id FROM events")[0]["id"]

    def stream(self, key):
        rows = self.query("SELECT * FROM streams WHERE key=?", (key,))
        return rows[0] if rows else None

    def save_stream(self, key, next_url="", status="ok", error=""):
        now = time.time()
        with self.connect() as db:
            db.execute("""INSERT INTO streams VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                next_url=excluded.next_url,status=excluded.status,last_attempt=excluded.last_attempt,
                last_success=COALESCE(excluded.last_success,streams.last_success),error=excluded.error""",
                (key, next_url, status, now, now if status == "ok" else None, error))

    def start_run(self, developer_id=None, mode="incremental"):
        if mode not in ("incremental", "full"):
            raise ValueError("采集模式必须为 incremental 或 full")
        with self.connect() as db:
            return db.execute("INSERT INTO runs(developer_id,mode,started_at,status) VALUES(?,?,?,'running')", (developer_id, mode, time.time())).lastrowid

    def finish_run(self, run_id, counts, errors):
        with self.connect() as db:
            db.execute("UPDATE runs SET finished_at=?,status=?,new_items=?,updated_items=?,errors=? WHERE id=?",
                       (time.time(), "partial" if errors else "ok", counts["created"], counts["updated"], json.dumps(errors, ensure_ascii=False), run_id))

    def recover_interrupted(self):
        with self.connect() as db:
            db.execute("UPDATE runs SET status='interrupted',finished_at=? WHERE status='running'", (time.time(),))
            db.execute("UPDATE deliveries SET status='pending' WHERE status='sending'")

    def status(self):
        return {
            "counts": {name: self.query(f"SELECT COUNT(*) AS n FROM {name}")[0]["n"] for name in ("developers", "games", "items", "events")},
            "streams": self.query("SELECT * FROM streams WHERE status!='ok' ORDER BY last_attempt DESC"),
            "last_run": self.query("SELECT * FROM runs ORDER BY id DESC LIMIT 1"),
        }
