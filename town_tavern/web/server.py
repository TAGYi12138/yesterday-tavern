"""观察器只读后端:FastAPI + SQLite 读 + SSE 推送。

四个只读端点(全部支持 ?mode=debug|player,双模式裁剪在服务端完成):
  GET /api/games                          列出所有存档
  GET /api/timeline?game_id=&after_id=    逐条消息流(增量拉取)
  GET /api/world-state?game_id=           世界面板(debug 给数值,player 给模糊描述)
  GET /api/npcs?game_id=                  NPC 状态栏
  GET /api/stream?game_id=&after_id=      SSE:有新消息即推送

player 模式绝不下发 flag/状态机/数值/私密事件/debug_payload,守住"真相归玩家"。
"""
import asyncio
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..storage.db import get_connection
from ..storage.repository import Repository

_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="昨日酒馆 · 观察器", docs_url="/api/docs")


def _repo() -> Repository:
    """每次请求新开一条只读连接(避免跨线程共享同一 sqlite 连接)。"""
    return Repository(get_connection())


def _norm_mode(mode: str) -> str:
    return "debug" if mode == "debug" else "player"


# 世界面板:player 模式只给模糊氛围描述,debug 模式给全量数值/状态机。
_PHASE_FEEL = {
    "awaiting_player": "山雨欲来 · 像是都在等谁推门进来",
    "running": "照常运转",
}


def _world_payload(repo: Repository, game_id: str, mode: str) -> dict:
    w = repo.get_world_state(game_id)
    if mode == "debug":
        return {
            "mode": "debug",
            "current_day": w.current_day,
            "world_phase": w.world_phase,
            "global_tension": w.global_tension,
            "exposure_stage": w.exposure_stage,
            "police_exposure_risk": w.police_exposure_risk,
            "debt_stage": w.debt_stage,
            "boss_debt": w.boss_debt,
            "debt_seize_countdown": w.debt_seize_countdown,
            "debt_resolution": w.debt_resolution,
            "truth_pressure": w.truth_pressure,
            "truth_stage": w.truth_stage,
            "athou_truth_progress": w.athou_truth_progress,
            "crisis_phase": w.crisis_phase,
            "crisis_days": w.crisis_days,
        }
    # player 模式:只给"体感",不给任何数值/状态机名。
    return {
        "mode": "player",
        "current_day": w.current_day,
        "atmosphere": _PHASE_FEEL.get(w.world_phase, "照常运转"),
        "debt_feel": w.debt_level(),
        "exposure_feel": w.exposure_level(),
        "truth_feel": "像是有人在藏着什么" if w.truth_pressure >= 55 else "没什么风声",
    }


def _npcs_payload(repo: Repository, game_id: str, mode: str) -> list:
    out = []
    for n in repo.get_all_npcs(game_id):
        item = {"id": n.id, "name": n.name, "job": n.job, "present": n.is_present()}
        if mode == "debug":
            item.update({
                "stress": n.stress,
                "status": n.status,
                "mental_state": n.mental_state,
                "current_goal": n.current_goal,
            })
        else:
            # player 模式:不给数值/目标,只给"在场/没来"这种可观察标签。
            item["state_label"] = "在场" if n.is_present() else "今天没来"
        out.append(item)
    return out


@app.get("/api/games")
def api_games() -> dict:
    repo = _repo()
    try:
        return {"games": repo.list_games()}
    finally:
        repo.conn.close()


@app.get("/api/timeline")
def api_timeline(
    game_id: str,
    after_id: int = 0,
    mode: str = Query("player"),
    limit: int = 300,
) -> dict:
    mode = _norm_mode(mode)
    repo = _repo()
    try:
        msgs = repo.get_timeline_messages(game_id, after_id=after_id, mode=mode, limit=limit)
        return {"mode": mode, "messages": msgs}
    finally:
        repo.conn.close()


@app.get("/api/world-state")
def api_world_state(game_id: str, mode: str = Query("player")) -> dict:
    mode = _norm_mode(mode)
    repo = _repo()
    try:
        return _world_payload(repo, game_id, mode)
    finally:
        repo.conn.close()


@app.get("/api/npcs")
def api_npcs(game_id: str, mode: str = Query("player")) -> dict:
    mode = _norm_mode(mode)
    repo = _repo()
    try:
        return {"mode": mode, "npcs": _npcs_payload(repo, game_id, mode)}
    finally:
        repo.conn.close()


@app.get("/api/stream")
async def api_stream(
    game_id: str,
    after_id: int = 0,
    mode: str = Query("player"),
) -> StreamingResponse:
    """SSE:每秒轮询库里是否有 id 更大的新消息,有则逐条 push。

    世界是单向事件流,SSE 足够(无需 WebSocket)。连接断开由客户端 EventSource 自动重连。
    """
    mode = _norm_mode(mode)

    async def _gen():
        repo = _repo()
        last_id = after_id
        try:
            # 连接建立先发一个 ready,便于前端确认通道打开。
            yield "event: ready\ndata: {}\n\n"
            while True:
                msgs = repo.get_timeline_messages(game_id, after_id=last_id, mode=mode, limit=100)
                for m in msgs:
                    last_id = max(last_id, m["id"])
                    yield "event: message\ndata: " + json.dumps(m, ensure_ascii=False) + "\n\n"
                # 心跳注释行,保活并探测断连。
                yield ": keep-alive\n\n"
                await asyncio.sleep(1.0)
        finally:
            repo.conn.close()

    return StreamingResponse(_gen(), media_type="text/event-stream")


# 静态前端(三栏只读观察器)挂在根路径;放在最后注册以免覆盖 /api/*。
if _STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")


def main() -> None:
    """本地起服务:python -m town_tavern.web.server。

    监听地址在 config.py 的 OBSERVER_HOST / OBSERVER_PORT 配置(默认 0.0.0.0:8000),
    也可用同名环境变量覆盖。
    """
    import uvicorn

    from .. import config

    uvicorn.run(app, host=config.OBSERVER_HOST, port=config.OBSERVER_PORT)


if __name__ == "__main__":
    main()
