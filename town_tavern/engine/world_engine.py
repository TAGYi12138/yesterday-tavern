"""世界引擎:advance_day() 主循环编排。

伪代码:
    读取当前天
      → 为每个 NPC 生成今日意图
      → 生成当日事件(类型固定 + LLM 填细节)
      → 应用事件后果
      → 按"谁知道"写入各 NPC 记忆(记忆隔离)
      → 压缩记忆
      → 推进阿土主线 / 全局紧张度
      → 推进天数
"""
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, List, Optional

from ..config import (
    ATHOU_PROGRESS_MAX, ATHOU_PROGRESS_MIN, CONVERSATION_MODE, DEBT_DAILY_INTEREST,
    DEBT_HIGH_INTEREST_MULT, DEBT_STRESS_THRESHOLD, MAX_AUTO_ADVANCE_DAYS,
    PER_NPC_ACTION_LLM, REAL_SECONDS_PER_DAY, REFLECTION_INTERVAL_DAYS,
    RELATION_MIN, RELATION_MAX, TENSION_DAILY_DECAY,
)
from ..llm.client import LLMClient
from ..models.action import NPCIntention
from ..models.event import Event, EventConsequences
from ..models.memory import MemoryType
from ..storage.repository import Repository
from . import (
    conversation_engine, event_engine, memory_engine, npc_engine, relationship_engine,
)


def _format_consequences(event: Event, name_of: dict) -> List[str]:
    """把事件后果渲染成人类可读的中文行,便于日志展示"这件事改变了什么"。"""
    c = event.consequences
    lines: List[str] = []

    # 关系变化:谁对谁的 信任/恐惧/怨恨/好感/怀疑 增减
    field_cn = {
        "trust": "信任", "fear": "恐惧", "resentment": "怨恨",
        "affection": "好感", "suspicion": "怀疑",
    }
    for rel in c.relationships:
        deltas = [
            f"{cn}{getattr(rel, f):+d}"
            for f, cn in field_cn.items() if getattr(rel, f, 0)
        ]
        if deltas:
            a = name_of.get(rel.from_npc, rel.from_npc)
            b = name_of.get(rel.to_npc, rel.to_npc)
            lines.append(f"      关系:{a}→{b} {' '.join(deltas)}")

    # 压力变化
    for s in c.stress:
        if s.delta:
            lines.append(f"      压力:{name_of.get(s.npc, s.npc)} {s.delta:+d}")

    # 主线 / 曝光 / 旗标
    if c.athou_progress_delta:
        lines.append(f"      阿土真相进度 {c.athou_progress_delta:+d}")
    if c.exposure_delta:
        lines.append(f"      老陈曝光风险 {c.exposure_delta:+d}")
    if c.flags:
        lines.append(f"      触发旗标:{', '.join(c.flags.keys())}")

    if not lines:
        lines.append("      (本次无显著数值后果)")
    return lines


def advance_day(
    repo: Repository,
    llm: LLMClient,
    game_id: str,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Event:
    """推进一天,返回当天生成的事件。

    on_progress 是可选的"过程阶段"回调:在生成每个 NPC 意图、综合事件、
    反思等关键节点被调用(传入一句中文进度描述),便于守护进程/CLI 把
    "这一天是怎么一步步拼出来的"实时打到日志。引擎层不直接 print。
    """
    day = repo.get_current_day(game_id)
    new_day = day + 1
    npcs = repo.get_all_npcs(game_id)
    name_of = {n.id: n.name for n in npcs}

    def _p(msg: str) -> None:
        if on_progress is not None:
            on_progress(msg)

    # Step 1~5: 生成当日事件(三选一模式),并落地后果与记忆。
    if CONVERSATION_MODE:
        # 【对话模式】每天多轮社交:各 NPC 自行决策(找人对话/独自行动),
        # 对话双方各自为自己说话(不代笔),旁白只记神态(不泄内容),裁决器定独自行动结果。
        # 后果在轮内逐一落地、记忆按知识隔离写入,故此处不再二次应用/写记忆。
        _p(f"第{new_day}天 · 酒馆里的人各自开始今天的活动……")
        event = conversation_engine.run_social_day(
            repo, llm, game_id, new_day, on_progress=on_progress
        )
        _p(f"  → 当日纪事《{event.title}》")
        _p(f"      {event.summary}")
        for line in _format_consequences(event, name_of):
            _p(line)
        # 后果已在对话引擎内逐一应用,这里仅补一次全局紧张度升温(避免重复计数)
        _bump_global_tension(repo, game_id, event.consequences)
        event.id = repo.add_event(game_id, event)
    else:
        # 【旧模式】先生成意图,再走群像行动层(PER_NPC_ACTION_LLM)或单一核心动作兜底。
        intentions: List[NPCIntention] = []
        for npc in npcs:
            _p(f"第{new_day}天 · {npc.name} 正在盘算今日打算……")
            try:
                intention = npc_engine.generate_intention(repo, llm, game_id, npc.id)
            except Exception:
                # 单个 NPC 意图生成失败不应阻断整天推进
                _p(f"  · {npc.name} 一时没了主意(生成失败,跳过)")
                continue
            intentions.append(intention)
            tgt = name_of.get(intention.target, intention.target) if intention.target else "—"
            _p(f"  · {npc.name} 打算:{intention.intention}(对象:{tgt} 风险:{intention.risk_level})")

        world = repo.get_world_state(game_id)
        if PER_NPC_ACTION_LLM and intentions:
            _p(f"第{new_day}天 · 众人各自行动……")
            resolved = event_engine.resolve_all_actions(
                repo, llm, game_id, intentions, world, on_progress=on_progress
            )
            if resolved:
                # 各人小后果汇总,由焦点事件承载并一次性落地(不重复应用)
                agg = event_engine.aggregate_consequences(resolved)
                _p(f"第{new_day}天 · 综合众人行动,生成当日焦点事件……")
                draft = event_engine.compose_focal_event(
                    repo, llm, game_id, resolved, world, on_progress=on_progress
                )
                event = draft.to_event(new_day)
                event.consequences = agg
                # 每个 NPC 记住自己今天做的事(增强个体连贯性)
                _write_action_memories(repo, game_id, resolved, new_day)
            else:
                # 全员行动解析失败,退回单一核心动作兜底
                draft = event_engine.generate_daily_event(
                    repo, llm, game_id, intentions, on_progress=on_progress
                )
                event = draft.to_event(new_day)
        else:
            _p(f"第{new_day}天 · 综合众人意图,生成当日事件……")
            draft = event_engine.generate_daily_event(
                repo, llm, game_id, intentions, on_progress=on_progress
            )
            event = draft.to_event(new_day)

        _p(f"  → 当日事件《{event.title}》")
        _p(f"      经过:{event.summary}")
        for line in _format_consequences(event, name_of):
            _p(line)

        _apply_event_consequences(repo, game_id, event)
        _write_event_memories(repo, game_id, event)
        event.id = repo.add_event(game_id, event)

    # Step 6: 压缩各 NPC 记忆
    for npc in npcs:
        memory_engine.compress_memories_if_needed(repo, game_id, npc.id, new_day, llm)

    # Step 7: 周期性反思——每隔 REFLECTION_INTERVAL_DAYS 天,各 NPC 总结处境、更新目标
    if REFLECTION_INTERVAL_DAYS > 0 and new_day % REFLECTION_INTERVAL_DAYS == 0:
        _p(f"第{new_day}天 · 反思日:各人重新审视自己的处境……")
        for npc in npcs:
            _p(f"  · {npc.name} 正在反思……")
            try:
                npc_engine.reflect(repo, llm, game_id, npc.id, new_day)
            except Exception:
                # 单个 NPC 反思失败不应阻断整天推进
                continue

    # Step 8: 活变量每日演化(债务滚利息、全局紧张度自然衰减)
    _daily_world_tick(repo, game_id)

    # Step 9: 推进天数,并为新的一天重置玩家行动点与免费聊天额度
    repo.increment_day(game_id)
    repo.reset_player_day(game_id)
    return event


def _daily_world_tick(repo: Repository, game_id: str) -> None:
    """每天结算的"活变量":债务利滚利、紧张度自然回落。

    让 boss_debt / global_tension 真正随时间演化,而非静态背景设定。
    """
    world = repo.get_world_state(game_id)

    # 债务每日利息:阿财压力越大,被催得越狠,利息增长越快
    boss = repo.get_npc(game_id, "boss")
    interest = DEBT_DAILY_INTEREST
    if boss and boss.stress >= DEBT_STRESS_THRESHOLD:
        interest = int(interest * DEBT_HIGH_INTEREST_MULT)
    repo.add_boss_debt(game_id, interest)

    # 全局紧张度自然衰减(无新冲突时慢慢回落,避免单调饱和)
    if world.global_tension > 0:
        repo.set_world_value(
            game_id, "global_tension",
            max(RELATION_MIN, world.global_tension - TENSION_DAILY_DECAY),
        )


def advance_world(
    repo: Repository,
    llm: LLMClient,
    game_id: str,
    steps: int,
    on_step: Optional[Callable[[int, Event], None]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> List[Event]:
    """连续推进世界 steps 天(活世界核心),返回这期间生成的事件列表。

    on_step 是可选回调,每推进完一天即被调用(day_index, event);
    on_progress 为逐阶段进度回调,透传给 advance_day。
    便于 CLI/守护实时显示进度。任一天推进失败不会中断整体流程。
    """
    events: List[Event] = []
    for i in range(max(0, steps)):
        try:
            event = advance_day(repo, llm, game_id, on_progress=on_progress)
        except Exception:
            # 单天推进失败则跳过,保证世界尽量继续向前
            continue
        events.append(event)
        if on_step is not None:
            on_step(i + 1, event)
    return events


def sync_with_real_time(
    repo: Repository,
    llm: LLMClient,
    game_id: str,
    now: Optional[datetime] = None,
    on_step: Optional[Callable[[int, Event], None]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> List[Event]:
    """按现实流逝时间自动补推世界(挂机长草)。

    依据 last_advanced_at 与现实时间差,折算成应推进的天数(封顶
    MAX_AUTO_ADVANCE_DAYS),连续推进并刷新时间锚点。返回补推的事件列表。
    """
    now = now or datetime.now(timezone.utc)
    last = repo.get_last_advanced_at(game_id)
    if last is None:
        # 没有锚点(老存档),直接以当前时间建立锚点,不补推
        repo.set_last_advanced_at(game_id, now)
        return []

    elapsed = (now - last).total_seconds()
    if REAL_SECONDS_PER_DAY <= 0:
        return []
    days = int(elapsed // REAL_SECONDS_PER_DAY)
    if days <= 0:
        return []

    days = min(days, MAX_AUTO_ADVANCE_DAYS)
    events = advance_world(
        repo, llm, game_id, days, on_step=on_step, on_progress=on_progress
    )
    # 刷新锚点为当前时间(MVP:不保留余数)
    repo.set_last_advanced_at(game_id, now)
    return events


def build_return_briefing(repo: Repository, game_id: str) -> str:
    """生成"玩家回来"的回归简报。

    设计要点(避免"玩家回来什么都看不到"的空洞感):
    - 公开事件:给出完整经过,玩家明面上能知道。
    - 私密事件:不剧透具体内容,只把"谁神色有异"作为模糊线索抛出,
      勾引玩家主动去找相关 NPC 挖掘真相。
    展示后把 last_seen_day 推进到当前天,表示玩家已读。
    """
    world = repo.get_world_state(game_id)
    last_seen = repo.get_last_seen_day(game_id)
    current = world.current_day

    if current <= last_seen:
        # 没有新进展
        return ""

    all_events = repo.get_events_in_range(
        game_id, last_seen + 1, current, only_public=False
    )
    repo.set_last_seen_day(game_id, current)

    span = current - last_seen
    if not all_events:
        return f"你离开了 {span} 天。酒馆一切如常,没听说出什么事。"

    public = [e for e in all_events if e.visibility == "public"]
    private = [e for e in all_events if e.visibility != "public"]

    # id -> 名字,便于线索展示
    name_of = {n.id: n.name for n in repo.get_all_npcs(game_id)}

    lines = [f"你离开的这 {span} 天里——"]

    if public:
        lines.append("\n【你听说的事】")
        for ev in public:
            actors = "、".join(name_of.get(a, a) for a in ev.actors) or "一些人"
            lines.append(f"  · 第{ev.day}天 {ev.summary}(涉及:{actors})")

    if private:
        # 把私密事件里"出镜最多"的关键人物作为模糊线索,不剧透内容,
        # 也不把所有人都列上(否则等于"全员可疑",失去指向性)。
        counter: Counter = Counter()
        for ev in private:
            for a in ev.actors:
                counter[a] += 1
        suspects = [name_of.get(a, a) for a, _ in counter.most_common(2)]
        if suspects:
            who = "、".join(suspects)
            quantifier = "" if len(suspects) == 1 else "这两人"
            lines.append(
                "\n【你察觉到的异样】\n"
                f"  你回来后隐约觉得 {who}{quantifier} 神色不对,"
                "像是背着人在忙活什么,具体是什么你还不清楚。"
            )
        else:
            lines.append("\n【你察觉到的异样】\n  酒馆暗处似乎有些事在悄悄发酵。")

    lines.append("\n想弄清楚?去找相关的人聊聊,或许能问出端倪。")
    return "\n".join(lines)


def _apply_event_consequences(repo: Repository, game_id: str, event: Event) -> None:
    """把事件后果落到数据库。"""
    cons = event.consequences

    # 关系变化
    relationship_engine.apply_relationship_deltas(repo, game_id, cons.relationships)

    # 压力变化
    for s in cons.stress:
        repo.update_npc_stress(game_id, s.npc, s.delta)

    # 旗标
    for flag, value in cons.flags.items():
        repo.set_world_value(game_id, f"flag_{flag}", "1" if value else "0")

    # 阿土主线进度
    if cons.athou_progress_delta:
        cur = repo.get_world_state(game_id).athou_truth_progress
        new = max(ATHOU_PROGRESS_MIN, min(ATHOU_PROGRESS_MAX, cur + cons.athou_progress_delta))
        repo.set_world_value(game_id, "athou_truth_progress", new)

    # 老陈曝光风险(由动作语法程序化裁决的增量)
    if cons.exposure_delta:
        repo.add_exposure(game_id, cons.exposure_delta)

    # 全局紧张度:有冲突/勒索/警告类事件时整体升温
    _bump_global_tension(repo, game_id, cons)


def _bump_global_tension(repo: Repository, game_id: str, cons: EventConsequences) -> None:
    """当日事件整体升温全局紧张度(有压力波动升 3,否则升 1)。

    抽出独立函数,使"对话模式"在轮内已逐一应用后果后,仍能补一次紧张度升温,
    而不必重复应用关系/压力(避免重复计数)。
    """
    tension = repo.get_world_state(game_id).global_tension
    bump = 3 if cons.stress else 1
    new_tension = max(RELATION_MIN, min(RELATION_MAX, tension + bump))
    repo.set_world_value(game_id, "global_tension", new_tension)


def _write_action_memories(repo: Repository, game_id: str, resolved_list, day: int) -> None:
    """群像行动层:让每个 NPC 记住自己今天具体做的那件事。

    这是个体连贯性的关键——NPC 反思/后续意图能引用"我昨天去做了什么",
    而不只是记住那条被选为头条的焦点事件。
    """
    for r in resolved_list:
        if not r.narration:
            continue
        # 私密行动重要度略高(是当事人自己的密谋,印象更深)
        importance = 60 if r.visibility != "public" else 50
        related = r.target_id if r.target_id else None
        memory_engine.write_memory(
            repo, game_id, r.actor_id, day,
            content=f"我今天:{r.narration}",
            mtype=MemoryType.SYSTEM_EVENT, importance=importance,
            related_npc=related,
        )


def _write_event_memories(repo: Repository, game_id: str, event: Event) -> None:
    """按"谁知道"原则写入记忆。

    - 事件 actors:亲历者,写入高重要度记忆。
    - 其他 NPC:仅当事件 visibility 为 public 时,作为听闻的传闻写入低重要度记忆。
    """
    actors = set(event.actors)
    for npc_id in actors:
        memory_engine.write_memory(
            repo, game_id, npc_id, event.day,
            content=f"{event.title}:{event.summary}",
            mtype=MemoryType.SYSTEM_EVENT, importance=70,
        )

    if event.visibility == "public":
        all_ids = {n.id for n in repo.get_all_npcs(game_id)}
        for npc_id in all_ids - actors:
            memory_engine.write_memory(
                repo, game_id, npc_id, event.day,
                content=f"听说:{event.title}",
                mtype=MemoryType.RUMOR, importance=35,
            )


def build_today_summary(repo: Repository, llm: LLMClient, game_id: str) -> str:
    """生成当天酒馆氛围摘要,供查看状态/对话上下文使用。"""
    world = repo.get_world_state(game_id)
    event = repo.get_event_by_day(game_id, world.current_day)
    if event:
        return event.summary
    # 第一天还没有事件时,给一句默认氛围
    return "酒馆刚开门,几缕油烟和酒气混在一起,客人还不多。"
