"""#5 第二条冲突 boss_sister_ledger(账本摊牌)+ #2 冲突状态注入隔离。"""
from town_tavern.engine import conflict_engine
from town_tavern.engine.conflict_engine import (
    BOSS_SISTER_LEDGER, decide_ledger_transition, describe_conflicts_for_npc,
)
from town_tavern.engine.memory_engine import write_memory
from town_tavern.models.conflict import Conflict, ConflictState
from town_tavern.models.memory import MemoryType


def _fresh_ledger(state=ConflictState.SUSPICION, age=0, stall=5):
    return Conflict(
        id="boss_sister_ledger", kind="boss_sister_ledger",
        participants=["boss", "sister"], state=state,
        age_in_state=age, max_stall_days=stall, created_day=1,
    )


# ---- 纯决策函数 ----
def test_ledger_progress_advances_one_stage():
    state, _r, cons = decide_ledger_transition(
        _fresh_ledger(), tension=50, boss_present=True, sister_present=True,
        has_progress_signal=True,
    )
    assert state == ConflictState.LEDGER_FOUND
    assert cons is None


def test_ledger_demand_truth_resolves_by_tension():
    # 高张力 → 崩裂
    s, _r, c = decide_ledger_transition(
        _fresh_ledger(ConflictState.DEMAND_TRUTH), tension=80,
        boss_present=True, sister_present=True, has_progress_signal=True,
    )
    assert s == ConflictState.RESOLVED_BREAKDOWN and c is not None
    # 中张力 → 重建信任
    s, _r, c = decide_ledger_transition(
        _fresh_ledger(ConflictState.DEMAND_TRUTH), tension=50,
        boss_present=True, sister_present=True, has_progress_signal=True,
    )
    assert s == ConflictState.RESOLVED_TRUST
    # 低张力 → 被糊弄掩盖
    s, _r, c = decide_ledger_transition(
        _fresh_ledger(ConflictState.DEMAND_TRUTH), tension=20,
        boss_present=True, sister_present=True, has_progress_signal=True,
    )
    assert s == ConflictState.RESOLVED_COVERUP


def test_ledger_absent_participant_stalls():
    s, _r, c = decide_ledger_transition(
        _fresh_ledger(ConflictState.LEDGER_FOUND), tension=50,
        boss_present=False, sister_present=True, has_progress_signal=True,
    )
    assert s == ConflictState.LEDGER_FOUND and c is None


def test_ledger_stall_force_resolves():
    s, _r, c = decide_ledger_transition(
        _fresh_ledger(ConflictState.PARTIAL_CONFESSION, age=5, stall=5), tension=30,
        boss_present=True, sister_present=True, has_progress_signal=False,
    )
    assert s == ConflictState.RESOLVED_COVERUP and c is not None


def test_ledger_min_days_floor_blocks_early_resolution():
    # 未满 min_days 时,即便到了 DEMAND_TRUTH+信号也不提早落槌
    s, _r, c = decide_ledger_transition(
        _fresh_ledger(ConflictState.DEMAND_TRUTH), tension=50,
        boss_present=True, sister_present=True, has_progress_signal=True,
        days_since_created=1, min_days=3,
    )
    assert s == ConflictState.DEMAND_TRUTH and c is None


# ---- 端到端 ----
def test_ledger_end_to_end_resolves_no_truth_progress(game):
    """账本线在 max 天内落槌,且全程不写真相进度(同录音线的红线)。"""
    repo, gid = game
    repo.set_world_value(gid, "global_tension", 50)  # 触发账本线 + 中张力结局
    resolved_day = None
    for day in range(2, 10):
        write_memory(repo, gid, "boss", day, "又翻到对不上的账本,瞒不住了", MemoryType.REFLECTION, 60)
        conflict_engine.tick_conflicts(repo, gid, day)
        assert repo.get_world_state(gid).athou_truth_progress == 0  # 红线
        conf = repo.get_conflict(gid, BOSS_SISTER_LEDGER)
        if conf and conf.is_resolved():
            resolved_day = day
            break
    assert resolved_day is not None
    assert resolved_day - 2 <= 5
    logs = repo.get_conflict_logs(gid, BOSS_SISTER_LEDGER)
    assert logs[-1]["to_state"].startswith("RESOLVED_")


def test_ledger_not_triggered_when_calm(game):
    repo, gid = game
    repo.set_world_value(gid, "global_tension", 10)
    repo.set_world_value(gid, "truth_pressure", 0)
    repo.set_world_value(gid, "exposure_stage", "normal")
    conflict_engine.tick_conflicts(repo, gid, 2)
    assert repo.get_conflict(gid, BOSS_SISTER_LEDGER) is None


# ---- #2 注入与知识隔离 ----
def test_describe_conflicts_only_for_participants(game):
    repo, gid = game
    repo.set_world_value(gid, "truth_pressure", 35)
    repo.set_world_value(gid, "exposure_stage", "stirring")
    conflict_engine.ensure_deal_recording_conflict(repo, gid, 2)
    conflict_engine.force_resolve_deal_recording(repo, gid, 3, reason="测试")

    # 参与者(赌徒)能看到该冲突的人话状态
    brief_gambler = describe_conflicts_for_npc(repo, gid, "gambler")
    assert "录音交易" in brief_gambler
    # 旁观者(淑芬)不是录音交易参与者 → 不应得知其结算细节(知识隔离)
    brief_sister = describe_conflicts_for_npc(repo, gid, "sister")
    assert "录音交易" not in brief_sister


def test_describe_resolved_conflict_warns_not_to_replay(game):
    repo, gid = game
    repo.set_world_value(gid, "truth_pressure", 35)
    repo.set_world_value(gid, "exposure_stage", "crisis")
    conflict_engine.ensure_deal_recording_conflict(repo, gid, 2)
    conflict_engine.tick_conflicts(repo, gid, 3)  # crisis → 截断落槌
    brief = describe_conflicts_for_npc(repo, gid, "gambler")
    assert "已了结" in brief  # 注入了"这事已结算、别再当没发生"的提示


# ---- #6 按角色分层:旁观者只得模糊提示,不泄露内情 ----
def test_bystander_gets_vague_hint_when_participant_hidden(game):
    """参与者(赌徒)蛰伏后,旁观者(淑芬)只看到'最近不太露面'这类表象,看不到冲突状态。"""
    repo, gid = game
    repo.set_world_value(gid, "truth_pressure", 35)
    conflict_engine.ensure_deal_recording_conflict(repo, gid, 2)
    repo.set_npc_status(gid, "gambler", "hiding", until_day=8)

    brief_sister = describe_conflicts_for_npc(repo, gid, "sister")
    assert "不太露面" in brief_sister              # 给了模糊表象
    assert "录音交易" not in brief_sister           # 不泄露冲突标签
    assert "RESOLVED" not in brief_sister            # 不泄露状态机
    assert "NEGOTIATING" not in brief_sister


def test_bystander_no_hint_when_all_participants_present(game):
    """所有参与者都在场时,旁观者得不到任何冲突动静(不凭空生成旁观信息)。"""
    repo, gid = game
    repo.set_world_value(gid, "truth_pressure", 35)
    conflict_engine.ensure_deal_recording_conflict(repo, gid, 2)
    brief_sister = describe_conflicts_for_npc(repo, gid, "sister")
    assert brief_sister == ""


def test_participant_still_sees_full_state_with_hidden_peer(game):
    """对照:参与者本人始终看到完整状态(含对手 id 与人话状态),不受旁观分层影响。"""
    repo, gid = game
    repo.set_world_value(gid, "truth_pressure", 35)
    conflict_engine.ensure_deal_recording_conflict(repo, gid, 2)
    repo.set_npc_status(gid, "gambler", "hiding", until_day=8)
    brief_gambler = describe_conflicts_for_npc(repo, gid, "gambler")
    assert "录音交易" in brief_gambler
    assert "进行中" in brief_gambler
