"""#3 crisis_days 语义统一 → exposure_stage_days(通用连续天数 + 派生 crisis_days)。"""
from town_tavern.engine import crisis_engine
from town_tavern.engine.consequence import get_flag
from town_tavern.engine.world_engine import update_exposure_stage


def _set_stage(repo, gid, stage):
    repo.set_world_value(gid, "exposure_stage", stage)


def test_stage_days_increments_while_stage_unchanged(game):
    repo, gid = game
    _set_stage(repo, gid, "suppressing")
    update_exposure_stage(repo, gid)
    assert repo.get_world_state(gid).exposure_stage_days == 1
    update_exposure_stage(repo, gid)
    update_exposure_stage(repo, gid)
    assert repo.get_world_state(gid).exposure_stage_days == 3


def test_stage_days_resets_on_stage_change(game):
    repo, gid = game
    _set_stage(repo, gid, "watching")
    update_exposure_stage(repo, gid)
    update_exposure_stage(repo, gid)
    assert repo.get_world_state(gid).exposure_stage_days == 2
    _set_stage(repo, gid, "suppressing")
    update_exposure_stage(repo, gid)
    assert repo.get_world_state(gid).exposure_stage_days == 1


def test_crisis_days_zero_outside_crisis(game):
    """关键修复:suppressing 阶段哪怕连续多天,crisis_days 也恒为 0。"""
    repo, gid = game
    _set_stage(repo, gid, "suppressing")
    for _ in range(3):
        update_exposure_stage(repo, gid)
    w = repo.get_world_state(gid)
    assert w.exposure_stage_days == 3
    assert w.crisis_days == 0


def test_crisis_days_tracks_stage_days_in_crisis(game):
    repo, gid = game
    _set_stage(repo, gid, "crisis")
    update_exposure_stage(repo, gid)
    update_exposure_stage(repo, gid)
    w = repo.get_world_state(gid)
    assert w.exposure_stage_days == 2
    assert w.crisis_days == 2


def test_crisis_events_only_fire_in_crisis_stage(game):
    """suppressing 阶段不触发任何 crisis 硬事件(即便连续多天)。"""
    repo, gid = game
    _set_stage(repo, gid, "suppressing")
    for d in range(2, 6):
        update_exposure_stage(repo, gid)
        crisis_engine.tick_crisis(repo, gid, d)
    assert not get_flag(repo, gid, "crisis_witness_threatened")
    assert not get_flag(repo, gid, "crisis_raid")
    # 切到 crisis 后才开始逐级触发
    _set_stage(repo, gid, "crisis")
    update_exposure_stage(repo, gid)
    crisis_engine.tick_crisis(repo, gid, 6)
    assert get_flag(repo, gid, "crisis_witness_threatened")
