"""多人讨论 PR-D:参与者三层个人记忆 + 旁观者片段传闻 + 后果裁决(真相红线回归)。"""
from town_tavern.engine import group_engine
from town_tavern.models.memory import MemoryType, parse_memory_content
from town_tavern.tests.test_group_loop import FakeLLM, PARTICIPANTS


def _day_memories(repo, gid, npc_id, day, mtype=None):
    out = []
    for m in repo.get_recent_memories(gid, npc_id) + repo.get_longterm_memories(gid, npc_id):
        if m.day == day and (mtype is None or m.type == mtype):
            out.append(m)
    return out


def _run(repo, gid, intents, utters=None, topic="", day=2):
    return group_engine.run_group_discussion(
        repo, FakeLLM(intents, utters), gid, day=day,
        participant_ids=PARTICIPANTS, topic=topic,
    )


# --------------------------------------------------- 参与者三层个人记忆(各不相同)
def test_each_participant_gets_distinct_three_layer_memory(game):
    repo, gid = game
    intents = {
        "reporter": {"wants_to_speak": True, "urgency": 80, "intent": "press",
                     "target": "gambler"},
        "gambler": {"wants_to_speak": True, "urgency": 60, "intent": "deny",
                    "target": "reporter"},
        "police": {"wants_to_speak": True, "urgency": 40, "intent": "probe"},
    }
    utters = {
        "reporter": {"text": "你那盘带子呢", "intent": "press", "target": "gambler"},
        "gambler": {"text": "我啥也不知道", "intent": "deny", "target": "reporter"},
    }
    state = _run(repo, gid, intents, utters)
    observed_by = {}
    for pid in PARTICIPANTS:
        mems = _day_memories(repo, gid, pid, 2, MemoryType.DIALOGUE)
        assert len(mems) == 1, f"{pid} 应恰好写 1 条讨论个人记忆"
        parsed = parse_memory_content(mems[0].content)
        # 三层齐备:观察 + 推测 + 把握度。
        assert parsed["observed"]
        assert parsed["interpretation"]
        assert parsed["confidence"] is not None
        observed_by[pid] = parsed["observed"]
    # 各人 observed 互不相同(视角不同:自己说的 vs 听见的)。
    assert len(set(observed_by.values())) == len(PARTICIPANTS)
    # 开口者的 observed 含"我说了";没开口的 police 含"我没怎么开口"。
    assert "我说了" in observed_by["reporter"]
    assert "我没怎么开口" in observed_by["police"]


# --------------------------------------------------- 旁观者只得片段传闻(无实录)
def test_observers_get_rumor_fragment_without_transcript(game):
    repo, gid = game
    intents = {
        "reporter": {"wants_to_speak": True, "urgency": 80, "intent": "press"},
        "gambler": {"wants_to_speak": True, "urgency": 60, "intent": "probe"},
        "police": {"wants_to_speak": False, "intent": "silent"},
    }
    utters = {
        "reporter": {"text": "你那盘带子到底在哪", "intent": "press"},
        "gambler": {"text": "你少血口喷人", "intent": "probe"},
    }
    _run(repo, gid, intents, utters)
    # 旁观者:在场但不在 participants(默认种子里 boss、sister 也在场)。
    observers = [
        n.id for n in repo.get_all_npcs(gid)
        if n.is_present() and n.id not in PARTICIPANTS
    ]
    assert observers, "应存在至少一名旁观者"
    for nid in observers:
        rumors = _day_memories(repo, gid, nid, 2, MemoryType.RUMOR)
        assert len(rumors) == 1
        txt = rumors[0].content
        # 片段:看见有人争执,但【不含】任何台词原文。
        assert "你那盘带子到底在哪" not in txt
        assert "你少血口喷人" not in txt
        assert "围着" in txt or "争执" in txt


# --------------------------------------------------- 红线:不写玩家真相进度
def test_redline_athou_progress_unchanged_but_pressure_rises(game):
    repo, gid = game
    before = repo.get_world_state(gid)
    clues_before = len(repo.get_player_clues(gid))
    intents = {
        "reporter": {"wants_to_speak": True, "urgency": 90, "intent": "press"},
        "gambler": {"wants_to_speak": False, "intent": "silent"},
        "police": {"wants_to_speak": False, "intent": "silent"},
    }
    utters = {"reporter": {"text": "阿土那天晚上到底去哪了", "intent": "press"}}
    state = _run(repo, gid, intents, utters, topic="阿土失踪那盘带子")
    assert group_engine._athou_keywords_hit(state)
    after = repo.get_world_state(gid)
    # 红线:玩家真相进度【绝不】因 NPC 自跑而变。
    assert after.athou_truth_progress == before.athou_truth_progress
    # 正向真相压力则应上升(局势推进,等待玩家)。
    assert after.truth_pressure > before.truth_pressure
    assert len(repo.get_player_clues(gid)) == clues_before  # 不擅自给玩家加线索


# --------------------------------------------------- 后果:施压抬升被指向者戒备
def test_pressuring_raises_target_suspicion(game):
    repo, gid = game
    base = repo.get_relationship(gid, "gambler", "reporter").suspicion
    intents = {
        "reporter": {"wants_to_speak": True, "urgency": 90, "intent": "press",
                     "target": "gambler"},
        "gambler": {"wants_to_speak": False, "intent": "silent"},
        "police": {"wants_to_speak": False, "intent": "silent"},
    }
    utters = {"reporter": {"text": "你那盘带子呢", "intent": "press", "target": "gambler"}}
    _run(repo, gid, intents, utters, topic="闲聊")
    after = repo.get_relationship(gid, "gambler", "reporter").suspicion
    assert after > base
