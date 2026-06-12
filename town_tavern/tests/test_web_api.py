"""观察器只读后端(FastAPI)的 API 测试:四个端点 + debug/player 服务端裁剪。"""
import pytest
from fastapi.testclient import TestClient

from town_tavern.storage.db import get_connection
from town_tavern.storage.repository import Repository
from town_tavern.web import server


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """把服务端 _repo() 指向临时库;每次请求新开连接(避免跨线程共享)。"""
    db_file = tmp_path / "web_test.db"
    seed = Repository(get_connection(db_file))
    gid = seed.create_game()
    seed.set_world_value(gid, "world_phase", "awaiting_player")
    seed.set_world_value(gid, "boss_debt", 700000)
    seed.add_timeline_message(gid, 1, type="dialogue", text="东西还在不在你手里?",
                              speaker_id="gambler", speaker_name="阿龙",
                              target_id="reporter", target_name="小林", visibility="public")
    seed.add_timeline_message(gid, 1, type="system", text="deal_recording 已 RESOLVED_INTERRUPTED",
                              visibility="private", debug_payload={"state": "RESOLVED_INTERRUPTED"})
    seed.conn.close()

    monkeypatch.setattr(server, "_repo", lambda: Repository(get_connection(db_file)))
    return TestClient(server.app), gid


def test_games_endpoint(client):
    c, gid = client
    r = c.get("/api/games")
    assert r.status_code == 200
    assert gid in r.json()["games"]


def test_timeline_player_hides_private_and_debug(client):
    c, gid = client
    r = c.get(f"/api/timeline?game_id={gid}&mode=player")
    msgs = r.json()["messages"]
    assert [m["text"] for m in msgs] == ["东西还在不在你手里?"]
    assert all("debug_payload" not in m for m in msgs)


def test_timeline_debug_shows_all(client):
    c, gid = client
    r = c.get(f"/api/timeline?game_id={gid}&mode=debug")
    msgs = r.json()["messages"]
    texts = {m["text"] for m in msgs}
    assert "deal_recording 已 RESOLVED_INTERRUPTED" in texts
    sys_row = next(m for m in msgs if m["type"] == "system")
    assert sys_row["debug_payload"]["state"] == "RESOLVED_INTERRUPTED"


def test_world_state_player_has_no_numbers(client):
    c, gid = client
    data = c.get(f"/api/world-state?game_id={gid}&mode=player").json()
    assert data["mode"] == "player"
    # player 模式绝不下发数值/状态机字段
    for k in ("boss_debt", "truth_pressure", "world_phase", "police_exposure_risk"):
        assert k not in data
    assert "atmosphere" in data


def test_world_state_debug_has_numbers(client):
    c, gid = client
    data = c.get(f"/api/world-state?game_id={gid}&mode=debug").json()
    assert data["world_phase"] == "awaiting_player"
    assert data["boss_debt"] == 700000


def test_npcs_player_vs_debug(client):
    c, gid = client
    player = c.get(f"/api/npcs?game_id={gid}&mode=player").json()["npcs"]
    assert player and all("stress" not in n for n in player)
    assert all("state_label" in n for n in player)

    debug = c.get(f"/api/npcs?game_id={gid}&mode=debug").json()["npcs"]
    assert all("stress" in n for n in debug)


def test_stream_yields_ready_then_messages(client):
    """直接驱动 SSE 异步生成器(避免用 TestClient 消费无限流而挂起)。"""
    import asyncio

    _, gid = client

    async def run():
        resp = await server.api_stream(game_id=gid, after_id=0, mode="debug")
        assert resp.media_type == "text/event-stream"
        agen = resp.body_iterator
        chunks = []
        for _ in range(6):
            chunks.append(await agen.__anext__())
            if "event: message" in "".join(chunks):
                break
        await agen.aclose()
        return "".join(chunks)

    out = asyncio.run(run())
    assert "event: ready" in out
    assert "event: message" in out
