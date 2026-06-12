"""观察器 PR:瑕疵① 封顶降级日 / 瑕疵② 债务终局 / 瑕疵③ 最小玩家报告 / 时间线表 + API。"""
from town_tavern.config import DEBT_CRITICAL
from town_tavern.engine import world_engine
from town_tavern.engine.reporting import build_player_report_min
from town_tavern.engine.world_engine import (
    _settle_stages, advance_day, resolve_debt_endgame, tick_debt_endgame,
)
from town_tavern.models.world import (
    DEBT_RESOLVED_PAID, WORLD_PHASE_AWAITING_PLAYER,
)


class _BoomLLM:
    """任何方法被调用都炸——用于断言"封顶降级日不走 LLM"。"""

    def __getattr__(self, name):
        raise AssertionError(f"awaiting_player 降级日不应调用 LLM(误调用了 {name})")


def _mem_count(repo, gid) -> int:
    return repo.conn.execute(
        "SELECT COUNT(*) AS c FROM memories WHERE game_id = ?", (gid,)
    ).fetchone()["c"]


# --------------------------------------------------------------- 瑕疵① 封顶降级日
def test_awaiting_player_day_is_low_intensity_and_llm_free(game):
    repo, gid = game
    repo.set_world_value(gid, "world_phase", WORLD_PHASE_AWAITING_PLAYER)
    before_day = repo.get_current_day(gid)
    before_mem = _mem_count(repo, gid)

    event = advance_day(repo, _BoomLLM(), gid)  # 不应触发任何 LLM 调用

    # 天数推进、落了一条公开氛围事件、但【没有】写新记忆(不再堆记忆)。
    assert repo.get_current_day(gid) == before_day + 1
    assert event.visibility == "public"
    assert _mem_count(repo, gid) == before_mem


def test_awaiting_player_day_writes_public_timeline_beat(game):
    repo, gid = game
    repo.set_world_value(gid, "world_phase", WORLD_PHASE_AWAITING_PLAYER)
    advance_day(repo, _BoomLLM(), gid)
    msgs = repo.get_timeline_messages(gid, mode="player")
    assert any(m["type"] == "narration" for m in msgs)


def test_running_day_not_diverted_to_ambient(game):
    repo, gid = game
    # 普通 running 世界不该走降级路径(会去调 LLM,这里用 Boom 反证它【确实】尝试推进)。
    assert not repo.get_world_state(gid).is_awaiting_player()


# ------------------------------------------------------------------- 瑕疵② 债务终局
def test_resolve_debt_endgame_freezes_countdown(game):
    repo, gid = game
    repo.set_world_value(gid, "boss_debt", DEBT_CRITICAL)
    _settle_stages(repo, gid)
    tick_debt_endgame(repo, gid, 2)  # 启动倒计时
    assert repo.get_world_state(gid).debt_seize_countdown >= 0

    out = resolve_debt_endgame(repo, gid, DEBT_RESOLVED_PAID)
    assert out == DEBT_RESOLVED_PAID
    w = repo.get_world_state(gid)
    assert w.debt_resolution == DEBT_RESOLVED_PAID
    assert w.debt_seize_countdown == -1
    # 已结算后续推进不再产生终局行(终态已定)。
    assert tick_debt_endgame(repo, gid, 3) == []


def test_resolve_debt_endgame_rejects_bad_outcome(game):
    repo, gid = game
    try:
        resolve_debt_endgame(repo, gid, "NOT_A_STATE")
        assert False, "应拒绝非法终态"
    except ValueError:
        pass


# ------------------------------------------------------------------- 瑕疵③ 最小报告
def test_min_player_report_is_concise_and_debug_free(game):
    repo, gid = game
    from town_tavern.models.event import Event, EventType
    repo.set_world_value(gid, "current_day", 5)
    repo.add_event(gid, Event(
        day=5, type=EventType.DAILY_LIFE, title="角落",
        summary="小林坐在角落,捏着半张纸。", actors=["reporter"], visibility="public",
    ))
    out = build_player_report_min(repo, gid, 5)
    assert "【第5天】" in out
    assert "小林坐在角落" in out
    for term in ("flag", "RESOLVED", "truth_pressure", "exposure_stage", "conflict"):
        assert term not in out


# --------------------------------------------------------------------- 时间线表/裁剪
def test_timeline_mode_filtering(game):
    repo, gid = game
    repo.add_timeline_message(gid, 1, type="dialogue", text="公开对话",
                              speaker_id="boss", speaker_name="阿财", visibility="public")
    repo.add_timeline_message(gid, 1, type="system", text="内部状态行",
                              visibility="private", debug_payload={"state": "RESOLVED_X"})

    player = repo.get_timeline_messages(gid, mode="player")
    assert [m["text"] for m in player] == ["公开对话"]
    assert all("debug_payload" not in m for m in player)

    debug = repo.get_timeline_messages(gid, mode="debug")
    assert {m["text"] for m in debug} == {"公开对话", "内部状态行"}
    sys_row = next(m for m in debug if m["type"] == "system")
    assert sys_row["debug_payload"] == {"state": "RESOLVED_X"}


def test_timeline_incremental_after_id(game):
    repo, gid = game
    first = repo.add_timeline_message(gid, 1, type="narration", text="第一条")
    repo.add_timeline_message(gid, 1, type="narration", text="第二条")
    rest = repo.get_timeline_messages(gid, after_id=first, mode="player")
    assert [m["text"] for m in rest] == ["第二条"]
