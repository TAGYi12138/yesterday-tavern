"""#4 节奏参数可配置 + 三档模式(debug_fast / demo_normal / slow_burn)。"""
import importlib
import os

from town_tavern import config


def test_default_is_debug_fast():
    """默认(未设 GAME_PACE)走 debug_fast:快速爆发,便于测试。"""
    assert config.GAME_PACE == "debug_fast"
    assert config.CONFLICT_MIN_DAYS == 0
    assert config.CRISIS_ESCALATION_INTERVAL == 1


def test_three_presets_exist_with_expected_values():
    p = config._PACE_PRESETS
    assert set(p) == {"debug_fast", "demo_normal", "slow_burn"}
    # demo_normal:10~15 天出大事件(给玩家 demo 用)
    assert p["demo_normal"]["conflict_min_days"] == 3
    assert p["demo_normal"]["crisis_escalation_interval"] == 2
    assert p["demo_normal"]["debt_interest_per_day"] == 8000
    # slow_burn:30 天以上慢热
    assert p["slow_burn"]["conflict_min_days"] == 5
    assert p["slow_burn"]["crisis_escalation_interval"] == 3


_SNAPSHOT_ATTRS = (
    "GAME_PACE", "CONFLICT_MIN_DAYS", "CONFLICT_MAX_DAYS",
    "CRISIS_ESCALATION_INTERVAL", "DEBT_DAILY_INTEREST", "EXPOSURE_DAILY_DECAY",
)


def _snapshot_with_env(**env) -> dict:
    """在指定环境变量下重载 config,抓取关键参数快照,然后【还原】为默认,避免污染其它测试。"""
    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(config)
        return {a: getattr(config, a) for a in _SNAPSHOT_ATTRS}
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(config)  # 还原为默认


def test_switching_mode_changes_pacing():
    snap = _snapshot_with_env(GAME_PACE="demo_normal", CONFLICT_MIN_DAYS=None,
                              CRISIS_ESCALATION_INTERVAL=None)
    assert snap["GAME_PACE"] == "demo_normal"
    assert snap["CONFLICT_MIN_DAYS"] == 3
    assert snap["CRISIS_ESCALATION_INTERVAL"] == 2
    # 恢复后默认值回到 debug_fast
    assert config.CONFLICT_MIN_DAYS == 0


def test_explicit_env_overrides_preset():
    snap = _snapshot_with_env(GAME_PACE="slow_burn", CONFLICT_MIN_DAYS="9")
    # 显式环境变量优先于预设
    assert snap["CONFLICT_MIN_DAYS"] == 9
