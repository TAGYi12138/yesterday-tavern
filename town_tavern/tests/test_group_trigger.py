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
