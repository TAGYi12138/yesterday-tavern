"""统一后果应用器 + B1 债务单源 + PR2 NPC 状态 的测试。"""
from town_tavern.engine.consequence import apply_consequence, get_flag
from town_tavern.models.consequence import (
    Consequence, MemorySpec, NpcStatusChange, RelationshipChange,
)


def test_b1_debt_single_source_syncs_boss_money(game):
    """B1:调债务后,阿财 NPC 的 money 恒等于 -boss_debt。"""
    repo, gid = game
    new = repo.add_boss_debt(gid, 168000)
    assert new == repo.get_world_state(gid).boss_debt
    assert repo.get_npc(gid, "boss").money == -new
    # 再降一些,仍恒等
    new2 = repo.add_boss_debt(gid, -68000)
    assert repo.get_npc(gid, "boss").money == -new2


def test_red_line_truth_progress_never_written(game):
    """红线:world_changes 里的 athou_truth_progress 必须被丢弃。"""
    repo, gid = game
    before = repo.get_world_state(gid).athou_truth_progress
    c = Consequence(world_changes={"athou_truth_progress": 99, "truth_pressure": 10})
    applied = apply_consequence(repo, gid, c, day=3)
    after = repo.get_world_state(gid)
    assert after.athou_truth_progress == before  # 未被改动
    assert after.truth_pressure == 10            # 同批的合法键正常生效
    assert any("拦截" in a for a in applied)


def test_flags_and_world_changes(game):
    repo, gid = game
    c = Consequence(
        flags={"recording_delivered": True},
        world_changes={"police_exposure_risk": 5, "global_tension": 8},
    )
    apply_consequence(repo, gid, c, day=2)
    assert get_flag(repo, gid, "recording_delivered") is True
    w = repo.get_world_state(gid)
    assert w.police_exposure_risk == 35  # 种子 30 + 5
    assert w.global_tension == 28        # 种子 20 + 8


def test_npc_status_away_and_expire(game):
    """PR2:away 让 NPC 退出在场池;到期后 expire 复位为 active。"""
    repo, gid = game
    c = Consequence(npc_status=[NpcStatusChange(npc_id="gambler", status="away", duration_days=3)])
    apply_consequence(repo, gid, c, day=4)
    g = repo.get_npc(gid, "gambler")
    assert g.status == "away" and g.status_until_day == 7
    assert "gambler" not in {n.id for n in repo.get_present_npcs(gid)}
    # 未到期不复位
    assert repo.expire_npc_statuses(gid, 6) == []
    assert repo.get_npc(gid, "gambler").status == "away"
    # 到期复位
    assert repo.expire_npc_statuses(gid, 7) == ["gambler"]
    assert repo.get_npc(gid, "gambler").status == "active"
    assert "gambler" in {n.id for n in repo.get_present_npcs(gid)}


def test_relationship_and_memory_isolation(game):
    repo, gid = game
    before = repo.get_relationship(gid, "police", "reporter").suspicion
    c = Consequence(
        relationship_changes=[RelationshipChange(from_npc="police", to_npc="reporter", suspicion=10)],
        memories=[MemorySpec(npc_id="reporter", content="只有记者自己知道的一条线索", importance=70)],
    )
    apply_consequence(repo, gid, c, day=5)
    assert repo.get_relationship(gid, "police", "reporter").suspicion == min(100, before + 10)
    # 记忆只写给 reporter 自己
    assert any("线索" in m.content for m in repo.get_memories_by_day(gid, "reporter", 5))
    assert repo.get_memories_by_day(gid, "boss", 5) == []
