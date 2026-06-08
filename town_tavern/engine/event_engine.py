"""事件引擎(动作语法版)。

职责:
1. 用"动作语法"组合出今日核心动作:谁(actor)对谁(target)做了什么(verb)。
   actor 由意图风险加权选出,target 取意图对象或推断张力最大者,
   verb 由角色倾向 + 关系张力加权决定。
2. 让 LLM 在该核心动作框架内填充具体事件经过(结构化输出)。
3. 可见性/分类标签由程序按动作裁决,返回 EventDraft 供 world_engine 落地。

动作词表(ActionVerb)与合法角色均固定,组合空间远大于旧的 6 种模板,
涌现更强,但仍由程序裁决、不致"剧情每天乱飞"。
"""
import random
from typing import Callable, List, Optional

from ..config import DEBT_DANGER, DEBT_WARN, EXPOSURE_DANGER, EXPOSURE_WATCH
from ..llm import prompts
from ..llm.client import LLMClient
from ..models.action import (
    ActionResolutionDraft, NPCIntention, ResolvedAction,
)
from ..models.event import (
    ActionVerb, EventConsequences, EventDraft, EventType, RelationshipDelta,
)
from ..models.world import WorldState
from ..storage.repository import Repository

# 单个 NPC 小动作的后果裁剪幅度(比焦点事件更克制)
# PR7 去保守化:适度放宽关系/压力单步幅度,让人物关系在多日里"动得起来",
# 而不是被钉在初值附近(每步仍受 clamp,长期仍由 RELATION_MIN/MAX 兜底)。
_REL_CLAMP = 12
_STRESS_CLAMP = 12
_ATHOU_ACTION_CLAMP = 5

# ---------------------------------------------------------------------------
# 动作语法(action grammar):用 (actor, verb, target) 组合代替 6 种固定模板
# ---------------------------------------------------------------------------
# 每个动作的中文释义,用于喂给"事件编剧"
VERB_HINT = {
    ActionVerb.CONFRONT: "当面对峙、质问",
    ActionVerb.THREATEN: "威胁、施压",
    ActionVerb.BRIBE: "塞钱收买、花钱安抚",
    ActionVerb.PERSUADE: "劝说、拉拢",
    ActionVerb.INVESTIGATE: "暗中调查、打探",
    ActionVerb.CONCEAL: "掩盖、销毁线索",
    ActionVerb.CONFIDE: "私下倾诉、开口求助",
    ActionVerb.DEAL: "谈交易、讲条件",
    ActionVerb.AVOID: "回避、疏远",
    ActionVerb.EXPOSE: "揭露、摊牌",
}

# 动作 → 默认可见性:当众/公开的动作 public,私下密谋的 private
VERB_VISIBILITY = {
    ActionVerb.CONFRONT: "public",
    ActionVerb.PERSUADE: "public",
    ActionVerb.AVOID: "public",
    ActionVerb.EXPOSE: "public",
    ActionVerb.THREATEN: "private",
    ActionVerb.BRIBE: "private",
    ActionVerb.INVESTIGATE: "private",
    ActionVerb.CONCEAL: "private",
    ActionVerb.CONFIDE: "private",
    ActionVerb.DEAL: "private",
}

# 动作 → 最接近的旧事件类型(仅作分类标签/降权用,保持存储兼容)
VERB_TO_TYPE = {
    ActionVerb.CONFRONT: EventType.POLICE_WARNING,
    ActionVerb.THREATEN: EventType.GAMBLER_BLACKMAIL,
    ActionVerb.BRIBE: EventType.GAMBLER_BLACKMAIL,
    ActionVerb.PERSUADE: EventType.DEBT_PRESSURE,
    ActionVerb.INVESTIGATE: EventType.REPORTER_INVESTIGATION,
    ActionVerb.CONCEAL: EventType.ATHOU_CLUE,
    ActionVerb.CONFIDE: EventType.SISTER_SUSPICION,
    ActionVerb.DEAL: EventType.GAMBLER_BLACKMAIL,
    ActionVerb.AVOID: EventType.DEBT_PRESSURE,
    ActionVerb.EXPOSE: EventType.ATHOU_CLUE,
}

# 各角色的动作倾向(让动作选择符合人设)
_ROLE_VERB_BIAS = {
    "police": [ActionVerb.THREATEN, ActionVerb.CONCEAL, ActionVerb.CONFRONT],
    "reporter": [ActionVerb.INVESTIGATE, ActionVerb.EXPOSE, ActionVerb.CONFRONT],
    "gambler": [ActionVerb.DEAL, ActionVerb.THREATEN, ActionVerb.BRIBE],
    "sister": [ActionVerb.CONFRONT, ActionVerb.INVESTIGATE, ActionVerb.CONFIDE],
    "boss": [ActionVerb.AVOID, ActionVerb.CONFIDE, ActionVerb.PERSUADE, ActionVerb.BRIBE],
}


def _infer_target(repo: Repository, game_id: str, actor_id: str) -> str:
    """当意图未指明对象时,挑出与 actor 张力最大(怨恨+怀疑+恐惧最高)的 NPC。"""
    best, best_score = "", -1
    for other in repo.get_all_npcs(game_id):
        if other.id == actor_id:
            continue
        rel = repo.get_relationship(game_id, actor_id, other.id)
        score = rel.resentment + rel.suspicion + rel.fear
        if score > best_score:
            best, best_score = other.id, score
    return best


def _choose_verb(
    repo: Repository, game_id: str, actor_id: str, target_id: str,
    recent_types: List[EventType], world: WorldState,
) -> ActionVerb:
    """根据角色倾向 + actor 对 target 的关系张力(+ 世界危险态势),加权挑一个动作。"""
    weights = {v: 1.0 for v in ActionVerb}

    # 角色人设倾向
    for v in _ROLE_VERB_BIAS.get(actor_id, []):
        weights[v] += 1.2

    # 关系张力决定动作色彩
    if target_id and target_id != "player":
        rel = repo.get_relationship(game_id, actor_id, target_id)
        if rel.resentment >= 40:
            weights[ActionVerb.CONFRONT] += 1.0
            weights[ActionVerb.THREATEN] += 1.0
            weights[ActionVerb.EXPOSE] += 0.8
        if rel.fear >= 40:
            weights[ActionVerb.AVOID] += 1.0
            weights[ActionVerb.BRIBE] += 0.8
            weights[ActionVerb.CONCEAL] += 0.8
            weights[ActionVerb.CONFIDE] += 0.5
        if rel.suspicion >= 40:
            weights[ActionVerb.INVESTIGATE] += 1.2
        if rel.trust >= 40 or rel.affection >= 40:
            weights[ActionVerb.CONFIDE] += 1.0
            weights[ActionVerb.PERSUADE] += 0.8

    # 世界态势联动:曝光风险越高,老陈越倾向掩盖/灭口;债务越高,阿财越孤注
    if actor_id == "police" and world.police_exposure_risk >= EXPOSURE_WATCH:
        weights[ActionVerb.CONCEAL] += 1.2
        weights[ActionVerb.THREATEN] += 1.0
    if actor_id == "boss" and world.boss_debt >= DEBT_WARN:
        weights[ActionVerb.CONFIDE] += 0.8
        weights[ActionVerb.PERSUADE] += 0.6
        weights[ActionVerb.DEAL] += 0.6

    # 最近发生过的类型降权(经 verb→type 映射),减少连续同类
    if recent_types:
        for v in ActionVerb:
            if VERB_TO_TYPE[v] in recent_types:
                weights[v] *= 0.5

    verbs = list(weights.keys())
    return random.choices(verbs, weights=[weights[v] for v in verbs], k=1)[0]


def compose_daily_action(
    repo: Repository, game_id: str, intentions: List[NPCIntention],
    recent_types: List[EventType], world: WorldState,
):
    """组合出今天的核心动作 (actor_id, verb, target_id)。

    actor 由意图风险加权选出(并受世界危险态势偏置);target 取该意图对象
    (校验合法),否则推断张力最大者;verb 由角色倾向 + 关系张力决定。
    """
    npcs = repo.get_all_npcs(game_id)
    valid_ids = {n.id for n in npcs}

    if intentions:
        actors = [it.npc_id for it in intentions]
        weights = [float(max(1, it.risk_level)) for it in intentions]
        # 危险态势偏置:债务高→阿财更易成为风暴中心;曝光高→老陈更活跃
        for i, aid in enumerate(actors):
            if aid == "boss" and world.boss_debt >= DEBT_DANGER:
                weights[i] *= 1.8
            elif aid == "boss" and world.boss_debt >= DEBT_WARN:
                weights[i] *= 1.4
            if aid == "police" and world.police_exposure_risk >= EXPOSURE_DANGER:
                weights[i] *= 1.8
            elif aid == "police" and world.police_exposure_risk >= EXPOSURE_WATCH:
                weights[i] *= 1.4
        actor_id = random.choices(actors, weights=weights, k=1)[0]
        it = next((x for x in intentions if x.npc_id == actor_id), None)
        target = it.target if it else ""
    else:
        actor_id = random.choice([n.id for n in npcs])
        target = ""

    # 目标校验:非法/指向自己/指向 player(自主演化时玩家不在场)→ 改为推断
    if target not in valid_ids or target == actor_id:
        target = _infer_target(repo, game_id, actor_id)

    verb = _choose_verb(repo, game_id, actor_id, target, recent_types, world)
    return actor_id, verb, target


def compute_exposure_delta(actor_id: str, verb: ActionVerb, target_id: str) -> int:
    """按动作语法程序化裁决一条事件对"老陈曝光风险"的影响。

    挖掘/揭露抬高风险;老陈本人的掩盖/威胁/收买压低风险。由程序裁决,不信任 LLM。
    """
    delta = 0
    if verb == ActionVerb.EXPOSE:
        delta += 15
    elif verb == ActionVerb.INVESTIGATE:
        delta += 8
    if verb == ActionVerb.CONFRONT and "police" in (actor_id, target_id):
        delta += 5
    if actor_id == "reporter":
        delta += 3  # 记者总在往真相上凑
    if actor_id == "police" and verb in (
        ActionVerb.CONCEAL, ActionVerb.THREATEN, ActionVerb.BRIBE
    ):
        delta -= 10  # 老陈灭口/掩盖,压低风险
    return max(-15, min(20, delta))


# ---------------------------------------------------------------------------
# 群像行动层:每个 NPC 各把意图落成一次具体行动(含小后果),再综合成焦点事件
# ---------------------------------------------------------------------------
def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _clamp_action_consequences(
    cons: EventConsequences, actor_id: str, valid_ids: set
) -> EventConsequences:
    """裁剪单个 NPC 小动作的后果:幅度收窄、过滤非法 id,避免 LLM 一次性放大数值。"""
    rels: List[RelationshipDelta] = []
    for r in cons.relationships:
        if r.from_npc not in valid_ids and r.from_npc != "player":
            continue
        if r.to_npc not in valid_ids and r.to_npc != "player":
            continue
        r.trust = _clamp(r.trust, -_REL_CLAMP, _REL_CLAMP)
        r.fear = _clamp(r.fear, -_REL_CLAMP, _REL_CLAMP)
        r.resentment = _clamp(r.resentment, -_REL_CLAMP, _REL_CLAMP)
        r.affection = _clamp(r.affection, -_REL_CLAMP, _REL_CLAMP)
        r.suspicion = _clamp(r.suspicion, -_REL_CLAMP, _REL_CLAMP)
        rels.append(r)
    stress = []
    for s in cons.stress:
        if s.npc not in valid_ids:
            continue
        s.delta = _clamp(s.delta, -_STRESS_CLAMP, _STRESS_CLAMP)
        stress.append(s)
    cons.relationships = rels
    cons.stress = stress
    cons.athou_progress_delta = _clamp(
        cons.athou_progress_delta, -_ATHOU_ACTION_CLAMP, _ATHOU_ACTION_CLAMP
    )
    return cons


def _action_weight(resolved: ResolvedAction, risk_level: int) -> float:
    """计算一次行动的"重要度",用于挑选当日焦点(头条)。

    综合:意图风险 + 关系/压力波动量级 + 曝光增量(加权) + 主线进度(加权) +
    公开动作加成(公开冲突更像头条)。
    """
    c = resolved.consequences
    rel_mag = sum(
        abs(r.trust) + abs(r.fear) + abs(r.resentment) + abs(r.affection) + abs(r.suspicion)
        for r in c.relationships
    )
    stress_mag = sum(abs(s.delta) for s in c.stress)
    weight = float(risk_level)
    weight += rel_mag + stress_mag
    weight += abs(c.exposure_delta) * 2.0
    weight += abs(c.athou_progress_delta) * 3.0
    if resolved.visibility == "public":
        weight += 5.0
    return weight


def resolve_npc_action(
    repo: Repository,
    llm: LLMClient,
    game_id: str,
    intention: NPCIntention,
    world: WorldState,
    recent_types: List[EventType],
) -> ResolvedAction:
    """把单个 NPC 的今日意图落成一次具体行动(动作框架程序裁决 + LLM 填经过与小后果)。

    actor 即该意图的 npc 本人;target 取意图对象(校验合法)否则推断张力最大者;
    verb 由角色倾向 + 关系张力决定;narration 与小后果由 LLM 产出后程序裁剪。
    """
    npc = repo.get_npc(game_id, intention.npc_id)
    valid_ids = {n.id for n in repo.get_all_npcs(game_id)}

    actor_id = intention.npc_id
    target_id = intention.target
    # 目标校验:非法 / 指向自己 / 指向 player(自主演化玩家不在场)→ 推断张力最大者
    if target_id not in valid_ids or target_id == actor_id:
        target_id = _infer_target(repo, game_id, actor_id)

    verb = _choose_verb(repo, game_id, actor_id, target_id, recent_types, world)

    npc_names = {n.id: n.name for n in repo.get_all_npcs(game_id)}
    target_name = npc_names.get(target_id, target_id)

    system, user = prompts.build_action_resolution_prompt(
        npc, verb, VERB_HINT[verb], target_id, target_name,
        intention.intention, world, npc_names,
    )
    draft = llm.chat_json(system, user, ActionResolutionDraft)

    cons = _clamp_action_consequences(draft.consequences, actor_id, valid_ids)
    # 曝光增量由程序裁决,不信任 LLM
    cons.exposure_delta = compute_exposure_delta(actor_id, verb, target_id)

    resolved = ResolvedAction(
        actor_id=actor_id,
        verb=verb,
        target_id=target_id,
        narration=draft.narration,
        consequences=cons,
        visibility=VERB_VISIBILITY[verb],
    )
    resolved.weight = _action_weight(resolved, intention.risk_level)
    return resolved


def resolve_all_actions(
    repo: Repository,
    llm: LLMClient,
    game_id: str,
    intentions: List[NPCIntention],
    world: WorldState,
    on_progress: Optional[Callable[[str], None]] = None,
) -> List[ResolvedAction]:
    """逐个 NPC 把意图落成具体行动(每人一次 LLM 调用)。单人失败不阻断整体。"""
    # 最近两天事件类型,用于动作去重降权
    recent_types: List[EventType] = []
    for d in (world.current_day, world.current_day - 1):
        ev = repo.get_event_by_day(game_id, d)
        if ev:
            recent_types.append(ev.type)

    npc_names = {n.id: n.name for n in repo.get_all_npcs(game_id)}
    resolved_list: List[ResolvedAction] = []
    for it in intentions:
        actor_name = npc_names.get(it.npc_id, it.npc_id)
        try:
            resolved = resolve_npc_action(repo, llm, game_id, it, world, recent_types)
        except Exception:
            if on_progress is not None:
                on_progress(f"  · {actor_name} 今日按兵不动(行动解析失败,跳过)")
            continue
        resolved_list.append(resolved)
        if on_progress is not None:
            target_name = npc_names.get(resolved.target_id, resolved.target_id) or "—"
            on_progress(
                f"  · {actor_name} {VERB_HINT[resolved.verb]} {target_name}:{resolved.narration}"
            )
    return resolved_list


def aggregate_consequences(resolved_list: List[ResolvedAction]) -> EventConsequences:
    """把当日所有行动的小后果汇总成一份后果集合(用于一次性落地与日志展示)。"""
    agg = EventConsequences()
    for r in resolved_list:
        c = r.consequences
        agg.relationships.extend(c.relationships)
        agg.stress.extend(c.stress)
        agg.flags.update(c.flags)
        agg.athou_progress_delta += c.athou_progress_delta
        agg.exposure_delta += c.exposure_delta
    return agg


def compose_focal_event(
    repo: Repository,
    llm: LLMClient,
    game_id: str,
    resolved_list: List[ResolvedAction],
    world: WorldState,
    on_progress: Optional[Callable[[str], None]] = None,
) -> EventDraft:
    """综合当日众人行动,挑出"今日头条"并由 LLM 编织成当日焦点事件。

    后果数值已在各行动层结算,本焦点事件只承载叙事(consequences 由调用方
    用 aggregate_consequences 汇总后回填,供日志/时间线展示,不再二次应用)。
    """
    npc_names = {n.id: n.name for n in repo.get_all_npcs(game_id)}

    # 挑权重最高者作为今日头条
    headline = max(resolved_list, key=lambda r: r.weight)
    headline_actor = npc_names.get(headline.actor_id, headline.actor_id)
    headline_target = npc_names.get(headline.target_id, headline.target_id) or "—"

    if on_progress is not None:
        on_progress(
            f"【今日头条】{headline_actor} 对 {headline_target} "
            f"{VERB_HINT[headline.verb]}(共 {len(resolved_list)} 人行动,以此为焦点)"
        )

    # 各人行动渲染成清单,供 LLM 编织
    lines = []
    for r in resolved_list:
        actor = npc_names.get(r.actor_id, r.actor_id)
        tgt = npc_names.get(r.target_id, r.target_id) or "—"
        lines.append(f"- {actor}({VERB_HINT[r.verb]} {tgt}):{r.narration}")
    actions_text = "\n".join(lines) if lines else "(今天大家都没什么动静)"

    system, user = prompts.build_focal_event_prompt(
        headline_actor, VERB_HINT[headline.verb], headline_target,
        actions_text, world, npc_names,
    )
    draft = llm.chat_json(system, user, EventDraft)
    # 分类标签与可见性按头条动作裁决;焦点事件本身不再携带数值后果(已分散结算)
    draft.type = VERB_TO_TYPE[headline.verb]
    draft.visibility = headline.visibility
    draft.consequences = EventConsequences()
    # 确保头条双方在 actors 内,且过滤非法 id
    valid = set(npc_names.keys())
    draft.actors = [a for a in draft.actors if a in valid]
    for nid in (headline.actor_id, headline.target_id):
        if nid in valid and nid not in draft.actors:
            draft.actors.append(nid)
    return draft


def generate_daily_event(
    repo: Repository,
    llm: LLMClient,
    game_id: str,
    intentions: List[NPCIntention],
    on_progress: Optional[Callable[[str], None]] = None,
) -> EventDraft:
    """整合意图 → 组合核心动作 (actor,verb,target) → LLM 据此生成事件细节。

    on_progress 可选回调:选出今日核心动作后,把"谁对谁做了什么"透出去,
    让上层(守护/CLI)显式展示这一天是由哪个动作推动成事件的。
    """
    world = repo.get_world_state(game_id)

    # 取最近两天已发生的事件类型(由近到远),用于降权去重
    recent_types: List[EventType] = []
    for d in (world.current_day, world.current_day - 1):
        ev = repo.get_event_by_day(game_id, d)
        if ev:
            recent_types.append(ev.type)

    # 动作语法:谁(actor)对谁(target)做了什么(verb)
    actor_id, verb, target_id = compose_daily_action(
        repo, game_id, intentions, recent_types, world
    )

    npc_names = {n.id: n.name for n in repo.get_all_npcs(game_id)}
    actor_name = npc_names.get(actor_id, actor_id)
    target_name = npc_names.get(target_id, target_id)

    if on_progress is not None:
        on_progress(
            f"【今日核心动作】{actor_name} 对 {target_name} "
            f"{VERB_HINT[verb]}(由 {actor_name} 的意图推动)"
        )

    # 渲染意图文本
    lines = [
        f"- {it.npc_id}: {it.intention}(目标:{it.target or '无'},风险{it.risk_level}) 理由:{it.reason}"
        for it in intentions
    ]
    intentions_text = "\n".join(lines) if lines else "(无明显意图)"

    system, user = prompts.build_event_prompt(
        actor_id, actor_name, verb, VERB_HINT[verb],
        target_id, target_name, intentions_text, world, npc_names,
    )
    draft = llm.chat_json(system, user, EventDraft)
    # 类型(分类标签)、可见性、曝光增量均由程序按动作裁决,不信任 LLM
    draft.type = VERB_TO_TYPE[verb]
    draft.visibility = VERB_VISIBILITY[verb]
    draft.consequences.exposure_delta = compute_exposure_delta(actor_id, verb, target_id)
    # 确保核心动作的双方都在 actors 列表里
    for nid in (actor_id, target_id):
        if nid in npc_names and nid not in draft.actors:
            draft.actors.append(nid)
    return draft
