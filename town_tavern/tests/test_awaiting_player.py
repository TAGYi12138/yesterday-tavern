"""P0/P1/P3:无人值守封顶(awaiting_player)+ 债务终局 + 压力饱和心理状态。

- P0:世界烧到临界 / 玩家长期缺席后进入 awaiting_player,停止自动推进核心冲突、
  危机升级与新建主线;玩家一介入即退回 running。并堵住"危机 none→active 无限循环"。
- P1:债务到 seizing 后启动接管倒计时,归零即停在最后一天等玩家(不自动落槌)。
- P3:NPC 压力见顶落 mental_state,使决策提示出现失常侧写;压力回落则恢复。
"""
from town_tavern.config import (
    AWAIT_PLAYER_IDLE_DAYS, DEBT_CRITICAL, DEBT_SEIZE_COUNTDOWN_DAYS,
    NPC_SATURATION_STRESS, TRUTH_PRESSURE_PLATFORM,
)
from town_tavern.engine import conflict_engine, crisis_engine, npc_engine
from town_tavern.engine.world_engine import (
    _settle_stages, tick_debt_endgame, update_exposure_stage, update_world_phase,
)


# --------------------------------------------------------------------------- P0
def test_climax_enters_awaiting_player(game):
    repo, gid = game
    # 真相压力绷到平台 + 全局紧张达阈值 → 高压临界
    repo.set_world_value(gid, "truth_pressure", TRUTH_PRESSURE_PLATFORM)
    repo.set_world_value(gid, "global_tension", 85)
    assert update_world_phase(repo, gid) == "awaiting_player"
    assert repo.get_world_state(gid).is_awaiting_player()


def test_seizing_debt_enters_awaiting_player(game):
    repo, gid = game
    repo.set_world_value(gid, "boss_debt", DEBT_CRITICAL)
    _settle_stages(repo, gid)
    assert repo.get_world_state(gid).debt_stage == "seizing"
    assert update_world_phase(repo, gid) == "awaiting_player"


def test_long_absence_enters_awaiting_player(game):
    repo, gid = game
    # 玩家从未行动(player_last_seen_day=0),把当前天推到超过缺席阈值
    repo.set_world_value(gid, "current_day", AWAIT_PLAYER_IDLE_DAYS + 1)
    assert update_world_phase(repo, gid) == "awaiting_player"


def test_player_action_returns_world_to_running(game):
    repo, gid = game
    repo.set_world_value(gid, "current_day", AWAIT_PLAYER_IDLE_DAYS + 5)
    assert update_world_phase(repo, gid) == "awaiting_player"
    # 玩家介入打点 → 缺席清零,且高压条件不成立 → 退回 running
    world = repo.get_world_state(gid)
    repo.set_world_value(gid, "player_last_seen_day", world.current_day)
    assert update_world_phase(repo, gid) == "running"


def test_awaiting_player_blocks_new_conflicts(game):
    repo, gid = game
    # 满足冲突触发条件(真相压力升温),但 awaiting_player 下仍不新建主线冲突
    repo.set_world_value(gid, "truth_pressure", 60)
    conflict_engine.tick_conflicts(repo, gid, 3, allow_new=False)
    assert repo.get_active_conflicts(gid) == []
    # 正常相位则会新建
    conflict_engine.tick_conflicts(repo, gid, 3, allow_new=True)
    assert repo.get_active_conflicts(gid) != []


def test_awaiting_player_blocks_crisis_reignition(game):
    """根因回归:曝光被钉在 crisis,但 awaiting_player 时危机【不再】none→active 重燃。"""
    repo, gid = game
    repo.set_world_value(gid, "police_exposure_risk", 95)
    repo.set_world_value(gid, "exposure_stage", "crisis")
    repo.set_world_value(gid, "crisis_phase", "none")
    repo.set_world_value(gid, "world_phase", "awaiting_player")
    update_exposure_stage(repo, gid)
    crisis_engine.tick_crisis(repo, gid, 5)
    crisis_engine.tick_crisis_phase(repo, gid, 5)
    # 仍停在 none(未重燃为 active),且未触发任何硬事件
    assert repo.get_world_state(gid).crisis_phase == "none"


def test_directive_reflects_awaiting_player(game):
    repo, gid = game
    repo.set_world_value(gid, "world_phase", "awaiting_player")
    directive = repo.get_world_state(gid).crisis_directive()
    assert "封顶" in directive or "等" in directive


# --------------------------------------------------------------------------- P1
def test_debt_endgame_countdown_starts_and_holds(game):
    repo, gid = game
    repo.set_world_value(gid, "boss_debt", DEBT_CRITICAL)
    _settle_stages(repo, gid)
    assert repo.get_world_state(gid).debt_stage == "seizing"

    # 首日:下最后通牒,启动倒计时
    lines = tick_debt_endgame(repo, gid, 2)
    assert lines
    assert repo.get_world_state(gid).debt_seize_countdown == DEBT_SEIZE_COUNTDOWN_DAYS

    # 逐天递减,直至停在最后一天(=1)且不再自动落槌
    countdowns = []
    for day in range(3, 12):
        tick_debt_endgame(repo, gid, day)
        countdowns.append(repo.get_world_state(gid).debt_seize_countdown)
    assert min(countdowns) == 1
    assert countdowns[-1] == 1  # 永远停在门口等玩家


def test_debt_endgame_resets_when_leaving_seizing(game):
    repo, gid = game
    repo.set_world_value(gid, "boss_debt", DEBT_CRITICAL)
    _settle_stages(repo, gid)
    tick_debt_endgame(repo, gid, 2)
    assert repo.get_world_state(gid).debt_seize_countdown >= 0
    # 债务被压下、退出 seizing → 倒计时复位
    repo.set_world_value(gid, "boss_debt", 0)
    _settle_stages(repo, gid)
    tick_debt_endgame(repo, gid, 3)
    assert repo.get_world_state(gid).debt_seize_countdown == -1


# --------------------------------------------------------------------------- P3
def test_saturated_stress_sets_mental_state(game):
    repo, gid = game
    npc = repo.get_all_npcs(gid)[0]
    repo.update_npc_stress(gid, npc.id, NPC_SATURATION_STRESS - npc.stress)
    npc_engine.update_mental_states(repo, gid)
    refreshed = repo.get_npc(gid, npc.id)
    assert refreshed.mental_state in npc_engine._MENTAL_STATES
    # 心理状态渲染进决策提示文本
    assert refreshed.mental_state_text()


def test_mental_state_clears_when_stress_drops(game):
    repo, gid = game
    npc = repo.get_all_npcs(gid)[0]
    repo.update_npc_stress(gid, npc.id, NPC_SATURATION_STRESS - npc.stress)
    npc_engine.update_mental_states(repo, gid)
    assert repo.get_npc(gid, npc.id).mental_state
    # 压力回落到阈值以下 → 恢复常态
    repo.update_npc_stress(gid, npc.id, -50)
    npc_engine.update_mental_states(repo, gid)
    assert repo.get_npc(gid, npc.id).mental_state == ""


def test_mental_state_deterministic(game):
    repo, gid = game
    npcs = repo.get_all_npcs(gid)
    first = npc_engine._pick_mental_state(npcs[0])
    again = npc_engine._pick_mental_state(npcs[0])
    assert first == again
    assert first in npc_engine._MENTAL_STATES
