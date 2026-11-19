from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import connect
from app.modules import timeout_change_no_rewrite as tcnr
from app.services import ingest as ingest_svc
from app.services import sessions as sess_svc

router = APIRouter(prefix="/api")


class IngestBody(BaseModel):
    fixture: str | None = None


class TimeoutBody(BaseModel):
    idle_timeout_sec: int


@router.get("/overview")
def overview():
    conn = connect()
    try:
        return {
            "visitors": conn.execute("SELECT COUNT(*) AS c FROM visitors").fetchone()["c"],
            "sessions": conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"],
            "hits_primary": conn.execute(
                "SELECT COUNT(*) AS c FROM hits WHERE channel='primary'"
            ).fetchone()["c"],
            "quarantine": conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"],
            **tcnr.current_config(conn),
        }
    finally:
        conn.close()


@router.get("/visitors")
def list_visitors():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, label, created_at FROM visitors ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/sessions")
def list_sessions(visitor_id: str | None = None):
    conn = connect()
    try:
        return sess_svc.list_sessions(conn, visitor_id)
    finally:
        conn.close()


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    conn = connect()
    try:
        detail = sess_svc.session_detail(conn, session_id)
        if not detail:
            raise HTTPException(404, "会话不存在")
        return detail
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, visitor_id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest(body: IngestBody | None = None):
    fixture = body.fixture if body and body.fixture else "ingest_batch.json"
    if "/" in fixture or "\\" in fixture or fixture.startswith("."):
        raise HTTPException(400, "夹具名不合法")
    result = ingest_svc.run_batch(fixture)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "收流失败"))
    return result


@router.get("/session-proofs")
def session_proofs(session_id: str | None = None):
    conn = connect()
    try:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM session_proofs WHERE session_id=? ORDER BY id DESC",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM session_proofs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        return {
            **tcnr.current_config(conn),
            "min_idle_timeout_sec": tcnr.MIN_TIMEOUT_SEC,
        }
    finally:
        conn.close()


@router.put("/settings/idle-timeout")
def put_idle_timeout(body: TimeoutBody):
    """单事务：写新配置版本 + 更新设置；失败整笔回滚。

    「重切历史」入口固定拒绝，见 POST /resplit-history。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = tcnr.change_timeout(conn, body.idle_timeout_sec, source="settings")
            conn.commit()
        except tcnr.ConfigError as e:
            conn.rollback()
            raise HTTPException(400, str(e))
        except Exception:
            conn.rollback()
            raise
        return result
    finally:
        conn.close()


@router.post("/resplit-history")
def resplit_history():
    """「重切历史」入口：固定拒绝，不在 closed 上搬成员或改切分边。"""
    try:
        tcnr.reject_resplit_history()
    except tcnr.RewriteRejected as e:
        raise HTTPException(409, str(e))


@router.get("/config-audit")
def config_audit():
    """配置变更核证：只读，不产生任何写入。"""
    conn = connect()
    try:
        return tcnr.audit(conn)
    finally:
        conn.close()
