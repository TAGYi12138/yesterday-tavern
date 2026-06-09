"""#3 结算后目标回流根治:冲突落槌时硬改写参与者 current_goal。

软提示挡不住 LLM 复读旧交易——current_goal 是持久字段,必须在终态确定性改写,
让 NPC 次日只能围绕残局/二级线索/补救行动,而非"再卖一次旧录音"。
"""
from town_tavern.engine import conflict_engine
from town_tavern.engine.conflict_engine import (
    _BUYER, _HOLDER, _RESOLUTION_GOALS, rewrite_goals_on_resolution,
)
from town_tavern.models.conflict import ConflictState


def test_force_resolve_rewrites_participant_goals(game):
    repo, gid = game
    before_holder = repo.get_npc(gid, _HOLDER).current_goal
    repo.set_world_value(gid, "truth_pressure", 35)
    conflict_engine.ensure_deal_recording_conflict(repo, gid, 1)
    # 默认曝光低 → 终态为证据作废(RESOLVED_EVIDENCE_COMPROMISED)
    line = conflict_engine.force_resolve_deal_recording(repo, gid, 3, reason="测试落槌")
    assert line is not None

    conflict = repo.get_conflict(gid, conflict_engine.DEAL_RECORDING)
    expected = _RESOLUTION_GOALS[conflict.state]
    holder_goal = repo.get_npc(gid, _HOLDER).current_goal
    buyer_goal = repo.get_npc(gid, _BUYER).current_goal
    # 目标已被改写为终态后的新目标,且确实变了
    assert holder_goal == expected[_HOLDER]
    assert buyer_goal == expected[_BUYER]
    assert holder_goal != before_holder
    # 硬约束语义:不再"兜售/再卖"那盘录音
    assert "兜售" not in holder_goal or "不再" in holder_goal


def test_rewrite_only_touches_participants(game):
    repo, gid = game
    repo.set_world_value(gid, "truth_pressure", 35)
    conflict_engine.ensure_deal_recording_conflict(repo, gid, 1)
    conflict = repo.get_conflict(gid, conflict_engine.DEAL_RECORDING)
    # 旁人(老陈)目标不应被这桩冲突的结算波及
    police_before = repo.get_npc(gid, "police").current_goal
    rewrite_goals_on_resolution(repo, gid, conflict, ConflictState.RESOLVED_INTERRUPTED)
    assert repo.get_npc(gid, "police").current_goal == police_before
    # 参与者(赌徒)被改写为"被截下/蛰伏"语义
    assert repo.get_npc(gid, _HOLDER).current_goal == \
        _RESOLUTION_GOALS[ConflictState.RESOLVED_INTERRUPTED][_HOLDER]


def test_rewrite_noop_for_non_terminal_state(game):
    repo, gid = game
    repo.set_world_value(gid, "truth_pressure", 35)
    conflict_engine.ensure_deal_recording_conflict(repo, gid, 1)
    conflict = repo.get_conflict(gid, conflict_engine.DEAL_RECORDING)
    holder_before = repo.get_npc(gid, _HOLDER).current_goal
    # 非终态(谈判中)没有目标映射 → 不改写
    changed = rewrite_goals_on_resolution(repo, gid, conflict, ConflictState.NEGOTIATING)
    assert changed == []
    assert repo.get_npc(gid, _HOLDER).current_goal == holder_before
