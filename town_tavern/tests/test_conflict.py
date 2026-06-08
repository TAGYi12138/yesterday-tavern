"""冲突状态机(P0 北极星)的测试:纯决策函数 + 端到端推进。"""
from town_tavern.engine import conflict_engine
from town_tavern.engine.conflict_engine import decide_transition
from town_tavern.engine.consequence import get_flag
from town_tavern.engine.memory_engine import write_memory
from town_tavern.models.conflict import Conflict, ConflictState
from town_tavern.models.memory import MemoryType


def _fresh_conflict(state=ConflictState.NEGOTIATING, age=0, stall=5):
    return Conflict(
        id="deal_recording", kind="deal_recording",
        participants=["gambler", "reporter"], state=state,
        age_in_state=age, max_stall_days=stall, created_day=1,
    )


def test_progress_advances_one_stage():
    state, reason, cons = decide_transition(
        _fresh_conflict(), exposure_risk=40, exposure_stage="normal",
        holder_present=True, buyer_present=True, has_progress_signal=True,
    )
    assert state == ConflictState.PARTIAL_PROOF_GIVEN
    assert cons is None


def test_exchange_attempt_progress_resolves_success():
    state, reason, cons = decide_transition(
        _fresh_conflict(ConflictState.EXCHANGE_ATTEMPT),
        exposure_risk=40, exposure_stage="normal",
        holder_present=True, buyer_present=True, has_progress_signal=True,
    )
    assert state == ConflictState.RESOLVED_SUCCESS
    assert cons is not None and cons.flags.get("recording_delivered") is True


def test_crisis_exposure_interrupts():
    state, reason, cons = decide_transition(
        _fresh_conflict(ConflictState.MONEY_SHOWN),
        exposure_risk=95, exposure_stage="crisis",
        holder_present=True, buyer_present=True, has_progress_signal=True,
    )
    assert state == ConflictState.RESOLVED_INTERRUPTED
    # 红线#2:截断必带二级线索
    assert cons.flags.get("secondary_clue_available") is True


def test_absent_participant_stalls():
    state, reason, cons = decide_transition(
        _fresh_conflict(ConflictState.MEETING_SET),
        exposure_risk=40, exposure_stage="normal",
        holder_present=False, buyer_present=True, has_progress_signal=True,
    )
    assert state == ConflictState.MEETING_SET  # 不进阶
    assert cons is None


def test_stall_force_resolution_evidence_compromised():
    """拖延封顶 + 曝光不高 + 未到交接阶段 → 证据出岔子(必带二级线索)。"""
    state, reason, cons = decide_transition(
        _fresh_conflict(ConflictState.NEGOTIATING, age=5, stall=5),
        exposure_risk=40, exposure_stage="normal",
        holder_present=True, buyer_present=True, has_progress_signal=False,
    )
    assert state == ConflictState.RESOLVED_EVIDENCE_COMPROMISED
    assert cons.flags.get("secondary_clue_available") is True


def test_no_signal_stalls():
    state, reason, cons = decide_transition(
        _fresh_conflict(ConflictState.NEGOTIATING, age=1),
        exposure_risk=40, exposure_stage="normal",
        holder_present=True, buyer_present=True, has_progress_signal=False,
    )
    assert state == ConflictState.NEGOTIATING
    assert cons is None


def test_resolved_is_terminal():
    state, reason, cons = decide_transition(
        _fresh_conflict(ConflictState.RESOLVED_SUCCESS),
        exposure_risk=95, exposure_stage="crisis",
        holder_present=True, buyer_present=True, has_progress_signal=True,
    )
    assert state == ConflictState.RESOLVED_SUCCESS
    assert cons is None


def test_end_to_end_resolves_within_five_days_no_truth_progress(game):
    """北极星:有推进信号且曝光温和时,录音交易在 5 天内落槌,且全程不写真相进度。"""
    repo, gid = game
    repo.set_world_value(gid, "truth_pressure", 35)
    repo.set_world_value(gid, "truth_stage", "stirring")
    repo.set_world_value(gid, "police_exposure_risk", 40)
    repo.set_world_value(gid, "exposure_stage", "normal")
    resolved_day = None
    for day in range(2, 9):
        write_memory(repo, gid, "reporter", day, "推进了码头录音的交易交接", MemoryType.DIALOGUE, 60)
        conflict_engine.tick_conflicts(repo, gid, day)
        assert repo.get_world_state(gid).athou_truth_progress == 0  # 红线
        conf = repo.get_conflict(gid, "deal_recording")
        if conf and conf.is_resolved():
            resolved_day = day
            break
    assert resolved_day is not None
    assert resolved_day - 2 <= 5  # 自创建起 5 天内落槌
    logs = repo.get_conflict_logs(gid, "deal_recording")
    assert logs[-1]["to_state"].startswith("RESOLVED_")


def test_not_triggered_when_calm(game):
    """局势平静(低压力、normal 曝光)时不创建冲突。"""
    repo, gid = game
    repo.set_world_value(gid, "truth_pressure", 0)
    repo.set_world_value(gid, "exposure_stage", "normal")
    conflict_engine.tick_conflicts(repo, gid, 2)
    assert repo.get_conflict(gid, "deal_recording") is None


def test_force_resolve_helper(game):
    repo, gid = game
    repo.set_world_value(gid, "truth_pressure", 35)
    repo.set_world_value(gid, "exposure_stage", "stirring")
    conflict_engine.ensure_deal_recording_conflict(repo, gid, 2)
    line = conflict_engine.force_resolve_deal_recording(repo, gid, 3, reason="测试强制结算")
    assert line is not None
    assert repo.get_conflict(gid, "deal_recording").is_resolved()
    # 再次强制结算返回 None(已落槌)
    assert conflict_engine.force_resolve_deal_recording(repo, gid, 4, reason="x") is None
