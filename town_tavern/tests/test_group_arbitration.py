"""多人讨论 PR-B:发言意图仲裁(choose_speaker / score_intent / personality_bias)纯函数单测。

不依赖 LLM / DB:直接构造 SpeakIntent + GroupDiscussionState 验证"谁这一句开口"。
"""
from town_tavern.config import GROUP_JUST_SPOKE_PENALTY
from town_tavern.engine.group_engine import (
    choose_speaker, personality_bias, score_intent,
)
from town_tavern.models.conversation import GroupDiscussionState, SpeakIntent
from town_tavern.models.npc import NPC


def _state(**kw) -> GroupDiscussionState:
    base = dict(group_id="g", participants=["a", "b", "c"])
    base.update(kw)
    return GroupDiscussionState(**base)


def _intent(npc_id, *, wants=True, urgency=50, intent="probe", target="",
            should_interrupt=False) -> SpeakIntent:
    return SpeakIntent(
        npc_id=npc_id, wants_to_speak=wants, urgency=urgency, intent=intent,
        target=target, should_interrupt=should_interrupt,
    )


def _npc(npc_id, mental_state="") -> NPC:
    return NPC(
        id=npc_id, name=npc_id, age=40, job="x", personality="", desire="",
        fear="", regret="", secret="", current_goal="", mental_state=mental_state,
    )


# ----------------------------------------------------------- 被点名 / 被质问
def test_named_person_gets_floor_over_equal_urgency():
    """上一句指向 b → 同等迫切下 b 更该接话。"""
    st = _state(last_speaker="a", last_target="b", last_intent="probe")
    intents = [_intent("b", urgency=40), _intent("c", urgency=40)]
    assert choose_speaker(intents, st) == "b"


def test_questioned_person_outranks_more_urgent_bystander():
    """b 被【逼问】(last_intent=press 且指向 b),即便 c 更急,也该 b 先回应。"""
    st = _state(last_speaker="a", last_target="b", last_intent="press")
    intents = [_intent("b", urgency=45), _intent("c", urgency=60)]
    assert choose_speaker(intents, st) == "b"


# ----------------------------------------------------------- 刚说过的人扣分
def test_just_spoke_penalty_yields_floor_to_other():
    """刚说完的 a 即便很急,也该让位给没怎么说话的 c(避免一人连说)。"""
    st = _state(last_speaker="a", last_target="", last_intent="probe")
    intents = [_intent("a", urgency=70), _intent("c", urgency=50)]
    # a 被扣 GROUP_JUST_SPOKE_PENALTY,降到 c 之下。
    assert GROUP_JUST_SPOKE_PENALTY > 20
    assert choose_speaker(intents, st) == "c"


# ----------------------------------------------------------- 沉默轮数
def test_silence_promotes_willing_speaker():
    """全场越僵,愿意开口者越该被推出来打破沉默(仍能选出人)。"""
    st = _state(silence_rounds=2)
    intents = [_intent("a", urgency=10), _intent("b", wants=False, urgency=0)]
    assert choose_speaker(intents, st) == "a"


def test_no_one_wants_to_speak_returns_none():
    """都不想说 → 返回 None(上层据此结束讨论)。"""
    st = _state(silence_rounds=1)
    intents = [
        _intent("a", wants=False), _intent("b", wants=False), _intent("c", wants=False),
    ]
    assert choose_speaker(intents, st) is None


def test_silent_or_observe_excluded_even_if_wants_true():
    """误把 silent/observe 标成 wants_to_speak 也不抢话筒;无其他候选则 None。"""
    st = _state()
    intents = [_intent("a", intent="silent"), _intent("b", intent="observe")]
    assert choose_speaker(intents, st) is None


# ----------------------------------------------------------- 触及秘密 / 偏置
def test_secret_touch_boosts_candidate():
    """话头戳到 b 卷入的秘密 → b 加权后压过更急的 c。"""
    st = _state()
    intents = [_intent("b", urgency=40), _intent("c", urgency=52)]
    ctx = {"b": {"touches_secret": True}}
    assert choose_speaker(intents, st, ctx) == "b"


def test_personality_bias_reporter_over_police():
    assert personality_bias(_npc("reporter")) > personality_bias(_npc("police"))


def test_withdrawn_mental_state_lowers_bias():
    assert personality_bias(_npc("reporter", "withdrawn")) < personality_bias(_npc("reporter"))


def test_score_named_adds_expected_boost():
    """量纲自检:被点名应在分数上体现为正向加权。"""
    st_named = _state(last_target="b", last_intent="probe")
    st_plain = _state()
    it = _intent("b", urgency=30)
    assert score_intent(it, st_named, {}) > score_intent(it, st_plain, {})
