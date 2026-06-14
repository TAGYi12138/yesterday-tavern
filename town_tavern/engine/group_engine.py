"""多人讨论引擎(群聊子流程)。

设计同整个项目一脉相承:**LLM 负责想象,程序负责裁决**。
- 每一轮,在场的人各出一个 `SpeakIntent`(想不想说、多迫切、什么角度、对谁)——这是 LLM。
- `choose_speaker` 是【纯函数】:只看意图 + 现场状态 + 少量上下文(压力/性格偏置/是否被戳到
  痛处),算出"这一句轮到谁开口",便于离线单测(无需 LLM / DB)。
- 选中者再出一句具体台词(`GroupUtterance`)——这是 LLM。
- 何时收场由 `should_end_discussion`(纯函数)判定。

知识隔离 & 红线:喂给 LLM 的只有【共享现场】(在场都听得见的话)+ 它【自己的】记忆/关系,
绝不含任何系统真相/flag/状态机;讨论的后果统一走 `conversation_engine._apply_consequences`,
继承"绝不写 athou_truth_progress / player_known_clues"的红线。
"""
from typing import Dict, List, Optional

from ..config import (
    GROUP_HEAT_END_THRESHOLD, GROUP_JUST_SPOKE_PENALTY, GROUP_MAX_ROUNDS,
    GROUP_MAX_SILENCE_ROUNDS,
)
from ..llm import prompts
from ..llm.client import LLMClient
from ..models.conversation import (
    GroupDiscussionState, PRESSURING_INTENTS, SPEAK_INTENTS, SpeakIntent,
)
from ..models.npc import NPC
from ..storage.repository import Repository

# 仲裁权重(纯数值,集中在此便于调参/被单测引用)。
_NAMED_BOOST = 25        # 上一句点名/指向我 → 我更该接话
_QUESTIONED_BOOST = 18   # 且上一句是逼问/质问类 → 再加一层"被质问"压力
_SECRET_BOOST = 20       # 话头戳到我卷入的秘密/冲突
_SILENCE_BOOST = 8       # 全场每多沉默一轮,愿意开口者更易被选
_INTERRUPT_BOOST = 12    # 明确想抢话打断
_STRESS_WEIGHT = 0.15    # 压力越高越按捺不住

# 性格/角色偏置:有人天生爱接话(记者/赌徒),有人惜字(警察);心理失常态再叠加。
_ID_BIAS = {
    "reporter": 10,   # 记者:好奇、爱追问
    "gambler": 8,     # 赌徒:话痨、爱搅和
    "boss": 2,
    "sister": 0,
    "police": -6,     # 老陈:沉得住气、惜字如金
}
_MENTAL_BIAS = {
    "reckless": 15,            # 铤而走险:更冲动地开口
    "confession_ready": 12,    # 绷到临界:话憋不住
    "paranoid": 6,             # 草木皆兵:爱试探
    "withdrawn": -25,          # 身心俱疲:更想闭嘴
    "": 0,
}


def personality_bias(npc: NPC) -> int:
    """由角色身份 + 当前心理状态派生一个发言倾向偏置(纯函数,供仲裁加权)。"""
    return _ID_BIAS.get(npc.id, 0) + _MENTAL_BIAS.get(npc.mental_state, 0)


def normalize_intent(value: str) -> str:
    """把 LLM 给的意图归一到合法取值;不认识就退化为 observe(只看不说)。"""
    v = (value or "").strip().lower()
    return v if v in SPEAK_INTENTS else "observe"


# ---------------------------------------------------------------------------
# 仲裁:这一句轮到谁开口(纯函数)
# ---------------------------------------------------------------------------
def score_intent(intent: SpeakIntent, state: GroupDiscussionState, info: dict) -> float:
    """给一个发言意图打分(越高越该被选中这一句开口)。纯函数,便于离线单测。

    info 是该 NPC 的少量上下文:{"stress": int, "bias": int, "touches_secret": bool}。
    """
    score = float(intent.urgency)
    # 被点名/被质问:上一句指向我,我更该接话;若那句还是逼问/威胁,叠加"被质问"压力。
    if state.last_target and state.last_target == intent.npc_id:
        score += _NAMED_BOOST
        if normalize_intent(state.last_intent) in PRESSURING_INTENTS:
            score += _QUESTIONED_BOOST
    # 话头戳到我卷入的秘密/冲突。
    if info.get("touches_secret"):
        score += _SECRET_BOOST
    # 压力越高越按捺不住。
    score += info.get("stress", 0) * _STRESS_WEIGHT
    # 全场越僵,愿意开口者越该被推出来打破沉默。
    score += state.silence_rounds * _SILENCE_BOOST
    # 刚说完话的人立刻再抢话要扣分,避免一个人连说不停。
    if state.last_speaker and state.last_speaker == intent.npc_id:
        score -= GROUP_JUST_SPOKE_PENALTY
    # 明确想抢话打断,小幅加成。
    if intent.should_interrupt:
        score += _INTERRUPT_BOOST
    # 性格/角色偏置。
    score += info.get("bias", 0)
    return score


def choose_speaker(
    intents: List[Optional[SpeakIntent]],
    state: GroupDiscussionState,
    ctx: Optional[Dict[str, dict]] = None,
) -> Optional[str]:
    """从本轮各人的意图里,纯程序地裁决"这一句"轮到谁开口。

    - 只有 wants_to_speak 且意图不是纯 silent/observe(沉默/旁观)的人才进入候选。
    - 已离场者不参与。
    - 都不想说(候选为空)→ 返回 None,由上层据此结束讨论。
    ctx[npc_id] = {"stress","bias","touches_secret"};缺省视为 0/False。
    """
    ctx = ctx or {}
    best_id: Optional[str] = None
    best_score = float("-inf")
    for it in intents:
        if it is None or not it.wants_to_speak:
            continue
        if it.npc_id in state.left:
            continue
        if normalize_intent(it.intent) in ("silent", "observe"):
            # 想沉默/只旁观的,不抢这一句的话筒(即便误标了 wants_to_speak)。
            continue
        score = score_intent(it, state, ctx.get(it.npc_id, {}))
        # 同分时偏向"沉默更久没说话"的人(spoken_count 少者优先),让轮替更均衡。
        if score > best_score or (
            score == best_score and best_id is not None
            and state.spoken_count.get(it.npc_id, 0)
            < state.spoken_count.get(best_id, 0)
        ):
            best_score = score
            best_id = it.npc_id
    return best_id


# ---------------------------------------------------------------------------
# 意图生成(LLM):主线程备料 + 单次调用封装
# ---------------------------------------------------------------------------
def _intent_job(
    repo: Repository, game_id: str, npc: NPC, state: GroupDiscussionState,
    scene_text: str,
) -> tuple:
    """主线程读 DB 备料,返回一个 (system, user, SpeakIntent) 的并发 job。

    只注入共享现场 + 该 NPC 自己的记忆/关系/本人冲突感知(知识隔离),不含系统真相。
    """
    # 延迟导入,避免与 conversation_engine 的相互引用形成 import 期循环。
    from . import conflict_engine
    recent = repo.get_recent_memories(game_id, npc.id)
    longterm = repo.get_longterm_memories(game_id, npc.id)
    world = repo.get_world_state(game_id)
    others = _others_text_in_group(repo, game_id, npc.id, state)
    conflict_brief = conflict_engine.describe_conflicts_for_npc(repo, game_id, npc.id)
    system, user = prompts.build_group_speak_intent_prompt(
        npc, scene_text, others, recent, longterm, world,
        conflict_brief=conflict_brief,
    )
    return (system, user, SpeakIntent)


def _others_text_in_group(
    repo: Repository, game_id: str, self_id: str, state: GroupDiscussionState,
) -> str:
    """像 conversation_engine._others_text,但只列【这场讨论里在场的其他人】。"""
    lines = []
    for pid in state.present():
        if pid == self_id:
            continue
        n = repo.get_npc(game_id, pid)
        if n is None:
            continue
        rel = repo.get_relationship(game_id, self_id, pid)
        lines.append(
            f"- {n.id}={n.name}({n.job});你对他:"
            f"信任{rel.trust} 恐惧{rel.fear} 怨恨{rel.resentment} "
            f"好感{rel.affection} 怀疑{rel.suspicion}"
        )
    return "\n".join(lines)


def generate_speak_intent(
    repo: Repository, llm: LLMClient, game_id: str, npc: NPC,
    state: GroupDiscussionState,
) -> Optional[SpeakIntent]:
    """单独生成某 NPC 这一轮的发言意图(便于点测;批量请走 run_group_discussion 的并发)。"""
    name_of = {n.id: n.name for n in repo.get_all_npcs(game_id)}
    scene_text = prompts.group_scene_text(state, name_of)
    system, user, schema = _intent_job(repo, game_id, npc, state, scene_text)
    try:
        intent = llm.chat_json(system, user, schema)
    except Exception:
        return None
    intent.npc_id = npc.id  # 以程序为准,避免 LLM 把 npc_id 写错/写空
    intent.intent = normalize_intent(intent.intent)
    return intent
