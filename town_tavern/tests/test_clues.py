"""#3 player_known_clues:系统 flag 与玩家已知线索的隔离测试。"""
from town_tavern.engine.clue_engine import PLAYER_CLUE_SOURCES, record_player_clue
from town_tavern.engine.consequence import apply_consequence, get_flag
from town_tavern.models.consequence import Consequence


def test_player_clue_written_only_by_player_action(game):
    repo, gid = game
    clue = record_player_clue(
        repo, gid, clue_id="clue_along_recording_exists",
        title="阿龙手里可能有一段录音", source="偷听", day=5, certainty=60,
    )
    assert clue is not None
    assert repo.has_player_clue(gid, "clue_along_recording_exists")
    got = repo.get_player_clue(gid, "clue_along_recording_exists")
    assert got.certainty == 60 and got.source == "偷听" and got.day_found == 5


def test_illegal_source_is_rejected(game):
    repo, gid = game
    # 非玩家行为来源(模拟系统直灌)一律拒写
    out = record_player_clue(
        repo, gid, clue_id="clue_x", title="不该被系统写入", source="系统flag", day=1,
    )
    assert out is None
    assert not repo.has_player_clue(gid, "clue_x")


def test_system_flag_change_does_not_write_player_clue(game):
    repo, gid = game
    # 系统真实 flag 变化:录音被扣
    apply_consequence(
        repo, gid,
        Consequence(flags={"recording_seized": True}),
        day=6, source="test",
    )
    assert get_flag(repo, gid, "recording_seized") is True
    # 但玩家并不会因此自动"知道"——线索表保持为空
    assert repo.get_player_clues(gid) == []


def test_repeated_discovery_keeps_higher_certainty(game):
    repo, gid = game
    record_player_clue(repo, gid, clue_id="c1", title="t", source="询问", day=3, certainty=40)
    record_player_clue(repo, gid, clue_id="c1", title="t", source="观察", day=5, certainty=70)
    assert repo.get_player_clue(gid, "c1").certainty == 70
    # 再以更低把握重复发现,不应降级
    record_player_clue(repo, gid, clue_id="c1", title="t", source="询问", day=6, certainty=20)
    assert repo.get_player_clue(gid, "c1").certainty == 70


def test_allowed_sources_are_player_actions_only():
    assert PLAYER_CLUE_SOURCES == {"观察", "询问", "偷听", "交易", "被告知"}
