"""进阶版多回合对话:回合预算分档 + 多回合驱动 + 知识隔离与红线保持。

- 普通寒暄对子保持 1 回合(即便对方 wants_to_continue,也不超预算);
- 高张力对子(关系对抗维度高 / 同属未结冲突)才你来我往,最多 TALK_MAX_TURNS_HIGH 回合;
- A 追问收口(continue_talking=False)或 B 不再续聊(wants_to_continue=False)任一即止;
- 整场对话只各记一条合并实录,relationship_delta 始终是 B→A(不代笔)。
"""
from town_tavern import config
from town_tavern.engine import conversation_engine as ce
from town_tavern.engine.relationship_engine import apply_relationship_deltas
from town_tavern.models.conflict import Conflict, ConflictState
from town_tavern.models.conversation import ConversationFollowup, ConversationReply
from town_tavern.models.event import EventConsequences, RelationshipDelta
from town_tavern.models.memory import MemoryType


def _set_suspicion(repo, gid, a, b, value):
    """把 a→b 的 suspicion 拉到指定值(先清零再加),用于构造高/低张力。"""
    apply_relationship_deltas(repo, gid, [RelationshipDelta(**{
        "from": a, "to": b, "suspicion": -100,
    })])
    if value:
        apply_relationship_deltas(repo, gid, [RelationshipDelta(**{
            "from": a, "to": b, "suspicion": value,
        })])


def _calm_pair(repo, gid, a, b):
    for x, y in ((a, b), (b, a)):
        apply_relationship_deltas(repo, gid, [RelationshipDelta(**{
            "from": x, "to": y, "suspicion": -100, "resentment": -100, "fear": -100,
        })])


class _FakeLLM:
    """按 schema 类型依次吐出预设响应,并记录被调用次数。"""

    def __init__(self, followups=None, replies=None):
        self._followups = list(followups or [])
        self._replies = list(replies or [])
        self.calls = []

    def chat_json(self, system, user, schema, temperature: float = 0.4):
        self.calls.append(schema.__name__)
        if schema is ConversationFollowup:
            return self._followups.pop(0)
        if schema is ConversationReply:
            return self._replies.pop(0)
        raise AssertionError(f"unexpected schema {schema}")


def _spec(asker_id, asker_name, target_id, target_name, utterance):
    return {
        "kind": "talk", "asker_id": asker_id, "asker_name": asker_name,
        "target_id": target_id, "target_name": target_name, "utterance": utterance,
        "job": None,
    }


def _reply(text, wants_to_continue):
    return ConversationReply(reply=text, wants_to_continue=wants_to_continue)


# --------------------------- 回合预算分档 ---------------------------

def test_budget_base_for_calm_pair(game):
    repo, gid = game
    _calm_pair(repo, gid, "boss", "sister")
    assert ce._talk_turn_budget(repo, gid, "boss", "sister") == config.TALK_MAX_TURNS_BASE


def test_budget_high_for_tense_pair(game):
    repo, gid = game
    _calm_pair(repo, gid, "police", "reporter")
    _set_suspicion(repo, gid, "police", "reporter", config.TALK_TENSION_THRESHOLD + 10)
    assert ce._talk_turn_budget(repo, gid, "police", "reporter") == config.TALK_MAX_TURNS_HIGH


def test_budget_high_for_conflict_pair(game):
    repo, gid = game
    _calm_pair(repo, gid, "gambler", "reporter")
    repo.upsert_conflict(gid, Conflict(
        id="deal_recording", kind="deal_recording",
        participants=["gambler", "reporter"], state=ConflictState.NEGOTIATING,
        age_in_state=0, max_stall_days=5, created_day=1,
    ))
    assert ce._talk_turn_budget(repo, gid, "gambler", "reporter") == config.TALK_MAX_TURNS_HIGH


# --------------------------- 多回合驱动 ---------------------------

def test_calm_pair_stays_single_turn_even_if_wants_to_continue(game):
    """普通对子:即便 B 表示还想继续,也不超 base 预算,且不发起任何 followup LLM 调用。"""
    repo, gid = game
    _calm_pair(repo, gid, "boss", "sister")
    llm = _FakeLLM()
    agg = EventConsequences()
    spec = _spec("boss", "阿牛", "sister", "淑芬", "最近还好吗？")
    line = ce._apply_talk(repo, llm, gid, 3, spec, _reply("挺好的。", wants_to_continue=True), agg, None)
    assert llm.calls == []  # base 预算=1,不进入多回合循环
    assert "聊了几个回合" not in (line or "")
    mems = repo.get_recent_memories(gid, "boss")
    assert any("我去找淑芬聊了一场" in m.content for m in mems)


def test_high_tension_runs_multi_turn_until_stop(game):
    """高张力对子:B 想继续 + A 追问 → 进行多回合,最终一方收口后合并写入实录。"""
    repo, gid = game
    _calm_pair(repo, gid, "police", "reporter")
    _set_suspicion(repo, gid, "police", "reporter", config.TALK_TENSION_THRESHOLD + 20)
    llm = _FakeLLM(
        followups=[ConversationFollowup(continue_talking=True, utterance="你到底拿到了什么？")],
        replies=[_reply("我什么都不知道。", wants_to_continue=False)],
    )
    agg = EventConsequences()
    spec = _spec("police", "老赵", "reporter", "小林", "听说你在查码头的事？")
    first = ConversationReply(
        reply="只是随便打听。",
        relationship_delta=RelationshipDelta(**{"from": "reporter", "to": "police", "suspicion": 5}),
        wants_to_continue=True,
    )
    line = ce._apply_talk(repo, llm, gid, 3, spec, first, agg, None)
    # 一次 A 追问(Followup)+ 一次 B 再回复(Reply)
    assert llm.calls == ["ConversationFollowup", "ConversationReply"]
    assert "聊了几个回合(共2轮)" in line
    # 合并实录:双方各一条,含追问内容
    rep_mem = repo.get_recent_memories(gid, "reporter")
    assert any("你到底拿到了什么" in m.content and m.type == MemoryType.DIALOGUE for m in rep_mem)


def test_followup_stop_ends_conversation(game):
    """A 听完第一句就收口(continue_talking=False)→ 不再生成 B 的二次回复。"""
    repo, gid = game
    _calm_pair(repo, gid, "police", "reporter")
    _set_suspicion(repo, gid, "police", "reporter", config.TALK_TENSION_THRESHOLD + 20)
    llm = _FakeLLM(followups=[ConversationFollowup(continue_talking=False, utterance="")])
    agg = EventConsequences()
    spec = _spec("police", "老赵", "reporter", "小林", "你在查什么？")
    line = ce._apply_talk(repo, llm, gid, 3, spec, _reply("没什么。", wants_to_continue=True), agg, None)
    assert llm.calls == ["ConversationFollowup"]  # 没有第二次 Reply
    assert "聊了几个回合" not in line


def test_humanize_ids_replaces_raw_ids_with_names():
    """旁白文本里残留的英文 id 必须被兜底替换成中文名。"""
    names = {"police": "老陈", "reporter": "小林", "gambler": "阿龙"}
    text = "阿财在police对面坐下,reporter将手指停在酒杯边缘,gambler凑近"
    out = ce._humanize_ids(text, names)
    assert "police" not in out and "reporter" not in out and "gambler" not in out
    assert "老陈" in out and "小林" in out and "阿龙" in out


def test_humanize_ids_noop_on_empty():
    assert ce._humanize_ids("", {"police": "老陈"}) == ""


def test_reply_delta_always_from_target_to_asker(game):
    """红线/不代笔:每个回合落地的关系增量必是 B→A,A 不替 B 写,也不反向。"""
    repo, gid = game
    _calm_pair(repo, gid, "police", "reporter")
    _set_suspicion(repo, gid, "police", "reporter", config.TALK_TENSION_THRESHOLD + 20)
    before = repo.get_relationship(gid, "reporter", "police").suspicion
    llm = _FakeLLM()
    agg = EventConsequences()
    spec = _spec("police", "老赵", "reporter", "小林", "老实交代。")
    first = ConversationReply(
        reply="我没什么好说的。",
        relationship_delta=RelationshipDelta(**{"from": "reporter", "to": "police", "suspicion": 6}),
        wants_to_continue=False,
    )
    ce._apply_talk(repo, llm, gid, 3, spec, first, agg, None)
    after = repo.get_relationship(gid, "reporter", "police").suspicion
    assert after == before + 6
    assert len(agg.relationships) == 1
    assert agg.relationships[0].from_npc == "reporter" and agg.relationships[0].to_npc == "police"
