"""#4 危机生命周期阶段机:危机会"降温"并留"余波",不再一直在 crisis 烧。

转移链:none →(进入 crisis)active →(烧到顶级 L4)cooling →(曝光跌出 crisis)
aftermath →(宽限耗尽)none。降温/余波期不再强触发硬事件。
"""
from town_tavern.engine import crisis_engine
from town_tavern.engine.consequence import get_flag
from town_tavern.engine.world_engine import _settle_stages, update_exposure_stage


def _crisis_day(repo, gid, day):
    """模拟一天的危机相关结算(不走 LLM 事件生成)。"""
    _settle_stages(repo, gid)
    update_exposure_stage(repo, gid)
    crisis_engine.tick_crisis(repo, gid, day)
    crisis_engine.tick_crisis_phase(repo, gid, day)


def test_crisis_phases_through_cooling_to_aftermath_and_back(game):
    repo, gid = game
    # 把曝光顶到 crisis,逐天推进让危机一路烧到顶级并降温
    repo.set_world_value(gid, "police_exposure_risk", 90)
    repo.set_world_value(gid, "exposure_stage", "crisis")

    phases = []
    for day in range(2, 14):
        _crisis_day(repo, gid, day)
        phases.append(repo.get_world_state(gid).crisis_phase)

    # 必须经历完整阶段链(顺序出现)
    assert "active" in phases
    i_active = phases.index("active")
    assert "cooling" in phases[i_active:]
    i_cool = phases.index("cooling")
    assert "aftermath" in phases[i_cool:]
    i_after = phases.index("aftermath")
    # 余波最终散去回到 none
    assert phases[-1] == "none"
    assert i_active < i_cool < i_after


def test_no_hard_events_during_cooling_and_aftermath(game):
    repo, gid = game
    # 直接置于降温阶段 + 曝光仍在 crisis 区间,且尚未触发过任何级别
    repo.set_world_value(gid, "police_exposure_risk", 95)
    repo.set_world_value(gid, "exposure_stage", "crisis")
    repo.set_world_value(gid, "crisis_phase", "cooling")
    update_exposure_stage(repo, gid)  # crisis_days > 0
    lines = crisis_engine.tick_crisis(repo, gid, 5)
    # 降温期:即便曝光仍在 crisis,也不再强触发逐级硬事件
    assert lines == []
    assert not get_flag(repo, gid, "crisis_witness_threatened")

    # 余波期同理
    repo.set_world_value(gid, "crisis_phase", "aftermath")
    lines2 = crisis_engine.tick_crisis(repo, gid, 6)
    assert lines2 == []
    assert not get_flag(repo, gid, "crisis_raid")


def test_cooling_drops_exposure(game):
    repo, gid = game
    repo.set_world_value(gid, "police_exposure_risk", 95)
    repo.set_world_value(gid, "exposure_stage", "crisis")
    repo.set_world_value(gid, "crisis_phase", "cooling")
    before = repo.get_world_state(gid).police_exposure_risk
    crisis_engine.tick_crisis_phase(repo, gid, 5)
    after = repo.get_world_state(gid).police_exposure_risk
    assert after < before


def test_directive_reflects_cooling_phase(game):
    repo, gid = game
    repo.set_world_value(gid, "exposure_stage", "crisis")
    repo.set_world_value(gid, "crisis_phase", "cooling")
    directive = repo.get_world_state(gid).crisis_directive()
    # 降温期的老陈口径应是"收敛/收锋芒",而非 crisis 的"不择手段"
    assert "收敛" in directive or "收锋芒" in directive
    assert "不择手段" not in directive
