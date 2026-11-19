import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.db import connect, fixtures_dir
from app.modules import timeout_change_no_rewrite as tcnr


def run_batch(fixture_name: str = "ingest_batch.json") -> dict:
    """按空闲超时切会话；改切窗只影响本批之后的新裁决。

    * 只对 ``open`` 会话做「继续 / 切开」裁决，按当前配置版本的新超时；
    * 切开时把前序会话落成 ``closed`` 并封印成员，再 append-only 记一条切分边；
    * 历史 ``closed`` 会话的成员与既有切分边一律不改。
    """
    path = Path(fixture_name)
    if not path.is_absolute():
        path = fixtures_dir() / fixture_name
    if not path.is_file():
        return {"ok": False, "error": f"缺少 {path.name}"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    # 同一批按时刻排序，保证间隙裁决确定；访客间顺序无关。
    hits = sorted(
        payload.get("hits", []),
        key=lambda h: (tcnr.parse_ts(h["event_ts"]), h["id"]),
    )

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        config = tcnr.current_config(conn)
        timeout_sec = config["idle_timeout_sec"]
        config_version = config["config_version"]
        written = 0
        for hit in hits:
            vid = hit["visitor_id"]
            # 幂等回放 / 唯一键重复：先判定，避免为未落库的击点开出空会话或切分边。
            dup = conn.execute(
                """
                SELECT 1 FROM hits
                WHERE id=?
                   OR (visitor_id=? AND event_ts=? AND page_path=? AND event_name=?)
                """,
                (
                    hit["id"],
                    vid,
                    hit["event_ts"],
                    hit["page_path"],
                    hit["event_name"],
                ),
            ).fetchone()
            if dup:
                continue
            exists = conn.execute("SELECT 1 FROM visitors WHERE id=?", (vid,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO visitors(id, label, created_at) VALUES (?,?,?)",
                    (vid, hit.get("visitor_label") or vid, _now()),
                )
            sid = _resolve_session(
                conn,
                visitor_id=vid,
                event_ts=hit["event_ts"],
                timeout_sec=timeout_sec,
                config_version=config_version,
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)
                VALUES (?,?,?,?,?,?, 'primary')
                """,
                (
                    hit["id"],
                    vid,
                    sid,
                    hit["event_ts"],
                    hit["page_path"],
                    hit["event_name"],
                ),
            )
            if not conn.execute("SELECT changes()").fetchone()[0]:
                # 撞唯一约束（同访客同刻同路径同名）：未落库，不计数。
                continue
            written += 1
            _append_hit_to_session(conn, sid, hit["event_ts"])
            _write_proof(conn, sid)
        report = {
            "batch_id": batch_id,
            "written": written,
            "input": len(hits),
            "idle_timeout_sec": timeout_sec,
            "config_version": config_version,
        }
        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
            (batch_id, _now(), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _resolve_session(
    conn,
    *,
    visitor_id: str,
    event_ts: str,
    timeout_sec: int,
    config_version: int | None,
) -> str:
    latest = conn.execute(
        """
        SELECT * FROM sessions WHERE visitor_id=?
        ORDER BY started_at DESC, rowid DESC LIMIT 1
        """,
        (visitor_id,),
    ).fetchone()

    if latest is not None and latest["status"] == "open":
        gap = tcnr.secs_between(latest["ended_at"], event_ts)
        if gap <= timeout_sec:
            return latest["id"]
        sid = _new_session(conn, visitor_id, event_ts)
        _close_session(conn, latest["id"])
        tcnr.record_cut(
            conn,
            visitor_id=visitor_id,
            prev_session_id=latest["id"],
            next_session_id=sid,
            prev_last_ts=latest["ended_at"],
            next_first_ts=event_ts,
            gap_sec=gap,
            timeout_sec_used=timeout_sec,
            config_version=config_version,
            reason="idle_timeout",
        )
        return sid

    # 上一个会话已显式 closed：新建 open；只追加边界，不改前序。
    sid = _new_session(conn, visitor_id, event_ts)
    if latest is not None and latest["ended_at"]:
        gap = tcnr.secs_between(latest["ended_at"], event_ts)
        tcnr.record_cut(
            conn,
            visitor_id=visitor_id,
            prev_session_id=latest["id"],
            next_session_id=sid,
            prev_last_ts=latest["ended_at"],
            next_first_ts=event_ts,
            gap_sec=gap,
            timeout_sec_used=timeout_sec,
            config_version=config_version,
            reason="explicit_close",
        )
    return sid


def _new_session(conn, visitor_id: str, event_ts: str) -> str:
    sid = f"S-{visitor_id}-{uuid.uuid4().hex[:6]}"
    conn.execute(
        """
        INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)
        VALUES (?,?,?,?,0,'open')
        """,
        (sid, visitor_id, event_ts, event_ts),
    )
    return sid


def _append_hit_to_session(conn, sid: str, event_ts: str) -> None:
    cur = conn.execute(
        """
        UPDATE sessions
        SET hit_count = hit_count + 1,
            ended_at = CASE
              WHEN ended_at IS NULL OR ended_at < ? THEN ?
              ELSE ended_at END
        WHERE id=? AND status='open'
        """,
        (event_ts, event_ts, sid),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"拒绝向已关闭会话追加击点：{sid}")


def _close_session(conn, sid: str) -> None:
    conn.execute("UPDATE sessions SET status='closed' WHERE id=?", (sid,))
    _write_proof(conn, sid)


def _write_proof(conn, sid: str) -> None:
    sess_row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts, id", (sid,)
        ).fetchall()
    ]
    conn.execute(
        """
        INSERT INTO session_proofs(
          session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            sid,
            sess_row["visitor_id"],
            json.dumps(ids, ensure_ascii=False),
            len(ids),
            sess_row["started_at"],
            sess_row["ended_at"],
            _now(),
        ),
    )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
