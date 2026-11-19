import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "seed_hits.json").write_text(
        json.dumps(
            {
                "visitors": [
                    {"id": "V-1", "label": "甲", "created_at": "2026-11-17T08:00:00Z"}
                ],
                "hits": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("FIXTURES_DIR", str(fixtures))

    from app.main import app

    with TestClient(app) as c:
        c.fixtures_dir = fixtures
        yield c


def batch(client, name, hits):
    (client.fixtures_dir / name).write_text(
        json.dumps({"batch_id": name, "hits": hits}), encoding="utf-8"
    )
    r = client.post("/api/ingest/run", json={"fixture": name})
    assert r.status_code == 200, r.text
    return r.json()


def hit(hid, ts):
    return {
        "id": hid,
        "visitor_id": "V-1",
        "event_ts": ts,
        "page_path": f"/{hid.lower()}",
        "event_name": "view",
    }


def set_timeout(client, sec):
    r = client.put("/api/settings/idle-timeout", json={"idle_timeout_sec": sec})
    assert r.status_code == 200, r.text
    return r.json()


def sessions(client):
    return client.get("/api/sessions").json()


def proof(client, sid):
    return {
        r["session_id"]: r
        for r in client.get("/api/session-proofs").json()
    }.get(sid)


def test_closed_members_and_cuts_survive_timeout_change(client):
    set_timeout(client, 60)
    batch(client, "b1.json", [hit("H1", "2026-11-17T09:00:00Z")])
    batch(client, "b2.json", [hit("H2", "2026-11-17T09:10:00Z")])

    closed = [s for s in sessions(client) if s["status"] == "closed"]
    assert len(closed) == 1
    before_proof = proof(client, closed[0]["id"])
    before_hits = [h["id"] for h in client.get(
        f"/api/sessions/{closed[0]['id']}"
    ).json()["hits"]]
    before_cuts = client.get("/api/config-audit").json()["cut_edges"]

    set_timeout(client, 5000)

    assert proof(client, closed[0]["id"])["hit_ids_json"] == before_proof["hit_ids_json"]
    after_hits = [h["id"] for h in client.get(
        f"/api/sessions/{closed[0]['id']}"
    ).json()["hits"]]
    assert after_hits == before_hits
    assert client.get("/api/config-audit").json()["cut_edges"] == before_cuts

    audit = client.get("/api/config-audit").json()
    assert audit["integrity"]["ok"] is True, audit["integrity"]


def test_open_rejudged_by_new_value_without_replay(client):
    set_timeout(client, 3600)
    batch(client, "b1.json", [hit("H1", "2026-11-17T09:00:00Z")])
    batch(client, "b2.json", [hit("H2", "2026-11-17T09:30:00Z")])

    open_ids = [s["id"] for s in sessions(client) if s["status"] == "open"]
    assert len(open_ids) == 1
    sid = open_ids[0]
    n_before = len(client.get(f"/api/sessions/{sid}").json()["hits"])

    # 改小超时不重放、不搬点。
    set_timeout(client, 60)
    assert len(client.get(f"/api/sessions/{sid}").json()["hits"]) == n_before

    # 下一批间隙 60s，未超过新超时 → 继续同一 open 会话。
    batch(client, "b3.json", [hit("H3", "2026-11-17T09:31:00Z")])
    assert client.get(f"/api/sessions/{sid}").json()["session"]["status"] == "open"
    assert any(
        h["id"] == "H3" for h in client.get(f"/api/sessions/{sid}").json()["hits"]
    )

    # 再下一批间隙 540s > 60 → 按新值切开，前序闭合并建切分边。
    batch(client, "b4.json", [hit("H4", "2026-11-17T09:40:00Z")])
    assert client.get(f"/api/sessions/{sid}").json()["session"]["status"] == "closed"
    cuts = client.get("/api/config-audit").json()["cut_edges"]
    edge = next(c for c in cuts if c["prev_session_id"] == sid)
    assert edge["gap_sec"] == 540
    assert edge["timeout_sec_used"] == 60
    assert edge["config_version"] is not None
    assert client.get("/api/config-audit").json()["integrity"]["ok"] is True


def test_resplit_history_rejected_and_bad_config_rolls_back(client):
    assert client.post("/api/resplit-history").status_code == 409

    before = client.get("/api/config-audit").json()["config_versions"]
    r = client.put("/api/settings/idle-timeout", json={"idle_timeout_sec": 10})
    assert r.status_code == 400
    after = client.get("/api/config-audit").json()["config_versions"]
    assert after == before


def test_within_batch_large_gap_splits_without_touching_earlier(client):
    set_timeout(client, 60)
    batch(
        client,
        "b1.json",
        [hit("H1", "2026-11-17T09:00:00Z"), hit("H2", "2026-11-17T10:00:00Z")],
    )
    rows = sessions(client)
    assert len(rows) == 2
    first = next(r for r in rows if r["started_at"] == "2026-11-17T09:00:00Z")
    assert first["status"] == "closed"
    assert [h["id"] for h in client.get(
        f"/api/sessions/{first['id']}"
    ).json()["hits"]] == ["H1"]
    cut = next(
        c
        for c in client.get("/api/config-audit").json()["cut_edges"]
        if c["prev_session_id"] == first["id"]
    )
    assert cut["gap_sec"] == 3600 and cut["reason"] == "idle_timeout"
    assert client.get("/api/config-audit").json()["integrity"]["ok"] is True


def test_db_level_triggers_block_snapshot_rewrite(client):
    import sqlite3

    set_timeout(client, 60)
    batch(client, "b1.json", [hit("H1", "2026-11-17T09:00:00Z")])
    batch(client, "b2.json", [hit("H2", "2026-11-17T09:10:00Z")])

    from app.db import connect

    conn = connect()
    try:
        for stmt in (
            "UPDATE session_cuts SET timeout_sec_used=9999",
            "DELETE FROM session_cuts",
            "UPDATE config_versions SET idle_timeout_sec=9999",
            "DELETE FROM config_versions",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(stmt)
        conn.rollback()
    finally:
        conn.close()
    assert client.get("/api/config-audit").json()["integrity"]["ok"] is True


def test_closed_members_cannot_be_reassigned(client):
    import sqlite3

    set_timeout(client, 60)
    batch(client, "b1.json", [hit("H1", "2026-11-17T09:00:00Z")])
    batch(client, "b2.json", [hit("H2", "2026-11-17T09:10:00Z")])
    closed = next(s for s in sessions(client) if s["status"] == "closed")
    open_s = next(s for s in sessions(client) if s["status"] == "open")

    from app.db import connect

    conn = connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE hits SET session_id=? WHERE id='H1'", (open_s["id"],)
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE hits SET session_id=? WHERE id='H2'", (closed["id"],)
            )
        conn.rollback()
    finally:
        conn.close()
    assert client.get(f"/api/sessions/{closed['id']}").json()["hits"][0]["id"] == "H1"


def test_out_of_order_batch_is_ordered_and_duplicates_ignored(client):
    set_timeout(client, 60)
    # 乱序输入：按时刻排序后 10:00 与 09:00 间隙 3600 > 60 → 切开。
    batch(
        client,
        "b1.json",
        [hit("H2", "2026-11-17T10:00:00Z"), hit("H1", "2026-11-17T09:00:00Z")],
    )
    rows = sorted(sessions(client), key=lambda s: s["started_at"])
    assert [s["started_at"] for s in rows] == [
        "2026-11-17T09:00:00Z",
        "2026-11-17T10:00:00Z",
    ]
    assert rows[0]["status"] == "closed"
    assert [h["id"] for h in client.get(f"/api/sessions/{rows[0]['id']}").json()["hits"]] == ["H1"]

    # 唯一键重复（不同 id，同访客同刻同路径同名）不计数、不建空会话。
    before = len(sessions(client))
    result = batch(
        client,
        "b2.json",
        [
            {
                "id": "H1-dup",
                "visitor_id": "V-1",
                "event_ts": "2026-11-17T09:00:00Z",
                "page_path": "/h1",
                "event_name": "view",
            }
        ],
    )
    assert result["report"]["written"] == 0
    assert len(sessions(client)) == before
    assert client.get("/api/config-audit").json()["integrity"]["ok"] is True


def test_config_versions_append_only_and_same_value_noop(client):
    v1 = client.get("/api/settings").json()["config_version"]
    r = set_timeout(client, 300)
    assert r["changed"] is True and r["previous_sec"] == 1800
    versions = client.get("/api/config-audit").json()["config_versions"]
    assert [v["version"] for v in versions] == [v1, v1 + 1]

    again = set_timeout(client, 300)
    assert again["changed"] is False
    assert len(client.get("/api/config-audit").json()["config_versions"]) == 2
