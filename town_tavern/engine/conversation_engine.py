"""酒馆社交对话引擎(Generative Agents 风格的当日推演)。

每天进行 CONV_ROUNDS 轮社交,每轮:
  1. 每个 NPC 各自决策(找人对话 TALK / 独自行动 SOLO)——只依据自己已知信息。
  2. 被点名者对每条搭话各回一次——【只为自己说话、只写自己对对方的感受】,杜绝代笔。
  3. 独自行动由"裁决器"给结果(读权威世界状态;程序 clamp 数值),当事人不自判。
  4. 旁白 AI 只记录"谁和谁有来往 + 神态"(绝不泄露对话内容、不做判断)。

记忆与可见性:
  - 对话双方各自把【全文经过】记入自己的私密记忆(只有当事两人知道)。
  - 旁白观察(无内容、只有神态)作为低重要度传闻写给【未直接参与】的旁观者,
    并汇总成当日公开事件,进入玩家回归简报。
  - 次日各 NPC 的决策会自然读到这些记忆(知识隔离严格保持)。
"""
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional

from ..config import (
    CONV_CONCURRENCY, CONV_REFEREE_LLM, CONV_ROUNDS,
    TALK_MAX_TURNS_BASE, TALK_MAX_TURNS_HIGH, TALK_TENSION_THRESHOLD,
)
from ..llm import prompts
from ..llm.client import LLMClient
from ..models.conversation import (
    ConversationFollowup, ConversationReply, NarratorObservation, SoloOutcome,
    TurnDecision, TurnKind,
)
from ..models.event import (
    ActionVerb, Event, EventConsequences, EventType, RelationshipDelta,
)
from ..models.memory import MemoryType
from ..storage.repository import Repository
from . import conflict_engine, event_engine, memory_engine, relationship_engine

# 独自行动可用的动作(对方无需在场/不知情)
_SOLO_VERBS = {
    "INVESTIGATE": ActionVerb.INVESTIGATE,
    "CONCEAL": ActionVerb.CONCEAL,
    "AVOID": ActionVerb.AVOID,
}
_SOLO_HINT = {
    ActionVerb.INVESTIGATE: "暗中调查、打探",
    ActionVerb.CONCEAL: "掩盖、销毁线索",
    ActionVerb.AVOID: "回避、疏远",
}


def _apply_consequences(
    repo: Repository, game_id: str, cons: EventConsequences, agg: EventConsequences
) -> None:
    """把一份后果【即时】落到数据库(供后续轮次读到最新状态),并累加进当日汇总 agg。

    曝光/主线/紧张度的边界裁剪沿用 repository 与 config 的统一规则。
    """
    if cons.relationships:
        relationship_engine.apply_relationship_deltas(repo, game_id, cons.relationships)
        agg.relationships.extend(cons.relationships)
    for s in cons.stress:
        if s.delta:
            repo.update_npc_stress(game_id, s.npc, s.delta)
            agg.stress.append(s)
    for flag, value in cons.flags.items():
        repo.set_world_value(game_id, f"flag_{flag}", "1" if value else "0")
        agg.flags[flag] = value
    # 【核心原则:真相归玩家】NPC 自运行链路(裁决器/对话)产出的 athou_progress_delta
    # 一律【不落地】到阿土真相进度(玩家驱动)——NPC 自跑只推局势,绝不替玩家揭真相。
    # 但其正向部分会转化为【真相压力 truth_pressure】(有平台上限),用于驱动危机阶段;
    # 仍记进 agg 仅供日志/调试展示,世界的"玩家真相进度"真值不变。
    if cons.athou_progress_delta:
        agg.athou_progress_delta += cons.athou_progress_delta
        if cons.athou_progress_delta > 0:
            repo.add_truth_pressure(game_id, cons.athou_progress_delta)
    if cons.exposure_delta:
        repo.add_exposure(game_id, cons.exposure_delta)
        agg.exposure_delta += cons.exposure_delta


def _others_text(repo: Repository, game_id: str, self_id: str) -> str:
    """列出此刻可搭话的其他 NPC(id=名字(职业) + 自己对其关系),供决策时挑对象。

    附上"自己对各人的五维关系",让 NPC 据此判断该亲近/试探/防备/回避谁——
    这是知识隔离允许的信息(本就是自己对别人的感受,不涉及别人的私密)。
    """
    lines = []
    for n in repo.get_all_npcs(game_id):
        if n.id == self_id:
            continue
        rel = repo.get_relationship(game_id, self_id, n.id)
        lines.append(
            f"- {n.id}={n.name}({n.job});你对他:"
            f"信任{rel.trust} 恐惧{rel.fear} 怨恨{rel.resentment} "
            f"好感{rel.affection} 怀疑{rel.suspicion}"
        )
    return "\n".join(lines)


def _fact_sheet(repo: Repository, game_id: str) -> str:
    """裁决器的"权威事实切片":直接取自 DB 的全局真相(数值 + 危险标签)。

    这就是"结合以往所有人行动"的压缩——世界状态本身即累积结果,无需额外记忆。
    """
    w = repo.get_world_state(game_id)
    base = (
        f"阿土真相进度={w.athou_truth_progress}/100;"
        f"老陈曝光风险={w.police_exposure_risk}({w.exposure_level()});"
        f"阿财欠债={w.boss_debt}({w.debt_level()});"
        f"全局紧张度={w.global_tension}。"
        "（提示:进度越低越难查到真相,曝光越高线索越多;掩盖未必成功。）"
    )
    # 引擎 A:把当前危机阶段的局势压力也喂给裁决器,使独自行动结果与阶段一致
    directive = w.crisis_directive()
    return f"{base}\n{directive}" if directive else base


def _gather_json(llm: LLMClient, jobs: List[Optional[tuple]]) -> List[Optional[object]]:
    """并发执行一批【纯网络】LLM 调用,保持与输入等长、同序的结果列表。

    每个 job 形如 (system, user, schema) 或 None(None 表示该位置无需调用)。
    单个调用失败返回 None,不影响其他;并发度由 CONV_CONCURRENCY 控制(<=1 即串行)。
    线程内【绝不】触碰数据库——所有 DB 读写都在主线程完成,以规避 sqlite 线程安全问题。
    """
    results: List[Optional[object]] = [None] * len(jobs)

    def _call(idx: int):
        job = jobs[idx]
        if job is None:
            return idx, None
        try:
            return idx, llm.chat_json(job[0], job[1], job[2])
        except Exception:
            return idx, None

    indices = list(range(len(jobs)))
    if CONV_CONCURRENCY <= 1:
        for idx in indices:
            _, results[idx] = _call(idx)
        return results
    with ThreadPoolExecutor(max_workers=CONV_CONCURRENCY) as ex:
        for idx, res in ex.map(_call, indices):
            results[idx] = res
    return results


def _prepare_talk(
    repo: Repository, game_id: str, asker_id: str, asker_name: str,
    target_id: str, utterance: str,
) -> Optional[dict]:
    """主线程预备一次"A 找 B 说话":读 B 的记忆/世界、构造回复 prompt。

    返回 spec(含待并发执行的 job),target 不存在时返回 None。
    """
    target = repo.get_npc(game_id, target_id)
    if target is None:
        return None
    world = repo.get_world_state(game_id)
    recent = repo.get_recent_memories(game_id, target_id)
    longterm = repo.get_longterm_memories(game_id, target_id)
    rel_to_asker = repo.get_relationship(game_id, target_id, asker_id)
    system, user = prompts.build_conversation_reply_prompt(
        target, asker_id, asker_name, utterance, recent, longterm, world, rel_to_asker
    )
    return {
        "kind": "talk", "asker_id": asker_id, "asker_name": asker_name,
        "target_id": target_id, "target_name": target.name, "utterance": utterance,
        "job": (system, user, ConversationReply),
    }


def _apply_reply_delta(
    repo: Repository, game_id: str, target_id: str, asker_id: str,
    reply: ConversationReply, agg: EventConsequences,
) -> None:
    """落地 B 这一句回复带来的感受变化:强制 from=B、to=A,小幅裁剪(只代表自己,不代笔)。"""
    d = reply.relationship_delta
    clamped = RelationshipDelta(**{
        "from": target_id, "to": asker_id,
        "trust": event_engine._clamp(d.trust, -8, 8),
        "fear": event_engine._clamp(d.fear, -8, 8),
        "resentment": event_engine._clamp(d.resentment, -8, 8),
        "affection": event_engine._clamp(d.affection, -8, 8),
        "suspicion": event_engine._clamp(d.suspicion, -8, 8),
    })
    _apply_consequences(repo, game_id, EventConsequences(relationships=[clamped]), agg)


def _talk_turn_budget(repo: Repository, game_id: str, a_id: str, b_id: str) -> int:
    """这一对本次对话最多进行几个回合(A说+B回=1回合)。

    高张力对子给 TALK_MAX_TURNS_HIGH,普通寒暄给 TALK_MAX_TURNS_BASE。判定"高张力":
    - 双向关系里任一对抗维度(怀疑/怨恨/恐惧)达到 TALK_TENSION_THRESHOLD;或
    - 两人同属某个未结的冲突(如录音交易)的参与者。
    这样普通寒暄保持一来一回,真正有张力的对峙/砍价/摊牌才你来我往,避免 token 无谓飙升。
    """
    high = max(TALK_MAX_TURNS_HIGH, 1)
    base = max(TALK_MAX_TURNS_BASE, 1)
    rel_ab = repo.get_relationship(game_id, a_id, b_id)
    rel_ba = repo.get_relationship(game_id, b_id, a_id)
    tension = max(
        rel_ab.suspicion, rel_ab.resentment, rel_ab.fear,
        rel_ba.suspicion, rel_ba.resentment, rel_ba.fear,
    )
    if tension >= TALK_TENSION_THRESHOLD:
        return high
    for c in repo.get_active_conflicts(game_id):
        if a_id in c.participants and b_id in c.participants:
            return high
    return base


def _transcript_memory(turns: List[tuple], self_is_asker: bool, asker_name: str, target_name: str) -> str:
    """把整场对话实录拼成"以自己视角"的一段私密记忆文本(自己说的标【我】)。

    turns 为 [(is_asker, text), ...],按发生顺序。self_is_asker 决定哪些句子是"我"说的。
    """
    lines = []
    for is_asker, text in turns:
        if is_asker == self_is_asker:
            who = "我"
        else:
            who = target_name if self_is_asker else asker_name
        lines.append(f"{who}:{text}")
    return "\n".join(lines)


def _apply_talk(
    repo: Repository, llm: LLMClient, game_id: str, day: int, spec: dict,
    reply: Optional[ConversationReply], agg: EventConsequences,
    on_progress: Optional[Callable[[str], None]],
) -> Optional[str]:
    """主线程应用一次对话:进阶版支持多回合你来我往,落地 B 的感受 + 双方各记全场实录。

    高张力对子(冲突参与者 / 关系对抗维度高)最多进行 TALK_MAX_TURNS_HIGH 个回合,
    普通寒暄保持 1 回合;是否续聊由 B 的 wants_to_continue 与 A 追问时的 continue_talking
    共同决定(任一收口即止)。每个回合的 B→A 感受即时落地,记忆在收尾时一次性整段写入。
    返回旁白可见的一句"谁找了谁 + 神态"(不含内容),首条回复缺失返回 None。
    """
    if reply is None:
        return None
    asker_id, asker_name = spec["asker_id"], spec["asker_name"]
    target_id, target_name = spec["target_id"], spec["target_name"]
    utterance = spec["utterance"]

    # 首个回合(A 说 + B 回)已在并发阶段拿到 reply;先落地 B 的感受,并记入实录。
    turns: List[tuple] = [(True, utterance), (False, reply.reply)]
    _apply_reply_delta(repo, game_id, target_id, asker_id, reply, agg)
    last_reply = reply
    last_memory_write = reply.memory_write
    turns_done = 1
    if on_progress is not None:
        on_progress(
            f"    · 对话 {asker_name} ➜ {target_name}"
            f"｜{asker_name}说:「{utterance}」"
            f"｜{target_name}答:「{last_reply.reply}」"
            f"｜神态:{last_reply.visible_reaction or '神色如常'}"
        )

    # 进阶版:高张力对子继续你来我往(串行 LLM:A 追问依赖 B 上一句)。
    budget = _talk_turn_budget(repo, game_id, asker_id, target_id)
    while turns_done < budget and last_reply.wants_to_continue:
        history = _transcript_memory(turns, self_is_asker=True, asker_name=asker_name, target_name=target_name)
        # A 决定是否继续追问(只为自己说话)
        asker = repo.get_npc(game_id, asker_id)
        world = repo.get_world_state(game_id)
        rel_a2b = repo.get_relationship(game_id, asker_id, target_id)
        f_sys, f_user = prompts.build_conversation_followup_prompt(
            asker, target_id, target_name, history, world, rel_a2b
        )
        try:
            followup = llm.chat_json(f_sys, f_user, ConversationFollowup)
        except Exception:
            followup = None
        if followup is None or not followup.continue_talking or not (followup.utterance or "").strip():
            break
        fu = followup.utterance.strip()
        turns.append((True, fu))

        # B 接着回应(带本场实录,接得上文)
        target = repo.get_npc(game_id, target_id)
        if target is None:
            break
        recent = repo.get_recent_memories(game_id, target_id)
        longterm = repo.get_longterm_memories(game_id, target_id)
        world = repo.get_world_state(game_id)
        rel_b2a = repo.get_relationship(game_id, target_id, asker_id)
        hist2 = _transcript_memory(turns[:-1], self_is_asker=False, asker_name=asker_name, target_name=target_name)
        r_sys, r_user = prompts.build_conversation_reply_prompt(
            target, asker_id, asker_name, fu, recent, longterm, world, rel_b2a, history=hist2
        )
        try:
            nxt = llm.chat_json(r_sys, r_user, ConversationReply)
        except Exception:
            nxt = None
        if nxt is None:
            # A 已说出口但 B 没回:把 A 这句也留在实录里,然后收口
            break
        turns.append((False, nxt.reply))
        _apply_reply_delta(repo, game_id, target_id, asker_id, nxt, agg)
        if nxt.memory_write and nxt.memory_write.content:
            last_memory_write = nxt.memory_write
        last_reply = nxt
        turns_done += 1
        if on_progress is not None:
            on_progress(
                f"      ↳ {asker_name}追问:「{fu}」"
                f"｜{target_name}答:「{nxt.reply}」"
                f"｜神态:{nxt.visible_reaction or '神色如常'}"
            )

    # 双方各记【整场实录】私密记忆(只有当事两人知道);多回合合并为一条,避免刷屏。
    rounds_note = f"(共{turns_done}个回合)" if turns_done > 1 else ""
    memory_engine.write_memory(
        repo, game_id, asker_id, day,
        content=f"我去找{target_name}聊了一场{rounds_note}:\n"
        + _transcript_memory(turns, self_is_asker=True, asker_name=asker_name, target_name=target_name),
        mtype=MemoryType.DIALOGUE, importance=55, related_npc=target_id,
    )
    memory_engine.write_memory(
        repo, game_id, target_id, day,
        content=f"{asker_name}来找我聊了一场{rounds_note}:\n"
        + _transcript_memory(turns, self_is_asker=False, asker_name=asker_name, target_name=target_name),
        mtype=MemoryType.DIALOGUE, importance=55, related_npc=asker_id,
    )
    if last_memory_write and last_memory_write.content:
        memory_engine.write_memory(
            repo, game_id, target_id, day, content=last_memory_write.content,
            mtype=MemoryType.DIALOGUE, importance=last_memory_write.importance,
            emotional_tag=last_memory_write.emotional_tag, related_npc=asker_id,
        )

    talk_word = f"聊了几个回合(共{turns_done}轮)" if turns_done > 1 else "说话"
    return (
        f"{asker_name}主动找{target_name}{talk_word},"
        f"{target_name}神态:{last_reply.visible_reaction or '如常'}"
    )


def _prepare_solo(
    repo: Repository, game_id: str, actor_id: str, actor_name: str,
    verb: ActionVerb, intent: str,
) -> dict:
    """主线程预备一次独自行动:程序裁决曝光、(可选)构造裁决 prompt。"""
    program_exposure = event_engine.compute_exposure_delta(actor_id, verb, "")
    job = None
    if CONV_REFEREE_LLM:
        npc = repo.get_npc(game_id, actor_id)
        world = repo.get_world_state(game_id)
        system, user = prompts.build_solo_referee_prompt(
            npc, _SOLO_HINT[verb], intent, world, _fact_sheet(repo, game_id)
        )
        job = (system, user, SoloOutcome)
    return {
        "kind": "solo", "actor_id": actor_id, "actor_name": actor_name,
        "verb": verb, "intent": intent, "program_exposure": program_exposure,
        "job": job,
    }


def _apply_solo(
    repo: Repository, game_id: str, day: int, spec: dict, outcome: Optional[SoloOutcome],
    agg: EventConsequences, on_progress: Optional[Callable[[str], None]],
) -> str:
    """主线程应用一次独自行动:裁决结果(程序 clamp)→ 落地 → 当事人私密记忆。

    返回旁白可见的一句"谁独自做了什么"(只含可观察行为,不含结果)。
    """
    actor_id, actor_name, verb = spec["actor_id"], spec["actor_name"], spec["verb"]
    narration = f"{actor_name}独自{_SOLO_HINT[verb]}。"
    discovery = ""
    cons = EventConsequences(exposure_delta=spec["program_exposure"])

    if outcome is not None:
        narration = outcome.narration or narration
        discovery = outcome.discovery or ""
        # 裁决器的关系/压力/主线:统一裁剪幅度 + 过滤非法 NPC id;曝光仍以程序规则为准(覆盖)
        valid_ids = {n.id for n in repo.get_all_npcs(game_id)}
        cons = event_engine._clamp_action_consequences(
            outcome.consequences, actor_id, valid_ids
        )
        cons.exposure_delta = spec["program_exposure"]

    _apply_consequences(repo, game_id, cons, agg)

    memory_engine.write_memory(
        repo, game_id, actor_id, day,
        content=f"我今天独自{_SOLO_HINT[verb]}:{narration}"
        + (f"(我发现:{discovery})" if discovery else ""),
        mtype=MemoryType.SYSTEM_EVENT, importance=62,
    )

    if on_progress is not None:
        extra = f" 查到:{discovery}" if discovery else ""
        on_progress(f"    · {actor_name} 独自{_SOLO_HINT[verb]} → {narration}{extra}")
    return f"{actor_name}独自{_SOLO_HINT[verb]},神色专注"


def _humanize_ids(text: str, npc_names: dict) -> str:
    """把旁白文本里偶尔残留的英文 npc_id(如 police/reporter)兜底替换成中文名。

    旁白 LLM 偶尔直接吐出 id 而非角色名;这里在落地前统一替换,保证当日纪事与传闻
    都用中文名。先替换较长的 id,避免一个 id 是另一个的子串时误伤。
    """
    if not text:
        return text
    for npc_id in sorted(npc_names, key=len, reverse=True):
        name = npc_names[npc_id]
        if npc_id and name and npc_id != name:
            text = text.replace(npc_id, name)
    return text


def _run_narrator(
    repo: Repository, llm: LLMClient, game_id: str, day: int,
    acts_lines: List[str], all_notes: list,
    on_progress: Optional[Callable[[str], None]],
) -> None:
    """旁白 AI 把本轮互动白描成"谁和谁有来往 + 神态"(无内容),写给旁观者。"""
    if not acts_lines:
        return
    npc_names = {n.id: n.name for n in repo.get_all_npcs(game_id)}
    valid_ids = set(npc_names)
    system, user = prompts.build_narrator_prompt("\n".join(acts_lines), npc_names)

    # 引擎 B 字段校验 + 失败重生成:旁白须给出具体 event_core;若整批漏填则重试一次。
    obs = None
    for _attempt in range(2):
        try:
            cand = llm.chat_json(system, user, NarratorObservation)
        except Exception:
            cand = None
        if cand is not None and any((n.event_core or "").strip() for n in cand.notes):
            obs = cand
            break
        obs = cand  # 记下最后一次结果(可能 event_core 仍为空)作为兜底
    if obs is None:
        return

    for note in obs.notes:
        note.actors = [a for a in note.actors if a in valid_ids]
        # 兜底:把旁白文本里残留的英文 id 换成中文名(就地改,使当日纪事汇总也一并修正)
        note.event_core = _humanize_ids(note.event_core, npc_names)
        note.demeanor = _humanize_ids(note.demeanor, npc_names)
        # event_core 缺失时退回 demeanor,保证当日纪事仍有可读内容(不阻断主流程)
        core = (note.event_core or "").strip() or note.demeanor
        all_notes.append(note)
        if on_progress is not None:
            tail = f"({note.demeanor})" if note.event_core and note.demeanor else ""
            on_progress(f"    [旁白] {core}{tail}")
        # 把"可观察到的具体动作 + 神态"作为低重要度传闻写给未直接参与者(旁观者也在场)
        seen_by = valid_ids - set(note.actors)
        for nid in seen_by:
            memory_engine.write_memory(
                repo, game_id, nid, day,
                content=f"我瞧见:{core}",
                mtype=MemoryType.RUMOR, importance=30,
            )


def run_social_day(
    repo: Repository,
    llm: LLMClient,
    game_id: str,
    day: int,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Event:
    """推演"对话模式"的一天,返回当日公开纪事 Event(后果已在轮内逐一落地)。

    返回的 Event.consequences 仅为当日汇总(供日志/时间线展示),调用方【不应】
    再次应用,以免重复计数。
    """
    def _p(msg: str) -> None:
        if on_progress is not None:
            on_progress(msg)

    # PR2:只有"在场"(active)的 NPC 参与社交决策池;hiding/away 者退出当日社交。
    # 但 name_of/valid_ids 仍含全部 NPC,便于在场者提及/指涉不在场的人。
    all_npcs = repo.get_all_npcs(game_id)
    npcs = [n for n in all_npcs if n.is_present()]
    name_of = {n.id: n.name for n in all_npcs}
    valid_ids = set(name_of)

    agg = EventConsequences()      # 当日所有已应用后果的汇总(仅展示用)
    all_notes: list = []           # 旁白跨轮观察汇总

    for r in range(1, CONV_ROUNDS + 1):
        _p(f"第{day}天 · 第{r}/{CONV_ROUNDS}轮社交……(并发度 {max(CONV_CONCURRENCY, 1)})")

        # 阶段1)决策:主线程读各自记忆+构造 prompt → 并发发起(互不依赖)→ 同序取回
        world = repo.get_world_state(game_id)
        dec_jobs: List[Optional[tuple]] = []
        for npc in npcs:
            recent = repo.get_recent_memories(game_id, npc.id)
            longterm = repo.get_longterm_memories(game_id, npc.id)
            others = _others_text(repo, game_id, npc.id)
            # PR5:注入"该 NPC 自己的"昨日个人摘要(知识隔离),让今日行动接得上昨天。
            personal_yesterday = memory_engine.build_personal_yesterday_summary(
                repo, game_id, npc.id, day
            )
            # #2:注入该 NPC【本人参与】的冲突当前状态(含已结算),防止聊已了结的旧交易。
            conflict_brief = conflict_engine.describe_conflicts_for_npc(
                repo, game_id, npc.id
            )
            system, user = prompts.build_turn_decision_prompt(
                npc, recent, longterm, world, others, r, CONV_ROUNDS,
                personal_yesterday=personal_yesterday,
                conflict_brief=conflict_brief,
            )
            dec_jobs.append((system, user, TurnDecision))
        decisions = _gather_json(llm, dec_jobs)

        # 阶段2)主线程依据决策预备执行 spec(读 DB + 构造 prompt),区分 对话 / 独自行动
        specs: List[dict] = []
        for npc, d in zip(npcs, decisions):
            if d is None:
                continue
            if d.kind == TurnKind.TALK and d.target in valid_ids and d.target != npc.id:
                spec = _prepare_talk(repo, game_id, npc.id, npc.name, d.target, d.content)
                if spec is not None:
                    specs.append(spec)
                    continue
            # SOLO(含 TALK 目标非法时的兜底):解析动作类别
            verb = _SOLO_VERBS.get((d.solo_verb or "").upper(), ActionVerb.INVESTIGATE)
            specs.append(_prepare_solo(repo, game_id, npc.id, npc.name, verb, d.content or d.reason))

        # 阶段3)并发执行所有 spec 的纯网络调用(回复 + 独自行动裁决互不依赖)
        exec_results = _gather_json(llm, [s["job"] for s in specs])

        # 阶段4)主线程串行应用后果与记忆(规避 sqlite 线程安全),并收集旁白素材
        acts_lines: List[str] = []
        for spec, res in zip(specs, exec_results):
            if spec["kind"] == "talk":
                seen = _apply_talk(repo, llm, game_id, day, spec, res, agg, on_progress)
            else:
                seen = _apply_solo(repo, game_id, day, spec, res, agg, on_progress)
            if seen:
                acts_lines.append(seen)

        # 阶段5)旁白白描本轮(无内容、只神态)→ 写给旁观者
        _run_narrator(repo, llm, game_id, day, acts_lines, all_notes, on_progress)

    # 汇总成当日公开纪事事件:优先用具体的 event_core(剧情日志),退回 demeanor(神态)
    summary = "；".join(
        (n.event_core or "").strip() or n.demeanor for n in all_notes
    ) or "酒馆里平平淡淡的一天,没什么大动静。"
    actors: List[str] = []
    for n in all_notes:
        for a in n.actors:
            if a not in actors:
                actors.append(a)
    event = Event(
        day=day, type=EventType.DAILY_LIFE,
        title=f"第{day}天 · 酒馆见闻", summary=summary,
        actors=actors, consequences=agg, visibility="public",
    )
    return event
