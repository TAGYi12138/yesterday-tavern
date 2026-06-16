"""多人讨论 PR-E:日终触发 maybe_trigger_group_discussion + API/时间线分组字段透传。"""
from fastapi.testclient import TestClient

from town_tavern.engine import group_engine
from town_tavern.models.conflict import Conflict, ConflictState
from town_tavern.tests.test_group_loop import FakeLLM, PARTICIPANTS
from town_tavern.web import server


def _wants_all():
    return {p: {"wants_to_speak": True, "urgency": 60, "intent": "probe"}
            for p in ["boss", "gambler", "sister", "police", "reporter"]}


def test_no_trigger_when_nothing_happens(game):
    repo, gid = game
    pre = group_engine.snapshot_conflict_resolution(repo, gid)
    state = group_engine.maybe_trigger_group_discussion(
        repo, FakeLLM(_wants_all()), gid, day=2, pre_resolved=pre
    )
    assert state is None


def test_trigger_on_debt_seizing(game):
    repo, gid = game
    repo.set_world_value(gid, "debt_stage", "seizing")
    pre = group_engine.snapshot_conflict_resolution(repo, gid)
    state = group_engine.maybe_trigger_group_discussion(
        repo, FakeLLM(_wants_all()), gid, day=2, pre_resolved=pre
    )
    assert state is not None
    assert len(state.participants) == 3
    assert "boss" in state.participants          # 阿财必到场
    dialogues = [
        m for m in repo.get_timeline_messages(gid, mode="debug")
        if m["type"] == "dialogue" and m["group_id"] == state.group_id
    ]
    assert dialogues and all(m["group_id"] == state.group_id for m in dialogues)


def test_trigger_on_conflict_just_resolved(game):
    repo, gid = game
    conf = Conflict(
        id="deal_recording", kind="deal_recording",
        participants=["gambler", "reporter"], state=ConflictState.EXCHANGE_ATTEMPT,
    )
    repo.upsert_conflict(gid, conf)
    pre = group_engine.snapshot_conflict_resolution(repo, gid)   # 此刻未落槌
    conf.state = ConflictState.RESOLVED_SUCCESS                  # 今天刚落槌
    repo.upsert_conflict(gid, conf)
    state = group_engine.maybe_trigger_group_discussion(
        repo, FakeLLM(_wants_all()), gid, day=2, pre_resolved=pre
    )
    assert state is not None
    # 当事人入场;在场不足 3 人则就近补齐。
    assert "gambler" in state.participants and "reporter" in state.participants


def test_cooldown_blocks_back_to_back_triggers(game):
    """债务长期 seizing 时不该天天刷:冷却期内第二次返回 None,过了冷却又能触发。"""
    repo, gid = game
    repo.set_world_value(gid, "debt_stage", "seizing")
    llm = FakeLLM(_wants_all())
    pre = group_engine.snapshot_conflict_resolution(repo, gid)
    first = group_engine.maybe_trigger_group_discussion(repo, llm, gid, 10, pre)
    assert first is not None
    # 紧接着的几天(< 冷却天数)都不再触发。
    blocked = group_engine.maybe_trigger_group_discussion(repo, llm, gid, 11, pre)
    assert blocked is None
    # 过了冷却天数后又能开一场。
    later = group_engine.maybe_trigger_group_discussion(
        repo, llm, gid, 10 + group_engine.GROUP_DISCUSSION_COOLDOWN_DAYS, pre
    )
    assert later is not None


def test_awaiting_player_day_can_spawn_discussion(game):
    """根因修复:awaiting_player 定格日(债务 seizing)也能自发一场三人对峙。"""
    from town_tavern.engine import world_engine
    repo, gid = game
    repo.set_world_value(gid, "world_phase", "awaiting_player")
    repo.set_world_value(gid, "boss_debt", 700000)   # → debt_stage 结算为 seizing
    event = world_engine.advance_day(repo, FakeLLM(_wants_all()), gid)
    assert event is not None
    dialogues = [
        m for m in repo.get_timeline_messages(gid, mode="debug")
        if m["type"] == "dialogue" and m["group_id"]
    ]
    assert dialogues, "awaiting_player 定格日应能产出一场带 group_id 的多人对峙"


def test_disabled_flag_blocks_trigger(game, monkeypatch):
    repo, gid = game
    repo.set_world_value(gid, "debt_stage", "seizing")
    monkeypatch.setattr(group_engine, "GROUP_DISCUSSION_ENABLED", False)
    pre = group_engine.snapshot_conflict_resolution(repo, gid)
    assert group_engine.maybe_trigger_group_discussion(
        repo, FakeLLM(_wants_all()), gid, day=2, pre_resolved=pre
    ) is None


# --------------------------------------------------- API 透传 group_id/participants
def test_api_timeline_returns_group_fields(tmp_path, monkeypatch):
    from town_tavern.storage.db import get_connection
    from town_tavern.storage.repository import Repository
    db_file = tmp_path / "grp_web.db"
    seed = Repository(get_connection(db_file))
    gid = seed.create_game()
    seed.add_timeline_message(
        gid, 2, type="dialogue", text="你那盘带子呢",
        speaker_id="reporter", speaker_name="小林",
        target_id="gambler", target_name="阿龙", visibility="public",
        group_id="grp-1", participants=["reporter", "gambler", "police"],
    )
    seed.conn.close()
    monkeypatch.setattr(server, "_repo", lambda: Repository(get_connection(db_file)))
    client = TestClient(server.app)
    for mode in ("player", "debug"):
        resp = client.get("/api/timeline", params={"game_id": gid, "mode": mode})
        assert resp.status_code == 200
        msg = next(m for m in resp.json()["messages"] if m["text"] == "你那盘带子呢")
        assert msg["group_id"] == "grp-1"
        assert msg["participants"] == ["reporter", "gambler", "police"]
