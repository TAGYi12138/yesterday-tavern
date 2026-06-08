"""PR4 危机倒计时 + PR5 记忆降噪/昨日摘要 + PR6 紧张度档位 的测试。"""
from town_tavern.engine import crisis_engine
from town_tavern.engine.consequence import get_flag
from town_tavern.engine.memory_engine import (
    build_personal_yesterday_summary, write_memory,
)
from town_tavern.models.memory import MemoryType
from town_tavern.models.world import WorldState


# ---------------- PR6: global_tension 档位 ----------------
def test_tension_target_takes_max_tier():
    w = WorldState(exposure_stage="crisis", debt_stage="stable", truth_stage="latent")
    assert w.tension_target() == 85
    w2 = WorldState(exposure_stage="normal", debt_stage="seizing", truth_stage="stirring")
    assert w2.tension_target() == 75  # 债务 seizing=75 最高


# ---------------- PR4: 危机倒计时逐级硬事件 ----------------
def test_crisis_escalation_levels_and_red_line(game):
    repo, gid = game
    repo.set_world_value(gid, "exposure_stage", "crisis")
    before_truth_prog = repo.get_world_state(gid).athou_truth_progress

    # 第1天:威胁证人
    crisis_engine.tick_crisis(repo, gid, 2)
    assert repo.get_world_state(gid).crisis_days == 1
    assert get_flag(repo, gid, "crisis_witness_threatened")

    # 第2天:搜查,赌徒蛰伏
    crisis_engine.tick_crisis(repo, gid, 3)
    assert repo.get_world_state(gid).crisis_days == 2
    assert repo.get_npc(gid, "gambler").status == "hiding"

    # 第3天:失踪/抢证 → 必带二级线索(红线#2)
    crisis_engine.tick_crisis(repo, gid, 4)
    assert repo.get_world_state(gid).crisis_days == 3
    assert get_flag(repo, gid, "crisis_disappearance")
    assert get_flag(repo, gid, "secondary_clue_available")

    # 全程不写真相进度(红线#1)
    assert repo.get_world_state(gid).athou_truth_progress == before_truth_prog


def test_crisis_days_reset_when_leaving_crisis(game):
    repo, gid = game
    repo.set_world_value(gid, "exposure_stage", "crisis")
    crisis_engine.tick_crisis(repo, gid, 2)
    assert repo.get_world_state(gid).crisis_days == 1
    # 离开危机阶段:清零并复位触发 flag
    repo.set_world_value(gid, "exposure_stage", "watching")
    crisis_engine.tick_crisis(repo, gid, 3)
    assert repo.get_world_state(gid).crisis_days == 0
    assert not get_flag(repo, gid, "crisis_witness_threatened")


# ---------------- PR5: 记忆降噪 + 昨日个人摘要 ----------------
def test_recent_memory_rumor_cap(game):
    repo, gid = game
    # 写入大量传闻 + 少量自身行动
    for i in range(6):
        write_memory(repo, gid, "boss", 2, f"听说传闻{i}", MemoryType.RUMOR, 35)
    write_memory(repo, gid, "boss", 2, "我自己今天去催了债", MemoryType.PLAYER_ACTION, 60)
    recent = repo.get_recent_memories(gid, "boss", limit=5)
    rumor_count = sum(1 for m in recent if m.type == MemoryType.RUMOR)
    # 传闻被限制(RECENT_RUMOR_CAP 默认 2),自身行动记忆得以保留
    assert rumor_count <= 2
    assert any(m.type == MemoryType.PLAYER_ACTION for m in recent)


def test_personal_yesterday_summary_isolated(game):
    repo, gid = game
    write_memory(repo, gid, "gambler", 4, "我昨天和记者谈了交易", MemoryType.DIALOGUE, 80)
    write_memory(repo, gid, "boss", 4, "阿财自己的私密心事", MemoryType.SECRET, 90)
    summ = build_personal_yesterday_summary(repo, gid, "gambler", today=5)
    assert "交易" in summ
    assert "私密心事" not in summ  # 不串别人的记忆
    # 第一天无昨日
    assert build_personal_yesterday_summary(repo, gid, "gambler", today=1) == ""
