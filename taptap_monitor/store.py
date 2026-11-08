from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

from .content import Item


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
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
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

    def save_game(self, developer_id: int, app: dict, group_id=None):
        # Keep only public game metadata, without analytics/tracking payloads.
        metadata = {k: app[k] for k in ("id", "title", "icon", "cover", "ad_banner", "description", "stat", "is_level", "tags", "_media") if k in app}
        with self.connect() as db:
            db.execute("""INSERT INTO games(id,title,group_id,metadata,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                group_id=COALESCE(excluded.group_id,games.group_id), metadata=excluded.metadata, updated_at=excluded.updated_at""",
                (app["id"], app["title"], group_id, json.dumps(metadata, ensure_ascii=False), time.time()))
            db.execute("INSERT OR IGNORE INTO developer_games VALUES(?,?)", (developer_id, app["id"]))

    def save_profile(self, developer_id, profile):
        source = {k: profile[k] for k in ("name", "intro", "avatar", "website") if k in profile}
        with self.connect() as db:
            db.execute("UPDATE developers SET name=? WHERE id=?", (profile["name"], developer_id))
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
        where, args = ["1=1"], []
        if developer_id is not None:
            where.append("app_id IN (SELECT app_id FROM developer_games WHERE developer_id=?)")
            args.append(developer_id)
        if app_id is not None:
            where.append("app_id=?")
            args.append(app_id)
        if kind is not None:
            where.append("kind=?")
            args.append(kind)
        rows = self.query("SELECT payload,first_seen,last_seen FROM items WHERE " + " AND ".join(where) + " ORDER BY json_extract(payload,'$.published_at') DESC,source_id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
        return [{**json.loads(row["payload"]), "first_seen": row["first_seen"], "last_seen": row["last_seen"]} for row in rows]

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

    def start_run(self, developer_id=None):
        with self.connect() as db:
            return db.execute("INSERT INTO runs(developer_id,started_at,status) VALUES(?,?,'running')", (developer_id, time.time())).lastrowid

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
