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
import uuid
from typing import Dict, List, Optional

from ..config import (
    GROUP_DISCUSSION_COOLDOWN_DAYS, GROUP_DISCUSSION_ENABLED,
    GROUP_HEAT_END_THRESHOLD, GROUP_JUST_SPOKE_PENALTY, GROUP_MAX_ROUNDS,
    GROUP_MAX_SILENCE_ROUNDS, GROUP_PARTICIPANTS,
)
from ..llm import prompts
from ..llm.client import LLMClient
from ..models.conversation import (
    GroupDiscussionState, GroupUtterance, PRESSURING_INTENTS, SPEAK_INTENTS,
    SpeakIntent,
)
from ..models.event import EventConsequences, RelationshipDelta, StressDelta
from ..models.memory import MemoryType
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


# ---------------------------------------------------------------------------
# 台词黑名单:台词里绝不能出现系统名词/状态机/隐藏真相的痕迹
# ---------------------------------------------------------------------------
_FORBIDDEN_TOKENS = (
    "athou_truth_progress", "player_known_clues", "truth_pressure",
    "exposure_stage", "exposure_risk", "global_tension", "debt_stage",
    "world_phase", "flag_", "resolved_", "conflictstate", "_delta",
)


def contains_forbidden_token(text: str) -> bool:
    """台词是否夹带了系统名词/状态机/flag 等不该被角色说出的内部痕迹。"""
    low = (text or "").lower()
    return any(tok in low for tok in _FORBIDDEN_TOKENS)


def sanitize_utterance(text: str, max_len: int = 40) -> str:
    """收口一句台词:超长截断到 max_len;若夹带系统名词则整句作废(返回空串)。

    返回空串表示"这一句不可用",上层据此当作本轮没说成(计一次沉默),绝不外显脏台词。
    """
    t = (text or "").strip()
    if not t:
        return ""
    if contains_forbidden_token(t):
        return ""
    return t[:max_len]


# ---------------------------------------------------------------------------
# 单句台词生成(LLM)
# ---------------------------------------------------------------------------
def generate_utterance_from_intent(
    repo: Repository, llm: LLMClient, game_id: str, npc: NPC,
    intent: SpeakIntent, state: GroupDiscussionState,
) -> Optional[GroupUtterance]:
    """让被仲裁选中的 npc 依据其意图说出【一句】具体台词(≤40 字,过黑名单)。"""
    name_of = {n.id: n.name for n in repo.get_all_npcs(game_id)}
    scene_text = prompts.group_scene_text(state, name_of)
    target_name = name_of.get(intent.target, "")
    recent = repo.get_recent_memories(game_id, npc.id)
    longterm = repo.get_longterm_memories(game_id, npc.id)
    world = repo.get_world_state(game_id)
    system, user = prompts.build_group_utterance_prompt(
        npc, scene_text, intent, target_name, recent, longterm, world
    )
    try:
        utt = llm.chat_json(system, user, GroupUtterance)
    except Exception:
        return None
    utt.speaker = npc.id  # 以程序为准
    utt.intent = normalize_intent(utt.intent)
    utt.text = sanitize_utterance(utt.text)
    if not utt.text:
        return None
    return utt


# ---------------------------------------------------------------------------
# 收场判定(纯函数)
# ---------------------------------------------------------------------------
def should_end_discussion(state: GroupDiscussionState) -> bool:
    """这场讨论是否该收场(任一条件满足即收)。纯函数,便于单测。

    - 轮数到顶;或在场不足两人(都离场了);
    - 连续多轮无人愿意开口(冷场);或现场热度烧到上限(再吵无益,见好就收)。
    """
    if state.rounds >= GROUP_MAX_ROUNDS:
        if not state.end_reason:
            state.end_reason = "max_rounds"
        return True
    if len(state.present()) < 2:
        if not state.end_reason:
            state.end_reason = "too_few_present"
        return True
    if state.silence_rounds >= GROUP_MAX_SILENCE_ROUNDS:
        if not state.end_reason:
            state.end_reason = "silence"
        return True
    if state.heat >= GROUP_HEAT_END_THRESHOLD:
        if not state.end_reason:
            state.end_reason = "too_heated"
        return True
    return False


# 各意图对现场"热度"的贡献:逼问/威胁/抢话升温,求情/岔开降温。
_HEAT_DELTA = {
    "threaten": 22, "press": 16, "interrupt": 14, "deny": 10, "probe": 8,
    "appeal": -4, "deflect": -2, "observe": 0, "silent": 0, "leave": 0,
}


def _heat_delta(intent: str) -> int:
    return _HEAT_DELTA.get(normalize_intent(intent), 4)


def _secret_involved_ids(repo: Repository, game_id: str) -> set:
    """卷入"任一进行中冲突"的 NPC id 集合 —— 话头戳到他们更易接话(touches_secret)。"""
    ids: set = set()
    for c in repo.get_all_conflicts(game_id):
        if not c.is_resolved():
            ids.update(c.participants)
    return ids


def _gather_intents(llm: LLMClient, jobs: List[tuple]) -> List[Optional[SpeakIntent]]:
    """并发执行一批意图生成调用(复用 conversation_engine 的并发器,受 CONV_CONCURRENCY 控)。

    延迟导入 conversation_engine,避免与其形成 import 期循环依赖。
    """
    from .conversation_engine import _gather_json
    return _gather_json(llm, jobs)


# ---------------------------------------------------------------------------
# 讨论主循环
# ---------------------------------------------------------------------------
def run_group_discussion(
    repo: Repository,
    llm: LLMClient,
    game_id: str,
    day: int,
    participant_ids: List[str],
    topic: str = "",
    topic_owner: str = "",
    location: str = "吧台",
    on_progress=None,
    agg: Optional["EventConsequences"] = None,
) -> Optional[GroupDiscussionState]:
    """运行一场多人讨论,把逐句台词以同一 group_id 落 timeline,返回现场终态。

    流程(每轮):在场各人各出一个 SpeakIntent(LLM·并发)→ 程序仲裁谁开口
    → 选中者出一句台词(LLM)→ 落 timeline、更新现场。无人愿意开口/冷场/烧到顶即收场。
    发言顺序由仲裁决定,**非固定 A/B/C 轮流**。返回 None 表示在场不足两人、未开成。
    讨论收场后写【参与者三层个人记忆 + 旁观者片段传闻】并裁决后果(继承真相红线)。
    传入 agg 时,当日后果汇总累加进去(供调用方展示),否则内部自建一份丢弃。
    """
    npcs = {n.id: n for n in repo.get_all_npcs(game_id)}
    name_of = {nid: n.name for nid, n in npcs.items()}
    present = [
        pid for pid in participant_ids
        if pid in npcs and npcs[pid].is_present()
    ]
    if len(present) < 2:
        return None

    group_id = "grp-" + uuid.uuid4().hex[:8]
    state = GroupDiscussionState(
        group_id=group_id, participants=list(present), location=location,
        topic=topic, topic_owner=topic_owner,
    )

    def _p(msg: str) -> None:
        if on_progress is not None:
            on_progress(msg)

    # 开场旁白(带 group_id + participants):让前端即便只有一句也能渲染出讨论块头。
    present_names = "、".join(name_of[p] for p in present)
    repo.add_timeline_message(
        game_id, day, type="narration",
        text=f"{present_names}在{location}围拢到一处,气氛有些僵。",
        visibility="public", group_id=group_id, participants=list(present),
        tick=0,
    )
    _p(f"  · 一场多人对峙在{location}拉开:{present_names}")

    secret_ids = _secret_involved_ids(repo, game_id)

    while not should_end_discussion(state):
        state.rounds += 1
        present_ids = state.present()
        if len(present_ids) < 2:
            break
        scene_text = prompts.group_scene_text(state, name_of)
        jobs = [
            _intent_job(repo, game_id, npcs[pid], state, scene_text)
            for pid in present_ids
        ]
        results = _gather_intents(llm, jobs)
        intents: List[SpeakIntent] = []
        for pid, res in zip(present_ids, results):
            if res is None:
                continue
            res.npc_id = pid
            res.intent = normalize_intent(res.intent)
            intents.append(res)

        ctx = {
            it.npc_id: {
                "stress": npcs[it.npc_id].stress,
                "bias": personality_bias(npcs[it.npc_id]),
                "touches_secret": it.npc_id in secret_ids,
            }
            for it in intents
        }
        speaker_id = choose_speaker(intents, state, ctx)
        if speaker_id is None:
            # 没人愿意开口:记一次冷场,够久就收场。
            state.silence_rounds += 1
            continue

        chosen = next(it for it in intents if it.npc_id == speaker_id)
        if chosen.intent == "leave":
            state.left.append(speaker_id)
            repo.add_timeline_message(
                game_id, day, type="narration",
                text=f"{name_of[speaker_id]}没再搭话,起身离开了。",
                visibility="public", group_id=group_id,
                participants=list(state.participants), tick=state.rounds,
            )
            _p(f"    · {name_of[speaker_id]} 离场")
            continue

        utt = generate_utterance_from_intent(
            repo, llm, game_id, npcs[speaker_id], chosen, state
        )
        if utt is None:
            state.silence_rounds += 1
            continue

        target_id = utt.target if utt.target in npcs else ""
        repo.add_timeline_message(
            game_id, day, type="dialogue", text=utt.text,
            speaker_id=speaker_id, speaker_name=name_of[speaker_id],
            target_id=target_id or None,
            target_name=name_of.get(target_id) if target_id else None,
            visibility="public", group_id=group_id,
            participants=list(state.participants), tick=state.rounds,
            debug_payload={"intent": utt.intent, "tone": utt.tone},
        )
        if utt.visible_reaction:
            repo.add_timeline_message(
                game_id, day, type="narration",
                text=f"({name_of[speaker_id]}{utt.visible_reaction})",
                visibility="public", group_id=group_id,
                participants=list(state.participants), tick=state.rounds,
            )
        state.add_utterance(speaker_id, target_id, utt.text, utt.intent)
        state.heat = min(100, state.heat + _heat_delta(utt.intent))
        _p(f"    {name_of[speaker_id]}:{utt.text}")

    if not state.end_reason:
        state.end_reason = "max_rounds"

    # 收场:写参与者三层个人记忆 + 旁观者片段传闻,并裁决后果(继承真相红线)。
    apply_discussion_aftermath(repo, game_id, day, state, name_of, agg=agg)
    return state


# ---------------------------------------------------------------------------
# 讨论善后(PR-D):个人记忆(三层)+ 旁观者传闻(片段)+ 后果裁决(红线)
# ---------------------------------------------------------------------------
def _athou_keywords_hit(state: GroupDiscussionState) -> bool:
    """这场讨论是否触及"阿土失踪"主线(仅据明面话头/台词关键词,不读系统真相)。"""
    blob = state.topic + " " + " ".join(t for _, _, t, _ in state.full_transcript)
    return any(k in blob for k in ("阿土", "失踪", "带子", "录音", "那天晚上", "真相"))


def build_discussion_consequences(
    state: GroupDiscussionState, secret_ids: Optional[set] = None,
) -> "EventConsequences":
    """据全程实录【纯程序地】裁出后果:关系增量 + 压力 + (触及主线时)真相压力增量。

    施压/质问/威胁会让【被指向者】对发话者升起戒备;求情拉拢小幅增好感。现场越热,
    在场者压力越涨。若话头触及阿土主线,给出正向 athou_progress_delta —— 它在
    `_apply_consequences` 里【只转化为 truth_pressure】,绝不落地玩家真相进度(红线)。
    """
    from ..engine import event_engine
    cons = EventConsequences()
    # 关系:按 (被指向者 -> 发话者) 聚合各对抗/亲近增量。
    pair: Dict[tuple, Dict[str, int]] = {}
    for sp, tg, _text, intent in state.full_transcript:
        if not tg or tg == sp:
            continue
        d = pair.setdefault((tg, sp), {
            "trust": 0, "fear": 0, "resentment": 0, "affection": 0, "suspicion": 0,
        })
        if intent in PRESSURING_INTENTS:
            d["suspicion"] += 3
            d["resentment"] += 2
            if intent == "threaten":
                d["fear"] += 3
        elif intent == "appeal":
            d["affection"] += 2
            d["trust"] += 1
    for (frm, to), d in pair.items():
        cons.relationships.append(RelationshipDelta(**{
            "from": frm, "to": to,
            "trust": event_engine._clamp(d["trust"], -8, 8),
            "fear": event_engine._clamp(d["fear"], -8, 8),
            "resentment": event_engine._clamp(d["resentment"], -8, 8),
            "affection": event_engine._clamp(d["affection"], -8, 8),
            "suspicion": event_engine._clamp(d["suspicion"], -8, 8),
        }))
    # 压力:现场越热,在场者越紧绷(轻量,统一小幅)。
    stress_gain = min(8, state.heat // 20)
    if stress_gain:
        for pid in state.participants:
            cons.stress.append(StressDelta(npc=pid, delta=stress_gain))
    # 触及主线:给正向真相压力增量(红线:只进 truth_pressure,不进 athou_truth_progress)。
    if _athou_keywords_hit(state):
        cons.athou_progress_delta = 3
    return cons


def _participant_memory(
    state: GroupDiscussionState, viewer_id: str, name_of: dict,
) -> tuple:
    """构造某参与者的三层个人记忆 (observed, interpretation, confidence)。

    observed 是【这位参与者视角】的事实复述(自己说的话 vs 听见别人说的),因此各人不同;
    interpretation/confidence 是其个人解读(随现场热度变化),不含任何系统真相。
    """
    mine = [t for sp, _tg, t, _ in state.full_transcript if sp == viewer_id]
    heard = [
        (name_of.get(sp, sp), t)
        for sp, _tg, t, _ in state.full_transcript if sp != viewer_id
    ]
    parts = [f"在{state.location}的一场争执里"]
    parts.append("我说了:" + "；".join(mine) if mine else "我没怎么开口")
    if heard:
        parts.append("听见 " + "；".join(f"{nm}说「{t}」" for nm, t in heard[:3]))
    observed = "，".join(parts)
    if state.heat >= 60:
        interpretation = "这事没完,有人快绷不住了"
        confidence = 65
    elif state.heat >= 30:
        interpretation = "话里有话,有人在遮掩什么"
        confidence = 55
    else:
        interpretation = "场面还压得住,没谈出什么"
        confidence = 45
    return observed, interpretation, confidence


def apply_discussion_aftermath(
    repo: Repository, game_id: str, day: int, state: GroupDiscussionState,
    name_of: Optional[dict] = None, agg: Optional["EventConsequences"] = None,
) -> "EventConsequences":
    """讨论收场后的统一善后:个人记忆 + 旁观者传闻 + 后果裁决(继承红线)。返回当日后果汇总。"""
    from . import memory_engine
    from .conversation_engine import _apply_consequences
    if name_of is None:
        name_of = {n.id: n.name for n in repo.get_all_npcs(game_id)}
    if agg is None:
        agg = EventConsequences()

    # 没说成任何话(纯冷场)→ 不写记忆、不裁后果,免得凭空生成内容。
    if not state.full_transcript:
        return agg

    # 1) 参与者各写一条【三层】个人记忆(observed 各人不同)。
    for pid in state.participants:
        observed, interpretation, confidence = _participant_memory(state, pid, name_of)
        memory_engine.write_memory(
            repo, game_id, pid, day, content=observed,
            mtype=MemoryType.DIALOGUE, importance=58,
            related_npc=state.topic_owner or None,
            interpretation=interpretation, confidence=confidence,
        )

    # 2) 旁观者(在场但未参与讨论者)只得【片段传闻】:看见有人在争,听不清内容。
    present_names = "、".join(name_of.get(p, p) for p in state.participants)
    observer_ids = {
        n.id for n in repo.get_all_npcs(game_id)
        if n.is_present() and n.id not in state.participants
    }
    for nid in observer_ids:
        memory_engine.write_memory(
            repo, game_id, nid, day,
            content=f"我瞧见{present_names}在{state.location}围着低声争执,没听清在说什么",
            mtype=MemoryType.RUMOR, importance=28,
        )

    # 3) 后果裁决:走与对话同一个 _apply_consequences,继承"真相归玩家"红线
    #    (athou_progress_delta 只转 truth_pressure,绝不写 athou_truth_progress)。
    secret_ids = _secret_involved_ids(repo, game_id)
    cons = build_discussion_consequences(state, secret_ids)
    _apply_consequences(repo, game_id, cons, agg)
    return agg


# ---------------------------------------------------------------------------
# 触发(PR-E):何时该自发一场多人讨论
# ---------------------------------------------------------------------------
_CONFLICT_LABEL = {
    "deal_recording": "那盘录音带子的交易",
    "ledger": "账本那笔糊涂账",
}
# 记最近一次自发讨论发生的天数(存 world_state,用于冷却)。
_LAST_GROUP_DAY_KEY = "last_group_discussion_day"


def snapshot_conflict_resolution(repo: Repository, game_id: str) -> Dict[str, bool]:
    """记下当前各冲突"是否已落槌",供日终比对出【今天刚落槌】的冲突。"""
    return {c.id: c.is_resolved() for c in repo.get_all_conflicts(game_id)}


def maybe_trigger_group_discussion(
    repo: Repository,
    llm: LLMClient,
    game_id: str,
    day: int,
    pre_resolved: Dict[str, bool],
    agg: Optional["EventConsequences"] = None,
    on_progress=None,
) -> Optional[GroupDiscussionState]:
    """日终判断是否自发一场多人讨论;满足条件则运行并返回现场终态,否则 None。

    首版触发条件(任一):
      ① 有冲突【今天刚落槌】(pre_resolved 里它还没了结,现在了结了)——当事人凑一块复盘;
      ③ 债务濒临接管(debt_stage == "seizing")——阿财/讨债人/家里人当面摊牌。
    参与者取相关当事人,在场不足再就近补到 GROUP_PARTICIPANTS 人;不足两人则不开。
    带冷却:两场自发讨论至少间隔 GROUP_DISCUSSION_COOLDOWN_DAYS 天,避免债务长期 seizing 天天刷。
    """
    if not GROUP_DISCUSSION_ENABLED:
        return None
    last_day = int(repo.get_world_value(game_id, _LAST_GROUP_DAY_KEY) or -10 ** 9)
    if day - last_day < GROUP_DISCUSSION_COOLDOWN_DAYS:
        return None
    world = repo.get_world_state(game_id)
    seed: List[str] = []
    topic = ""
    topic_owner = ""

    for c in repo.get_all_conflicts(game_id):
        if c.is_resolved() and not pre_resolved.get(c.id, False):
            seed = list(c.participants)
            topic_owner = seed[0] if seed else ""
            topic = f"刚出了结果的{_CONFLICT_LABEL.get(c.kind, '那桩纠葛')}"
            break

    if not topic and world.debt_stage == "seizing":
        seed = ["boss", "gambler", "sister"]
        topic_owner = "boss"
        topic = "阿财快被逼着卖店的事"

    if not topic:
        return None

    present_all = [n.id for n in repo.get_all_npcs(game_id) if n.is_present()]
    chosen = [p for p in seed if p in present_all]
    for pid in present_all:           # 当事人不足时,就近拉在场的人凑够人数
        if len(chosen) >= GROUP_PARTICIPANTS:
            break
        if pid not in chosen:
            chosen.append(pid)
    chosen = chosen[:GROUP_PARTICIPANTS]
    if len(chosen) < 2:
        return None

    if on_progress is not None:
        on_progress(f"  · 一桩事把人聚到了一处:{topic}")
    state = run_group_discussion(
        repo, llm, game_id, day, chosen, topic=topic, topic_owner=topic_owner,
        on_progress=on_progress, agg=agg,
    )
    if state is not None:
        repo.set_world_value(game_id, _LAST_GROUP_DAY_KEY, day)   # 起冷却
    return state
