"""#1 三种日志视角的隔离测试:debug 全摊开 / player 只见现象 / npc 只见己事。"""
from town_tavern.engine import conflict_engine
from town_tavern.engine.memory_engine import write_memory
from town_tavern.engine.reporting import (
    _DEV_TERMS, build_debug_report, build_npc_private_report, build_player_report,
)
from town_tavern.models.event import Event, EventType
from town_tavern.models.memory import MemoryType


def _seed_resolved_recording(repo, gid, day=4):
    repo.set_world_value(gid, "truth_pressure", 35)
    repo.set_world_value(gid, "exposure_stage", "stirring")
    conflict_engine.ensure_deal_recording_conflict(repo, gid, 2)
    conflict_engine.force_resolve_deal_recording(repo, gid, day, reason="测试落槌")


def test_debug_report_exposes_system_state(game):
    repo, gid = game
    _seed_resolved_recording(repo, gid, day=4)
    report = build_debug_report(repo, gid, 4)
    # debug 视角应当看得到状态机/结算等系统真相
    assert "deal_recording" in report
    assert "RESOLVED_" in report
    assert "冲突状态机" in report


def test_player_report_hides_all_dev_terms(game):
    repo, gid = game
    _seed_resolved_recording(repo, gid, day=4)
    # 一桩玩家能观察到的公开事件
    repo.add_event(gid, Event(
        day=4, type=EventType.DAILY_LIFE, title="酒馆日常",
        summary="小林坐在角落，捏着半张皱掉的纸。", actors=["reporter"], visibility="public",
    ))
    # 一桩私密事件:玩家视角不该出现
    repo.add_event(gid, Event(
        day=4, type=EventType.REPORTER_INVESTIGATION, title="密谈",
        summary="录音被police扣下的内幕被私下议论。", actors=["reporter"], visibility="private",
    ))
    report = build_player_report(repo, gid, 4)
    assert "小林坐在角落" in report
    assert "录音被" not in report  # 私密事件不剧透
    for term in _DEV_TERMS:
        assert term not in report, f"玩家视角泄露了开发者术语: {term}"


def test_player_report_shows_absentee_without_reason(game):
    repo, gid = game
    repo.set_npc_status(gid, "gambler", "away", until_day=13)
    report = build_player_report(repo, gid, 5)
    # 玩家只看到"今天没来",看不到 away/状态机原因
    npc = repo.get_npc(gid, "gambler")
    assert f"{npc.name}今天没来" in report
    assert "away" not in report
    assert "13" not in report


def test_npc_private_report_only_own_memory(game):
    repo, gid = game
    write_memory(repo, gid, "gambler", 5, "我把录音的事跟记者透了底", MemoryType.DIALOGUE, 70)
    write_memory(repo, gid, "reporter", 5, "记者私下记下的另一件事", MemoryType.DIALOGUE, 70)
    report = build_npc_private_report(repo, gid, "gambler", 5)
    assert "我把录音的事跟记者透了底" in report
    # 知识隔离:不串入别人(记者)的记忆
    assert "记者私下记下的另一件事" not in report
