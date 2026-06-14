"""多人讨论 PR-C:单句台词生成 + 讨论主循环 + 收场判定。

用假 LLM(按 prompt 里的 npc_id/speaker 路由)验证:
- 发言顺序由 choose_speaker 决定,**非固定 A/B/C 轮流**;
- 每轮对每个在场者都生成一次 SpeakIntent;
- 无人愿意开口时讨论收场(不产出任何台词);
- 台词黑名单(夹带系统名词的台词被作废)。
"""
import re

from town_tavern.config import GROUP_MAX_ROUNDS, GROUP_MAX_SILENCE_ROUNDS
from town_tavern.engine import group_engine
from town_tavern.models.conversation import (
    GroupDiscussionState, GroupUtterance, SpeakIntent,
)


class FakeLLM:
    """按 prompt 内容路由的假 LLM:意图按 npc_id 取,台词按 speaker 取。"""

    def __init__(self, intent_by_npc, utter_by_npc=None):
        self.intent_by_npc = intent_by_npc
        self.utter_by_npc = utter_by_npc or {}
        self.intent_calls = 0
        self.utter_calls = 0

    def _npc_in(self, text, key):
        m = re.search(rf'"{key}": "([a-z_]+)"', text)
        return m.group(1) if m else None

    def chat_json(self, system, user, schema, **kw):
        if schema is SpeakIntent:
            self.intent_calls += 1
            npc_id = self._npc_in(user, "npc_id")
            data = dict(self.intent_by_npc[npc_id])
            data.setdefault("npc_id", npc_id)
            return SpeakIntent(**data)
        if schema is GroupUtterance:
            self.utter_calls += 1
            npc_id = self._npc_in(user, "speaker")
            data = dict(self.utter_by_npc.get(npc_id, {}))
            data.setdefault("speaker", npc_id)
            data.setdefault("text", f"{npc_id}说话")
            data.setdefault("intent", "probe")
            return GroupUtterance(**data)
        raise AssertionError(f"unexpected schema {schema}")


PARTICIPANTS = ["reporter", "gambler", "police"]


def _dialogue_msgs(repo, gid, group_id):
    return [
        m for m in repo.get_timeline_messages(gid, mode="debug")
        if m["type"] == "dialogue" and m["group_id"] == group_id
    ]


def test_speaker_order_is_arbitrated_not_round_robin(game):
    """非固定轮流:reporter 最急 → 选中后被扣分让位 gambler,如此交替;police 一直插不上。"""
    repo, gid = game
    intents = {
        "reporter": {"wants_to_speak": True, "urgency": 80, "intent": "press"},
        "gambler": {"wants_to_speak": True, "urgency": 60, "intent": "probe"},
        "police": {"wants_to_speak": True, "urgency": 40, "intent": "probe"},
    }
    llm = FakeLLM(intents)
    state = group_engine.run_group_discussion(
        repo, llm, gid, day=2, participant_ids=PARTICIPANTS, topic="那盘带子"
    )
    assert state is not None
    msgs = _dialogue_msgs(repo, gid, state.group_id)
    speakers = [m["speaker_id"] for m in msgs]
    assert speakers == ["reporter", "gambler"] * (GROUP_MAX_ROUNDS // 2)
    assert "police" not in speakers          # 仲裁压制:police 始终插不上话
    assert speakers != PARTICIPANTS          # 绝非 A/B/C 顺序轮流
    # 每轮对 3 个在场者各生成一次意图。
    assert llm.intent_calls == 3 * GROUP_MAX_ROUNDS
    # 所有台词共享同一 group_id 与 participants。
    assert all(m["group_id"] == state.group_id for m in msgs)
    assert all(m["participants"] == PARTICIPANTS for m in msgs)


def test_discussion_ends_when_no_one_speaks(game):
    """都不想开口 → 连续冷场到阈值即收场,不产出任何台词。"""
    repo, gid = game
    intents = {p: {"wants_to_speak": False, "urgency": 0, "intent": "silent"}
               for p in PARTICIPANTS}
    llm = FakeLLM(intents)
    state = group_engine.run_group_discussion(
        repo, llm, gid, day=2, participant_ids=PARTICIPANTS
    )
    assert state is not None
    assert state.end_reason == "silence"
    assert state.silence_rounds >= GROUP_MAX_SILENCE_ROUNDS
    assert _dialogue_msgs(repo, gid, state.group_id) == []
    assert llm.utter_calls == 0


def test_too_few_present_returns_none(game):
    repo, gid = game
    assert group_engine.run_group_discussion(
        repo, llm=FakeLLM({}), game_id=gid, day=1, participant_ids=["reporter"]
    ) is None


def test_run_writes_opening_narration_with_group_id(game):
    repo, gid = game
    intents = {p: {"wants_to_speak": False, "intent": "silent"} for p in PARTICIPANTS}
    state = group_engine.run_group_discussion(
        repo, FakeLLM(intents), gid, day=2, participant_ids=PARTICIPANTS,
        location="后巷",
    )
    narrations = [
        m for m in repo.get_timeline_messages(gid, mode="player")
        if m["group_id"] == state.group_id and m["type"] == "narration"
    ]
    assert any("后巷" in m["text"] for m in narrations)
    assert all(m["participants"] == PARTICIPANTS for m in narrations)


# ----------------------------------------------------------- 台词黑名单
def test_sanitize_drops_forbidden_token():
    assert group_engine.sanitize_utterance("athou_truth_progress 涨了") == ""
    assert group_engine.contains_forbidden_token("快 flag_xxx 了")
    assert not group_engine.contains_forbidden_token("你那盘带子呢")


def test_sanitize_truncates_to_40():
    long = "字" * 60
    assert len(group_engine.sanitize_utterance(long)) == 40


def test_utterance_with_forbidden_token_is_dropped(game):
    """LLM 吐出夹带系统名词的台词 → 整句作废,该轮记为没说成。"""
    repo, gid = game
    intents = {"reporter": {"wants_to_speak": True, "urgency": 90, "intent": "press"},
               "gambler": {"wants_to_speak": False, "intent": "silent"},
               "police": {"wants_to_speak": False, "intent": "silent"}}
    utters = {"reporter": {"text": "我看 athou_truth_progress 都涨了", "intent": "press"}}
    llm = FakeLLM(intents, utters)
    state = group_engine.run_group_discussion(
        repo, llm, gid, day=2, participant_ids=PARTICIPANTS
    )
    assert _dialogue_msgs(repo, gid, state.group_id) == []


# ----------------------------------------------------------- should_end 纯函数
def test_should_end_on_max_rounds():
    st = GroupDiscussionState(group_id="g", participants=["a", "b"])
    st.rounds = GROUP_MAX_ROUNDS
    assert group_engine.should_end_discussion(st)
    assert st.end_reason == "max_rounds"


def test_should_end_on_heat():
    from town_tavern.config import GROUP_HEAT_END_THRESHOLD
    st = GroupDiscussionState(group_id="g", participants=["a", "b"])
    st.heat = GROUP_HEAT_END_THRESHOLD
    assert group_engine.should_end_discussion(st)
    assert st.end_reason == "too_heated"
