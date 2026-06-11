"""Prompt 模板集中管理。

通用约束(防止 AI 篡改设定)在 GUARDRAIL 中统一声明,所有 prompt 都拼接它。
"""
from typing import List, Optional

from ..models.event import EventType
from ..models.memory import Memory, parse_memory_content
from ..models.npc import NPC
from ..models.relationship import Relationship
from ..models.world import WorldState

# 通用护栏:每个 prompt 都提醒模型不得越界
GUARDRAIL = (
    "【硬性约束】\n"
    "1. 不得改变 NPC 的固定事实(性格、欲望、恐惧、悔恨、秘密、身份)。\n"
    "2. 不得编造已有秘密之外的新重大设定。\n"
    "3. 严禁创造新的【有名字】的角色。剧情若需要背景人物(如讨债的人、路过的客人),"
    "只能用泛称指代(如『讨债人』『钱庄来的人』),不得为其起任何姓名,"
    "也不得让其取代或抢戏于既定角色。\n"
    "4. 该 NPC 只知道自己经历过/被告知过的事,不得引用它不可能知道的信息。\n"
    "5. 你只负责想象与表达,游戏数值与后果由程序裁决。"
)


def _mental_block(npc: NPC) -> str:
    """P3:把 NPC 压力饱和后的心理状态渲染成可拼接的提示块(未饱和返回空串)。"""
    text = npc.mental_state_text()
    return text + "\n" if text else ""


def _memories_text(recent: List[Memory], longterm: List[Memory]) -> str:
    """把记忆列表渲染成文本块。

    #5:把【我确定知道的事(观察事实)】与【我的推测】分开呈现,推测带可信度。
    纯字符串记忆(无推测)只进"确定知道的事";让 NPC 带着可能的误判去行动,更像真人。
    """
    facts: List[str] = []        # (来源, 天, 观察事实)
    guesses: List[str] = []      # (天, 推测 + 可信度)
    for label, mems in (("长期", longterm), ("最近", recent)):
        for m in mems:
            p = parse_memory_content(m.content)
            if p["observed"]:
                facts.append(f"- (第{m.day}天·{label}) {p['observed']}")
            if p["interpretation"]:
                conf = f"(可信度 {p['confidence']})" if p["confidence"] is not None else ""
                guesses.append(f"- (第{m.day}天) {p['interpretation']}{conf}")

    lines: List[str] = []
    if facts:
        lines.append("【我确定知道的事】")
        lines.extend(facts)
    if guesses:
        lines.append("【我的推测(可能有误,别当成事实)】")
        lines.extend(guesses)
    if not lines:
        lines.append("(暂无特别记忆)")
    return "\n".join(lines)


# ===========================================================================
# 1. 对话
# ===========================================================================
def build_dialogue_prompt(
    npc: NPC,
    rel_to_player: Relationship,
    recent: List[Memory],
    longterm: List[Memory],
    world: WorldState,
    today_summary: str,
    player_message: str,
) -> tuple[str, str]:
    """构造对话的 (system, user) prompt。返回结构化 DialogueResult。"""
    system = (
        f"{GUARDRAIL}\n\n"
        f"你正在扮演小镇酒馆故事里的一个角色。\n"
        f"{npc.fixed_profile_text(audience='player')}\n\n"
        "你要以这个角色的口吻、依据其性格与当前情绪,自然地回应玩家。"
    )
    user = (
        f"【当前状态】\n"
        f"压力:{npc.stress}/100\n"
        f"当前目标:{npc.current_goal}\n"
        f"{rel_to_player.summary_text()}\n"
        f"世界:{world.summary_text()}\n\n"
        f"{_memories_text(recent, longterm)}\n\n"
        f"【今日酒馆氛围】\n{today_summary or '(平常的一天)'}\n\n"
        f"【玩家对你说】\n{player_message}\n\n"
        "请按以下 JSON 结构输出:\n"
        "{\n"
        '  "reply": "你的口吻回复,自然中文,80字以内",\n'
        '  "visible_reaction": "玩家能观察到的神情或动作,如 回避/紧张/冷笑",\n'
        '  "relationship_delta": {"from": "' + npc.id + '", "to": "player",'
        ' "trust": 0, "fear": 0, "resentment": 0, "affection": 0, "suspicion": 0},\n'
        '  "memory_write": null 或 {"npc_id": "' + npc.id + '",'
        ' "content": "若这次对话值得记住,写下你记住的内容", "importance": 0-100,'
        ' "emotional_tag": "情绪标签"}\n'
        "}\n"
        "关系增量用 -10 到 10 的小幅度变化。被追问敏感话题应增加 suspicion;"
        "被真诚对待可增加 trust/affection。"
    )
    return system, user


# ===========================================================================
# 1b. NPC 主动搭话(玩家进门时,情绪最强烈的人先开口)
# ===========================================================================
def build_initiative_prompt(
    npc: NPC,
    rel_to_player: Relationship,
    recent: List[Memory],
    longterm: List[Memory],
    world: WorldState,
    today_summary: str,
    motive: str,
) -> tuple[str, str]:
    """构造"NPC 主动开口"的 (system, user) prompt,复用 DialogueResult 结构。

    motive 是程序根据该 NPC 当前最强烈的情绪/压力推断出的搭话动机,
    用来给 LLM 的开场白定调(试探/求助/抱怨/警告等)。
    """
    system = (
        f"{GUARDRAIL}\n\n"
        f"你正在扮演小镇酒馆故事里的一个角色。\n"
        f"{npc.fixed_profile_text(audience='player')}\n\n"
        "此刻玩家(酒馆的常客)刚走进店里。是你【主动】开口搭话,"
        "不是被动回答。开场白要符合你的性格与当下心境,自然、有动机,"
        "不要寒暄客套,而要透出你真正在意的事。"
    )
    user = (
        f"【当前状态】\n"
        f"压力:{npc.stress}/100\n"
        f"当前目标:{npc.current_goal}\n"
        f"{rel_to_player.summary_text()}\n"
        f"世界:{world.summary_text()}\n\n"
        f"{_memories_text(recent, longterm)}\n\n"
        f"【今日酒馆氛围】\n{today_summary or '(平常的一天)'}\n\n"
        f"【你此刻主动开口的动机】{motive}\n\n"
        "请按以下 JSON 结构输出你主动说的第一句话:\n"
        "{\n"
        '  "reply": "你主动搭话的内容,自然中文,60字以内",\n'
        '  "visible_reaction": "玩家能观察到的神情或动作,如 凑近压低声音/别开脸",\n'
        '  "relationship_delta": {"from": "' + npc.id + '", "to": "player",'
        ' "trust": 0, "fear": 0, "resentment": 0, "affection": 0, "suspicion": 0},\n'
        '  "memory_write": null 或 {"npc_id": "' + npc.id + '",'
        ' "content": "若这次主动开口值得记住,写下内容", "importance": 0-100,'
        ' "emotional_tag": "情绪标签"}\n'
        "}\n"
        "关系增量用 -5 到 5 的小幅度;主动倾诉可略增 trust/affection,"
        "主动试探/警告可略增 suspicion。"
    )
    return system, user


# ===========================================================================
# 2. 玩家行动影响评估
# ===========================================================================
def build_action_impact_prompt(
    actor_desc: str,
    target_npc: NPC,
    rel_target_to_player: Relationship,
    world: WorldState,
    action_type: str,
    content: str,
    amount: int,
) -> tuple[str, str]:
    """构造行动影响评估的 (system, user) prompt。返回结构化 ActionImpact。"""
    system = (
        f"{GUARDRAIL}\n\n"
        "你是一个游戏世界的'规则裁决器'。根据玩家行动、目标NPC的性格与当前状态,"
        "判断该行动造成的结构化后果。\n\n"
        "【铁律】每个行动必须至少产生一个后果(关系变化/压力变化/记忆/目标变化/旗标)。"
    )
    user = (
        f"【目标 NPC】\n{target_npc.name}({target_npc.id})\n"
        f"性格:{target_npc.personality}\n"
        f"恐惧:{target_npc.fear}\n"
        f"当前压力:{target_npc.stress}/100\n"
        f"{rel_target_to_player.summary_text()}\n"
        f"世界:{world.summary_text()}\n\n"
        f"【玩家行动】\n"
        f"类型:{action_type}\n"
        f"对象:{target_npc.id}\n"
        f"内容:{content}\n"
        f"金额:{amount}\n\n"
        "请输出如下 JSON:\n"
        "{\n"
        '  "relationship_changes": [{"from": "npc_id", "to": "player",'
        ' "trust":0,"fear":0,"resentment":0,"affection":0,"suspicion":0}],\n'
        '  "stress_changes": [{"npc": "npc_id", "delta": 0}],\n'
        '  "memory_writes": [{"npc_id": "npc_id", "content": "该NPC会记住的内容",'
        ' "importance": 0-100, "emotional_tag": "情绪"}],\n'
        '  "goal_changes": {"npc_id": "新的当前目标(仅在确有改变时填)"},\n'
        '  "flags": {"some_flag": true},\n'
        '  "athou_progress_delta": 0,\n'
        '  "narration": "给玩家看的简短结果旁白,中文"\n'
        "}\n"
        "数值幅度参考:关系 -15~15,压力 -10~15,进度 0~10。"
        "威胁/举报会提升 suspicion/fear/resentment;帮助/给钱会提升 trust/affection。"
        "只有触及阿土失踪相关关键信息时,athou_progress_delta 才大于0。"
    )
    return system, user


# ===========================================================================
# 3. NPC 今日意图
# ===========================================================================
def build_intention_prompt(
    npc: NPC,
    recent: List[Memory],
    longterm: List[Memory],
    world: WorldState,
) -> tuple[str, str]:
    """构造 NPC 今日意图生成的 (system, user) prompt。返回 NPCIntention。"""
    system = (
        f"{GUARDRAIL}\n\n"
        f"你在为小镇酒馆故事推演角色的当日行动意图。\n"
        f"{npc.fixed_profile_text()}\n\n"
        "基于角色的目标、压力与记忆,推断它今天最想做的一件事。"
    )
    user = (
        f"【当前状态】\n压力:{npc.stress}/100\n当前目标:{npc.current_goal}\n"
        f"{_mental_block(npc)}"
        f"世界:{world.summary_text()}\n\n"
        f"{_memories_text(recent, longterm)}\n\n"
        "输出 JSON:\n"
        "{\n"
        f'  "npc_id": "{npc.id}",\n'
        '  "intention": "今天最想做的一件具体的事",\n'
        '  "target": "针对的对象 id 或 player 或留空",\n'
        '  "risk_level": 0-100,\n'
        '  "reason": "为什么这么做,引用具体记忆或目标"\n'
        "}"
    )
    return system, user


# ===========================================================================
# 4. 当日事件生成
# ===========================================================================
def build_event_prompt(
    actor_id: str,
    actor_name: str,
    verb,
    verb_hint: str,
    target_id: str,
    target_name: str,
    intentions_text: str,
    world: WorldState,
    npc_names: dict,
) -> tuple[str, str]:
    """构造当日事件生成的 (system, user) prompt(动作语法版)。

    今天的"核心动作"(谁对谁做什么)已由程序裁决,LLM 只负责把这个动作
    展开成具体事件经过,不得偏离这个动作框架,也不得发明新角色。
    """
    name_map = "、".join(f"{k}={v}" for k, v in npc_names.items())
    legal_ids = "、".join(npc_names.keys())
    system = (
        f"{GUARDRAIL}\n\n"
        "你是小镇酒馆故事的'事件编剧'。今天的【核心动作】已由系统给定,"
        "你只能据此把事件写具体,不得偏离这个动作,也不得另起炉灶换主角。\n\n"
        f"【全部合法角色(只有这些人有名字,actors 只能取这些 id)】{name_map}\n"
        f"【合法 id】{legal_ids}\n"
        "涉及讨债人/钱庄等场外人物时,只能用泛称(如『讨债人』),严禁起名、"
        "严禁把它写进 actors。"
    )
    user = (
        f"【今日核心动作】{actor_name}({actor_id}) 对 {target_name}({target_id}) "
        f"{verb_hint}\n"
        f"【世界状态】{world.summary_text()}\n\n"
        f"【各 NPC 今日意图(可作为细节参考)】\n{intentions_text}\n\n"
        "请把上面这个核心动作展开成当天发生的一件具体事件,输出 JSON:\n"
        "{\n"
        '  "title": "事件标题",\n'
        '  "summary": "120字以内的事件经过,中文,必须体现上面的核心动作",\n'
        f'  "actors": ["涉及的 npc_id,至少包含 {actor_id} 和 {target_id}"],\n'
        '  "consequences": {\n'
        '    "relationships": [{"from":"id","to":"id","trust":0,"fear":0,'
        '"resentment":0,"affection":0,"suspicion":0}],\n'
        '    "stress": [{"npc":"id","delta":0}],\n'
        '    "flags": {"flag_name": true},\n'
        '    "athou_progress_delta": 0\n'
        "  }\n"
        "}\n"
        "关系增量 -15~15,压力 -10~15。只有当事件确实揭示阿土失踪相关真相时,"
        "athou_progress_delta 才大于0(1~10)。actors 只能用上面的合法 id。"
    )
    return system, user


# ===========================================================================
# 4b. 单个 NPC 行动落地(群像行动层:把意图展开成"今天具体做了什么 + 小后果")
# ===========================================================================
def build_action_resolution_prompt(
    npc: NPC,
    verb,
    verb_hint: str,
    target_id: str,
    target_name: str,
    intention_text: str,
    world: WorldState,
    npc_names: dict,
) -> tuple[str, str]:
    """构造"单个 NPC 今日行动落地"的 (system, user) prompt。返回 ActionResolutionDraft。

    今天这个 NPC 的核心动作(对谁做什么)已由程序裁决给定,LLM 只负责把它
    写成一句具体经过,并给出【小幅】后果。这样每个 NPC 都有自己今天做的事,
    再由程序综合成当日焦点事件。
    """
    legal_ids = "、".join(npc_names.keys())
    system = (
        f"{GUARDRAIL}\n\n"
        f"你在为小镇酒馆故事推演【{npc.name}】今天的一次具体行动。\n"
        f"{npc.fixed_profile_text()}\n\n"
        "今天这个角色要做的【核心动作】已由系统给定,你只能据此把它写成一句"
        "具体经过,不得换成别的动作,也不得发明新的有名字的角色。\n"
        f"【合法角色 id】{legal_ids}(关系/压力只能落在这些 id 或 player 上)"
    )
    target_line = (
        f"对 {target_name}({target_id}) " if target_id and target_id in npc_names else ""
    )
    user = (
        f"【你今天的核心动作】{target_line}{verb_hint}\n"
        f"【你的今日意图】{intention_text}\n"
        f"【当前状态】压力:{npc.stress}/100,当前目标:{npc.current_goal}\n"
        f"【世界状态】{world.summary_text()}\n\n"
        "请把这个动作写成今天发生的一件具体小事,输出 JSON:\n"
        "{\n"
        '  "narration": "你今天具体做了什么,一句话,40字内,必须体现上面的核心动作",\n'
        '  "consequences": {\n'
        '    "relationships": [{"from":"id","to":"id","trust":0,"fear":0,'
        '"resentment":0,"affection":0,"suspicion":0}],\n'
        '    "stress": [{"npc":"id","delta":0}],\n'
        '    "flags": {},\n'
        '    "athou_progress_delta": 0\n'
        "  }\n"
        "}\n"
        "这是【单个人的一次小动作】,后果要克制:关系增量 -8~8,压力 -6~8。"
        "只有确实触及阿土失踪真相时 athou_progress_delta 才 1~5。"
        f"relationships 的 from 应主要是 {npc.id}。actors/关系只能用上面的合法 id。"
    )
    return system, user


# ===========================================================================
# 4c. 综合众人行动 → 当日焦点事件
# ===========================================================================
def build_focal_event_prompt(
    headline_actor_name: str,
    headline_verb_hint: str,
    headline_target_name: str,
    actions_text: str,
    world: WorldState,
    npc_names: dict,
) -> tuple[str, str]:
    """构造"综合当天众人行动 → 写出焦点事件"的 (system, user) prompt。返回 EventDraft。

    当天每个 NPC 已各自行动(actions_text 列出),其中最重要的一条已被程序选为
    "今日头条"。LLM 只负责把这些行动编织成一段连贯的当日纪事,以头条动作为中心,
    不得另起炉灶、不得发明新角色。后果(数值)已由各 NPC 行动层裁决,这里只写叙事,
    consequences 可全部留 0。
    """
    name_map = "、".join(f"{k}={v}" for k, v in npc_names.items())
    legal_ids = "、".join(npc_names.keys())
    system = (
        f"{GUARDRAIL}\n\n"
        "你是小镇酒馆故事的'纪事编剧'。今天酒馆里每个人都做了自己的事,"
        "其中最关键的一件已被系统定为【今日头条】。你的任务是把这些行动编织成"
        "一段连贯的当日纪事,以头条为中心,顺带带出其他人的动向。\n\n"
        f"【全部合法角色(只有这些人有名字,actors 只能取这些 id)】{name_map}\n"
        f"【合法 id】{legal_ids}\n"
        "场外人物(讨债人等)只能用泛称,严禁起名或写进 actors。"
    )
    user = (
        f"【今日头条动作】{headline_actor_name} 对 {headline_target_name} "
        f"{headline_verb_hint}\n"
        f"【世界状态】{world.summary_text()}\n\n"
        f"【今天每个人各自做的事】\n{actions_text}\n\n"
        "请综合以上,写出当天的焦点事件(以头条为主线),输出 JSON:\n"
        "{\n"
        '  "title": "事件标题",\n'
        '  "summary": "120字以内的当日纪事,中文,以头条动作为中心,可顺带提及其他人动向",\n'
        f'  "actors": ["涉及的 npc_id,只能用合法 id"],\n'
        '  "consequences": {"relationships": [], "stress": [], "flags": {},'
        ' "athou_progress_delta": 0}\n'
        "}\n"
        "注意:数值后果已由各人行动单独结算,这里 consequences 一律留空/0,只写叙事。"
        "actors 只能用上面的合法 id。"
    )
    return system, user


# ===========================================================================
# 4d. 酒馆社交对话模式:NPC 每轮决策 / 对话回复 / 旁白观察 / 独自行动裁决
# ===========================================================================
def build_turn_decision_prompt(
    npc: NPC,
    recent: List[Memory],
    longterm: List[Memory],
    world: WorldState,
    others_text: str,
    round_no: int,
    total_rounds: int,
    personal_yesterday: str = "",
    conflict_brief: str = "",
) -> tuple[str, str]:
    """构造"某 NPC 本轮要做什么"的 (system, user) prompt。返回 TurnDecision。

    NPC 根据自己【已知的信息】(只有自己的记忆 + 公开观察)决定:找谁说话,
    或独自去做一件不需要对话的事(暗中调查/掩盖/回避)。

    conflict_brief(#2):该 NPC【本人参与】的冲突当前状态(含已结算)。注入后可避免
    冲突落槌后第二天还像没发生一样继续谈旧交易;严守知识隔离——只含其自己的冲突。
    """
    system = (
        f"{GUARDRAIL}\n\n"
        f"你正在扮演小镇酒馆故事里的一个角色,推演你今天的社交行动。\n"
        f"{npc.fixed_profile_text()}\n\n"
        "你只能依据【你自己知道的信息】行动:你的记忆、你的目标,以及你亲眼看到的人。"
        "你不知道别人私下都说了什么。"
    )
    # 引擎 A:按当前危机【阶段】注入局势压力(对话模式的"阶段事件池"等价物)。
    # 只在曝光/债务进入相应阶段时才出现,引导对应 NPC 的防守/施压姿态,不揭真相。
    directive = world.crisis_directive()
    directive_block = f"【当前局势压力(请据此调整你的行动姿态)】\n{directive}\n\n" if directive else ""
    # PR5:只属于"你自己"的昨日个人摘要(知识隔离),帮助今天的行动接得上昨天。
    yesterday_block = f"【你昨天自己做/经历的事(只有你知道)】\n{personal_yesterday}\n\n" if personal_yesterday else ""
    # #6:冲突上下文(参与者全量 + 旁观者模糊提示),并附"你必须遵守这些状态"的硬约束,
    # 防止落槌后第二天还谈旧交易、或当面去找已经躲起来的人。
    conflict_block = (
        f"【你当前感知到的局面】\n{conflict_brief}\n"
        "你必须遵守这些状态:\n"
        "- 如果某人最近不露面(已躲起来/离场),不要假设他在场、更不要当面找他说话。\n"
        "- 如果一桩交易/对峙已经了结或作废,不要再当它没发生、重复发起同一桩旧事。\n"
        "- 如果证据已被扣下或损毁,不要假装它还安然在手。\n"
        "- 对你只是旁观到的事,只能依据表象去猜,不要说得像你知道内情。\n\n"
    ) if conflict_brief else ""
    user = (
        f"【场景】此刻你在『昨日酒馆』店内(镇上唯一的酒馆,你们都在这儿)。\n"
        f"【当前状态】压力:{npc.stress}/100,当前目标:{npc.current_goal}\n"
        f"{_mental_block(npc)}"
        f"世界:{world.summary_text()}\n\n"
        f"{yesterday_block}"
        f"{conflict_block}"
        f"{directive_block}"
        f"【此刻同在酒馆、你可以找其搭话的人(附你对各人的关系,据此判断该亲近/试探/回避谁)】\n"
        f"{others_text}\n\n"
        f"{_memories_text(recent, longterm)}\n\n"
        f"【这是今天第 {round_no}/{total_rounds} 轮】\n"
        "请决定你这一轮要做什么,输出 JSON:\n"
        "{\n"
        '  "kind": "TALK 或 SOLO",\n'
        '  "target": "TALK 时填要搭话对象的 id(只能是上面列出的人);SOLO 时留空",\n'
        '  "content": "TALK 时:你主动开口要说/要问的话(自然中文,60字内);'
        'SOLO 时:你打算独自去做的事",\n'
        '  "solo_verb": "SOLO 时填 INVESTIGATE/CONCEAL/AVOID 之一;TALK 时留空",\n'
        '  "reason": "你为什么这么做,引用具体记忆或目标"\n'
        "}\n"
        "找人说话要有真实动机(试探/求助/质问/拉拢/警告);"
        "若你这一刻更想私下做事而非交谈,就选 SOLO。"
    )
    return system, user


def build_conversation_reply_prompt(
    npc: NPC,
    asker_id: str,
    asker_name: str,
    utterance: str,
    recent: List[Memory],
    longterm: List[Memory],
    world: WorldState,
    rel_to_asker: Optional[Relationship] = None,
    history: str = "",
) -> tuple[str, str]:
    """构造"被搭话者回复"的 (system, user) prompt。返回 ConversationReply。

    铁律:你只为自己说话、只写自己对对方的感受,绝不替对方写反应。
    rel_to_asker 给出"你对搭话者"的关系,让回复的亲疏/戒备符合既有立场。
    history(进阶版多回合):本场对话已发生的你来我往实录,让追问回合接得上文。
    """
    # NPC↔NPC 不渲染「外乡玩家」第一印象(B2);仅当搭话者就是玩家时才注入
    reply_audience = "player" if asker_id == "player" else ""
    system = (
        f"{GUARDRAIL}\n\n"
        f"你正在扮演小镇酒馆故事里的一个角色。{asker_name} 此刻主动来找你说话,"
        "你要以自己的口吻、依据自己的性格与记忆来回应。\n"
        f"{npc.fixed_profile_text(audience=reply_audience)}\n\n"
        "【铁律】你只能为【你自己】说话和反应,绝不能替 "
        f"{asker_name} 写他的话或他的反应。关系变化只写【你对他】的感受。"
    )
    rel_line = (
        f"【你对 {asker_name} 的关系】"
        f"信任{rel_to_asker.trust} 恐惧{rel_to_asker.fear} 怨恨{rel_to_asker.resentment} "
        f"好感{rel_to_asker.affection} 怀疑{rel_to_asker.suspicion}\n"
        if rel_to_asker is not None else ""
    )
    history_block = f"【这场对话到此为止的实录(接着往下回应)】\n{history}\n\n" if history else ""
    user = (
        f"【场景】你们都在『昨日酒馆』店内,{asker_name} 正当面对你说话。\n"
        f"【当前状态】压力:{npc.stress}/100,当前目标:{npc.current_goal}\n"
        f"{rel_line}"
        f"{world.summary_text()}\n\n"
        f"{_memories_text(recent, longterm)}\n\n"
        f"{history_block}"
        f"【{asker_name} 对你说】{utterance}\n\n"
        "请按以下 JSON 回复:\n"
        "{\n"
        '  "reply": "你的口吻回复,自然中文,80字内",\n'
        '  "visible_reaction": "旁人能看到的你的神情/动作,如 皱眉别开脸/凑近压低声音",\n'
        '  "relationship_delta": {"from": "' + npc.id + '", "to": "' + asker_id + '",'
        ' "trust":0,"fear":0,"resentment":0,"affection":0,"suspicion":0},\n'
        '  "memory_write": null 或 {"npc_id": "' + npc.id + '",'
        ' "content": "若值得记住,写下你记住的内容", "importance": 0-100,'
        ' "emotional_tag": "情绪"},\n'
        '  "wants_to_continue": true/false（你觉得这场对话还没说完、还想继续就 true,'
        '话已说尽就 false）\n'
        "}\n"
        "关系增量用 -8~8 的小幅;被追问敏感话题增 suspicion,被真诚相待增 trust/affection,"
        "被威胁增 fear/resentment。"
    )
    return system, user


def build_conversation_followup_prompt(
    npc: NPC,
    target_id: str,
    target_name: str,
    history: str,
    world: WorldState,
    rel_to_target: Optional[Relationship] = None,
) -> tuple[str, str]:
    """构造"发话者(A)听到回复后是否继续追问"的 (system, user) prompt。返回 ConversationFollowup。

    进阶版多回合对话用:A 已经开了口、B 也回了话,这里让 A 决定是否再追一句。
    铁律同样适用——A 只为自己说话,绝不替 B 写任何反应。
    """
    reply_audience = "player" if target_id == "player" else ""
    system = (
        f"{GUARDRAIL}\n\n"
        f"你正在扮演小镇酒馆故事里的一个角色。你刚才主动找 {target_name} 说话,"
        "他已经回应了你。你要依据自己的性格、目标与记忆,决定这场对话是否继续。\n"
        f"{npc.fixed_profile_text(audience=reply_audience)}\n\n"
        "【铁律】你只能为【你自己】说话,绝不能替 "
        f"{target_name} 写他的话或反应。"
    )
    rel_line = (
        f"【你对 {target_name} 的关系】"
        f"信任{rel_to_target.trust} 恐惧{rel_to_target.fear} 怨恨{rel_to_target.resentment} "
        f"好感{rel_to_target.affection} 怀疑{rel_to_target.suspicion}\n"
        if rel_to_target is not None else ""
    )
    user = (
        f"【场景】你们都在『昨日酒馆』店内,你正和 {target_name} 面对面交谈。\n"
        f"【当前状态】压力:{npc.stress}/100,当前目标:{npc.current_goal}\n"
        f"{rel_line}"
        f"{world.summary_text()}\n\n"
        f"【这场对话到此为止的实录】\n{history}\n\n"
        "请决定你接下来怎么办,输出 JSON:\n"
        "{\n"
        '  "continue_talking": true/false（还有要追问/回应/交代的就 true,'
        '目的已达到、话已说尽或没必要再纠缠就 false）,\n'
        '  "utterance": "continue_talking 为 true 时:你接着对他说/追问的话(自然中文,60字内);否则留空"\n'
        "}\n"
        "只有当你确实还有动机(继续试探/逼问/解释/拉拢/警告)时才继续;"
        "若对方已明显回避、或你已问到想要的、或再说也无益,就收口(false)。"
    )
    return system, user


def build_narrator_prompt(acts_text: str, npc_names: dict) -> tuple[str, str]:
    """构造"旁白观察"的 (system, user) prompt。返回 NarratorObservation。

    旁白是酒馆里一个沉默的旁观者:只能看到谁和谁凑在一起、各自的神态,
    听不到具体谈了什么。严禁泄露对话内容,严禁做任何判断或推测动机。
    """
    legal_ids = "、".join(npc_names.keys())
    name_map = "、".join(f"{k}={v}" for k, v in npc_names.items())
    system = (
        f"{GUARDRAIL}\n\n"
        "你是酒馆里一个沉默的旁观者(旁白)。你能看见谁和谁凑在一起、各自的神态举止、"
        "看得见的动作与物件,但【听不到】他们具体说了什么。\n\n"
        "【铁律】\n"
        "1. 只陈述可观察到的事实:谁找了谁、谁独自做了什么、动了什么物件、神态如何。\n"
        "2. 严禁写出或暗示对话的【具体内容】(你根本听不到)。\n"
        "3. 严禁做价值判断、严禁推测动机或后果,只白描。\n"
        f"【合法角色】{name_map}(actors 字段只能填这些 id:{legal_ids};"
        "但 event_core 与 demeanor 的描述文字里【必须用中文名】,绝不能出现英文 id)"
    )
    user = (
        f"【这一轮里实际发生的互动(仅供你判断谁和谁有来往,不要照抄内容)】\n{acts_text}\n\n"
        "请以旁观者视角,白描你【看到】的画面,输出 JSON:\n"
        "{\n"
        '  "notes": [\n'
        '    {"actors": ["相关 npc_id"],\n'
        '     "event_core": "这次来往里看得见的【核心动作或具体细节】,'
        '必须包含一个具体物件或动作结果,不含任何听到的对话内容,'
        '如 『阿财把一张折过的纸条塞给老陈后匆匆离开』",\n'
        '     "demeanor": "可观察到的神态,如 『老陈接过纸条时脸色一沉』"}\n'
        "  ]\n"
        "}\n"
        "每段来往/独自行动写一条 note。\n"
        "【硬性要求】每条 note 的 event_core 必须落到一个【具体物件 / 动作结果 / 可记忆细节】,"
        "不得只写『低声交谈』『眼神闪烁』『神色不对』这类空泛描述;只写看得见的,绝不写听得见的。"
    )
    return system, user


def build_solo_referee_prompt(
    npc: NPC,
    verb_hint: str,
    intent: str,
    world: WorldState,
    fact_sheet: str,
) -> tuple[str, str]:
    """构造"独自行动裁决"的 (system, user) prompt。返回 SoloOutcome。

    裁决器读【权威世界事实】(由程序从 DB 取出的相关切片)来给出一致的结果,
    当事人无权自判。数值后果仍会被程序 clamp。
    """
    system = (
        f"{GUARDRAIL}\n\n"
        "你是小镇酒馆故事的'结果裁决器'。某个角色独自做了一件事,你要依据"
        "【权威世界事实】判断这次行动【真实】得到的结果,而不是他一厢情愿的想象。\n\n"
        "结果要与既有事实一致:查不到的就查不到,掩盖未必成功。只写结果,数值由程序定标。"
    )
    user = (
        f"【行动者】{npc.name}({npc.id}),性格:{npc.personality}\n"
        f"【他独自做的事】{verb_hint}:{intent}\n"
        f"【世界状态】{world.summary_text()}\n\n"
        f"【权威世界事实(裁决依据)】\n{fact_sheet}\n\n"
        "请裁决这次行动的真实结果,输出 JSON:\n"
        "{\n"
        '  "narration": "只有他自己知道的私密经过/结果,一句话,40字内",\n'
        '  "discovery": "若他确实查到了具体线索就写下(将成为他的私密记忆),否则留空",\n'
        '  "consequences": {"relationships": [{"from":"' + npc.id + '","to":"id",'
        '"trust":0,"fear":0,"resentment":0,"affection":0,"suspicion":0}],'
        ' "stress": [{"npc":"' + npc.id + '","delta":0}], "flags": {},'
        ' "athou_progress_delta": 0}\n'
        "}\n"
        "关系增量 -8~8,压力 -6~8。只有确实触及阿土失踪真相时 athou_progress_delta 才 1~5。"
    )
    return system, user


# ===========================================================================
# 5. NPC 反思(每隔几天总结处境、更新目标)
# ===========================================================================
def build_reflection_prompt(
    npc: NPC,
    recent: List[Memory],
    longterm: List[Memory],
    world: WorldState,
) -> tuple[str, str]:
    """构造 NPC 自我反思的 (system, user) prompt,返回 ReflectionResult。

    让 NPC 回顾近期经历,凝练出对自身处境的内心总结,并据此调整当前目标,
    使其行为有长线连贯性,而非每天孤立决策。
    """
    system = (
        f"{GUARDRAIL}\n\n"
        f"你在扮演小镇酒馆故事里的角色,进行一次私密的【自我反思】。\n"
        f"{npc.fixed_profile_text()}\n\n"
        "回顾这段时间发生在你身上的事,以这个角色的内心视角,"
        "凝练出你此刻对自己处境的真实判断,并据此决定接下来要紧盯的目标。"
    )
    user = (
        f"【当前状态】\n压力:{npc.stress}/100\n当前目标:{npc.current_goal}\n"
        f"{_mental_block(npc)}"
        f"世界:{world.summary_text()}\n\n"
        f"{_memories_text(recent, longterm)}\n\n"
        "输出 JSON:\n"
        "{\n"
        f'  "npc_id": "{npc.id}",\n'
        '  "observed": "用第一人称写下你这段时间【确凿经历或被明确告知的事】(只写事实,不掺推测),40字内",\n'
        '  "summary": "用第一人称写下你对自己当前处境的【内心解读/推测】,60字内(可含猜测语气)",\n'
        '  "confidence": 你对上面这份解读的把握度(0-100 的整数),\n'
        '  "updated_goal": "若你决定调整接下来的目标就写新目标,否则留空",\n'
        '  "mood": "一个心境标签,如 焦虑/孤注一掷/麻木/警惕"\n'
        "}\n"
        "区分事实与推测:observed 只放你确定的事,summary 放你的判断/猜测;"
        "不得引用你不可能知道的信息。"
    )
    return system, user
