import json

from app.db import connect, fixtures_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visitors (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  hit_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS hits (
  id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  session_id TEXT REFERENCES sessions(id),
  event_ts TEXT NOT NULL,
  page_path TEXT NOT NULL,
  event_name TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT 'primary',
  UNIQUE(visitor_id, event_ts, page_path, event_name)
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visitor_id TEXT,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS session_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  visitor_id TEXT NOT NULL,
  hit_ids_json TEXT NOT NULL,
  hit_count INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  created_at TEXT NOT NULL
);

-- 配置版本表（改切窗不回溯的落库形态）：只追加，不改历史行。
CREATE TABLE IF NOT EXISTS config_versions (
  version INTEGER PRIMARY KEY AUTOINCREMENT,
  idle_timeout_sec INTEGER NOT NULL,
  previous_sec INTEGER,
  source TEXT NOT NULL DEFAULT 'settings',
  created_at TEXT NOT NULL
);

-- 切分边快照（append-only）：记录每次切开时所用的超时与配置版本。
CREATE TABLE IF NOT EXISTS session_cuts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visitor_id TEXT NOT NULL,
  prev_session_id TEXT NOT NULL,
  next_session_id TEXT NOT NULL,
  prev_last_ts TEXT NOT NULL,
  next_first_ts TEXT NOT NULL,
  gap_sec INTEGER NOT NULL,
  timeout_sec_used INTEGER NOT NULL,
  config_version INTEGER,
  reason TEXT NOT NULL DEFAULT 'idle_timeout',
  created_at TEXT NOT NULL
);

-- 失败闭的库级兜底：切分边与配置版本只可插入，不可改写/删除。
CREATE TRIGGER IF NOT EXISTS trg_cuts_no_update
BEFORE UPDATE ON session_cuts
BEGIN SELECT RAISE(ABORT, '禁止改写切分边快照'); END;

CREATE TRIGGER IF NOT EXISTS trg_cuts_no_delete
BEFORE DELETE ON session_cuts
BEGIN SELECT RAISE(ABORT, '禁止删除切分边快照'); END;

CREATE TRIGGER IF NOT EXISTS trg_config_versions_no_update
BEFORE UPDATE ON config_versions
BEGIN SELECT RAISE(ABORT, '禁止改写配置版本'); END;

CREATE TRIGGER IF NOT EXISTS trg_config_versions_no_delete
BEFORE DELETE ON config_versions
BEGIN SELECT RAISE(ABORT, '禁止删除配置版本'); END;

-- closed 会话成员封闭：不得再挂入击点，也不得把成员迁出/迁入 closed。
CREATE TRIGGER IF NOT EXISTS trg_hits_no_insert_into_closed
BEFORE INSERT ON hits
WHEN NEW.session_id IS NOT NULL
 AND (SELECT status FROM sessions WHERE id = NEW.session_id) = 'closed'
BEGIN SELECT RAISE(ABORT, '禁止向 closed 会话挂入击点'); END;

CREATE TRIGGER IF NOT EXISTS trg_hits_no_move_out_of_closed
BEFORE UPDATE OF session_id ON hits
WHEN OLD.session_id IS NOT NULL
 AND OLD.session_id IS NOT NEW.session_id
 AND (SELECT status FROM sessions WHERE id = OLD.session_id) = 'closed'
BEGIN SELECT RAISE(ABORT, '禁止从 closed 会话迁出击点'); END;

CREATE TRIGGER IF NOT EXISTS trg_hits_no_move_into_closed
BEFORE UPDATE OF session_id ON hits
WHEN NEW.session_id IS NOT NULL
 AND OLD.session_id IS NOT NEW.session_id
 AND (SELECT status FROM sessions WHERE id = NEW.session_id) = 'closed'
BEGIN SELECT RAISE(ABORT, '禁止向 closed 会话迁入击点'); END;
"""

TIDY_COLUMNS = {
    # 落库形态选「配置版本表」：会话不绑定 timeout_sec_snapshot。
    "session_cuts": {
        "config_version": "INTEGER",
        "reason": "TEXT NOT NULL DEFAULT 'idle_timeout'",
    },
}


def _migrate(conn):
    """老库补列；配置版本表基线由功能模块补齐。"""
    from app.modules import timeout_change_no_rewrite as tcnr

    for table, cols in TIDY_COLUMNS.items():
        have = {
            r["name"]
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, decl in cols.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    tcnr.ensure_baseline(conn)


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM settings WHERE key='idle_timeout_sec'").fetchone()
    if not row:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('idle_timeout_sec', ?)",
            ("1800",),
        )
    _migrate(conn)
    n = conn.execute("SELECT COUNT(*) AS c FROM visitors").fetchone()["c"]
    if n == 0:
        _seed_fixtures(conn)
    conn.commit()
    conn.close()


def _seed_fixtures(conn):
    fixture = fixtures_dir() / "seed_hits.json"
    if not fixture.exists():
        return
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for v in data.get("visitors", []):
        conn.execute(
            "INSERT INTO visitors(id, label, created_at) VALUES (?,?,?)",
            (v["id"], v["label"], v["created_at"]),
        )
    # 基座：按访客整段落进一个 open 会话（超时切分留给功能题空桩）
    for visitor_id, group in _group_hits(data.get("hits", [])).items():
        group = sorted(group, key=lambda h: (h["event_ts"], h["id"]))
        sid = f"S-{visitor_id}-1"
        conn.execute(
            "INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status) VALUES (?,?,?,?,?,?)",
            (
                sid,
                visitor_id,
                group[0]["event_ts"],
                group[-1]["event_ts"],
                len(group),
                "open",
            ),
        )
        hit_ids = []
        for h in group:
            conn.execute(
                """
                INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    h["id"],
                    visitor_id,
                    sid,
                    h["event_ts"],
                    h["page_path"],
                    h["event_name"],
                    h.get("channel", "primary"),
                ),
            )
            hit_ids.append(h["id"])
        conn.execute(
            """
            INSERT INTO session_proofs(session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at)
            VALUES (?,?,?,?,?,?,datetime('now'))
            """,
            (
                sid,
                visitor_id,
                json.dumps(hit_ids, ensure_ascii=False),
                len(hit_ids),
                group[0]["event_ts"],
                group[-1]["event_ts"],
            ),
        )


def _group_hits(hits):
    out = {}
    for h in hits:
        out.setdefault(h["visitor_id"], []).append(h)
    return out


def idle_timeout_sec(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key='idle_timeout_sec'").fetchone()
    return int(row["value"]) if row else 1800
