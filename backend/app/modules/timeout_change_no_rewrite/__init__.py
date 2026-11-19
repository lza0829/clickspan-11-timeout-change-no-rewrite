"""改切窗秒数不回溯：配置只影响之后收流裁决。

落库形态（二选一）：采用「配置版本表」``config_versions``，
不采用「创建会话绑定 timeout_sec_snapshot」。

失败闭（二选一）：配置变更在单事务内写新版本，并固定拒绝「重切历史」入口；
任何写入失败整笔回滚，不留下半条版本或半条切分边。

不变量：
* ``closed`` 会话的成员（hits）与最新会话证明不可再改；
* ``session_cuts`` 切分边快照 append-only；
* 修改 ``idle_timeout_sec`` 只改变之后收流的裁决，open 会话在下一次收流时
  按新超时与末击点间隙决定继续或切开，不重放、不搬移历史击点。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

STATUS = "implemented"

MIN_TIMEOUT_SEC = 60


class RewriteRejected(Exception):
    """历史重切入口固定拒绝。"""


class ConfigError(ValueError):
    """配置变更输入不合法。"""


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def secs_between(earlier: str, later: str) -> int:
    return int((parse_ts(later) - parse_ts(earlier)).total_seconds())


def _setting(conn) -> int:
    row = conn.execute(
        "SELECT value FROM settings WHERE key='idle_timeout_sec'"
    ).fetchone()
    return int(row["value"]) if row else 1800


def latest_version(conn):
    return conn.execute(
        "SELECT * FROM config_versions ORDER BY version DESC LIMIT 1"
    ).fetchone()


def ensure_baseline(conn) -> None:
    """老库补齐基线版本；新库把初始设置记成 v1。"""
    if latest_version(conn) is not None:
        return
    conn.execute(
        """
        INSERT INTO config_versions(idle_timeout_sec, previous_sec, source, created_at)
        VALUES (?, NULL, 'baseline', ?)
        """,
        (_setting(conn), now_utc()),
    )


def current_config(conn) -> dict:
    row = latest_version(conn)
    return {
        "idle_timeout_sec": _setting(conn),
        "config_version": row["version"] if row else None,
        "updated_at": row["created_at"] if row else None,
    }


def change_timeout(conn, sec: int, source: str = "settings") -> dict:
    """在调用方已开启的事务内写新版本并更新设置。

    调用方负责 commit / rollback。相同值不产生新版本（不是一次变更）。
    """
    if isinstance(sec, bool) or not isinstance(sec, int):
        raise ConfigError("idle_timeout_sec 必须为整数")
    if sec < MIN_TIMEOUT_SEC:
        raise ConfigError(f"空闲超时至少 {MIN_TIMEOUT_SEC} 秒")
    previous = _setting(conn)
    if sec == previous:
        current = current_config(conn)
        return {**current, "previous_sec": previous, "changed": False}

    created_at = now_utc()
    cur = conn.execute(
        """
        INSERT INTO config_versions(idle_timeout_sec, previous_sec, source, created_at)
        VALUES (?,?,?,?)
        """,
        (sec, previous, source, created_at),
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES ('idle_timeout_sec', ?)",
        (str(sec),),
    )
    return {
        "idle_timeout_sec": sec,
        "previous_sec": previous,
        "config_version": cur.lastrowid,
        "updated_at": created_at,
        "changed": True,
    }


def record_cut(
    conn,
    *,
    visitor_id: str,
    prev_session_id: str,
    next_session_id: str,
    prev_last_ts: str,
    next_first_ts: str,
    gap_sec: int,
    timeout_sec_used: int,
    config_version: int | None,
    reason: str = "idle_timeout",
) -> int:
    """append-only 切分边快照；外层事务失败则整条消失。"""
    cur = conn.execute(
        """
        INSERT INTO session_cuts(
          visitor_id, prev_session_id, next_session_id,
          prev_last_ts, next_first_ts, gap_sec,
          timeout_sec_used, config_version, reason, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            visitor_id,
            prev_session_id,
            next_session_id,
            prev_last_ts,
            next_first_ts,
            gap_sec,
            timeout_sec_used,
            config_version,
            reason,
            now_utc(),
        ),
    )
    return int(cur.lastrowid)


def reject_resplit_history(*_args, **_kwargs):
    """历史重切入口：固定拒绝，绝不改写 closed 成员或切分边。"""
    raise RewriteRejected("禁止重切历史：closed 会话成员与既有切分边不可改写")


def list_cuts(conn, visitor_id: str | None = None, limit: int = 200) -> list[dict]:
    limit = max(1, min(int(limit), 1000))
    if visitor_id:
        rows = conn.execute(
            "SELECT * FROM session_cuts WHERE visitor_id=? ORDER BY id DESC LIMIT ?",
            (visitor_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM session_cuts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def _closed_mismatches(conn) -> list[dict]:
    violations: list[dict] = []
    closed = conn.execute(
        """
        SELECT id, visitor_id, started_at, ended_at, hit_count
        FROM sessions WHERE status='closed' ORDER BY started_at, id
        """
    ).fetchall()
    for sess in closed:
        proof = conn.execute(
            "SELECT * FROM session_proofs WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (sess["id"],),
        ).fetchone()
        actual = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts, id",
                (sess["id"],),
            ).fetchall()
        ]
        if proof is None:
            violations.append(
                {"session_id": sess["id"], "problem": "closed 会话缺少会话证明"}
            )
            continue
        try:
            claimed = json.loads(proof["hit_ids_json"])
        except (TypeError, ValueError):
            claimed = None
        if claimed != actual:
            violations.append(
                {
                    "session_id": sess["id"],
                    "problem": "closed 击点集与证明不一致",
                    "proof_hit_ids": claimed,
                    "actual_hit_ids": actual,
                }
            )
        if int(proof["hit_count"]) != len(actual):
            violations.append(
                {
                    "session_id": sess["id"],
                    "problem": "closed 证明计数与击点集不一致",
                    "proof_hit_count": proof["hit_count"],
                    "actual": len(actual),
                }
            )
    return violations


def _cut_mismatches(conn) -> list[dict]:
    violations: list[dict] = []
    versions = {
        r["version"]: r["idle_timeout_sec"]
        for r in conn.execute("SELECT version, idle_timeout_sec FROM config_versions")
    }
    cuts = conn.execute("SELECT * FROM session_cuts ORDER BY id").fetchall()
    for cut in cuts:
        prev = conn.execute(
            "SELECT id, status, ended_at FROM sessions WHERE id=?",
            (cut["prev_session_id"],),
        ).fetchone()
        nxt = conn.execute(
            "SELECT id, started_at FROM sessions WHERE id=?",
            (cut["next_session_id"],),
        ).fetchone()
        if prev is None or nxt is None:
            violations.append(
                {"cut_id": cut["id"], "problem": "切分边指向不存在的会话"}
            )
            continue
        if prev["status"] != "closed":
            violations.append(
                {
                    "cut_id": cut["id"],
                    "problem": "切分边前序会话未 closed",
                    "session_id": prev["id"],
                }
            )
        # 只在 idle_timeout 切分边上比对前序末击点；显式关闭的前序可能已延长。
        if cut["reason"] == "idle_timeout" and prev["ended_at"] != cut["prev_last_ts"]:
            violations.append(
                {
                    "cut_id": cut["id"],
                    "problem": "切分边前序末击点快照被改动",
                    "expected": cut["prev_last_ts"],
                    "actual": prev["ended_at"],
                }
            )
        if nxt["started_at"] != cut["next_first_ts"]:
            violations.append(
                {
                    "cut_id": cut["id"],
                    "problem": "切分边后继首击点快照被改动",
                    "expected": cut["next_first_ts"],
                    "actual": nxt["started_at"],
                }
            )
        actual_gap = secs_between(cut["prev_last_ts"], cut["next_first_ts"])
        if actual_gap != cut["gap_sec"]:
            violations.append(
                {
                    "cut_id": cut["id"],
                    "problem": "切分边间隙快照被改动",
                    "expected": cut["gap_sec"],
                    "actual": actual_gap,
                }
            )
        if cut["gap_sec"] <= cut["timeout_sec_used"] and cut["reason"] == "idle_timeout":
            violations.append(
                {"cut_id": cut["id"], "problem": "切分边未超过当时超时"}
            )
        if cut["config_version"] is not None:
            if versions.get(cut["config_version"]) != cut["timeout_sec_used"]:
                violations.append(
                    {
                        "cut_id": cut["id"],
                        "problem": "切分边超时与所引用配置版本不一致",
                    }
                )
    return violations


def audit(conn) -> dict:
    """配置变更 + 历史只读核证（纯 SELECT，不产生任何写入）。"""
    current = current_config(conn)
    versions = [
        dict(r)
        for r in conn.execute(
            "SELECT version, idle_timeout_sec, previous_sec, source, created_at "
            "FROM config_versions ORDER BY version"
        ).fetchall()
    ]
    closed_counts = conn.execute(
        "SELECT "
        "SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed, "
        "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open "
        "FROM sessions"
    ).fetchone()
    closed_mismatches = _closed_mismatches(conn)
    cut_mismatches = _cut_mismatches(conn)
    cuts = list_cuts(conn, limit=200)
    violations = closed_mismatches + cut_mismatches
    return {
        "read_only": True,
        "current": current,
        "config_versions": versions,
        "closed_sessions": int(closed_counts["closed"] or 0),
        "open_sessions": int(closed_counts["open"] or 0),
        "cut_edges": cuts,
        "integrity": {
            "ok": not violations,
            "closed_members_match_proofs": not closed_mismatches,
            "cut_edges_match_sessions": not cut_mismatches,
            "violations": violations,
        },
    }
