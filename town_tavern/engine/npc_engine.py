"""NPC 引擎:对话回复、主动搭话 与 今日意图生成。"""
from typing import Optional, Tuple

from ..llm import prompts
from ..llm.client import LLMClient
from ..models.action import DialogueResult, NPCIntention, ReflectionResult
from ..models.memory import MemoryType
from ..models.npc import NPC
from ..storage.repository import Repository
from . import memory_engine, relationship_engine
from .action_engine import ActionCostError

# NPC 主动开口的触发阈值:压力 + 对玩家最强情绪之和达到该值才会主动搭话,
# 避免每次进门都有人缠着玩家。
INITIATIVE_THRESHOLD = 65
# 同一 NPC 当天免费额度用完后,继续追问消耗的行动点
TALK_AP_COST = 1


def talk_to_npc(
    repo: Repository,
    llm: LLMClient,
    game_id: str,
    npc_id: str,
    player_message: str,
) -> DialogueResult:
    """玩家与某 NPC 对话。

    构造该 NPC 视角的上下文(只含其自己的记忆) → LLM 生成结构化回复 →
    应用关系增量并按需写入记忆。
    """
    npc = repo.get_npc(game_id, npc_id)
    if npc is None:
        raise ValueError(f"NPC 不存在: {npc_id}")

    # 行动力:每个 NPC 每天首次闲聊免费,之后继续追问消耗 1 点行动点
    if repo.has_free_talk(game_id, npc_id):
        repo.mark_free_talk(game_id, npc_id)
    elif not repo.spend_player_energy(game_id, TALK_AP_COST):
        raise ActionCostError(
            f"你今天已经和{npc.name}聊过了,再追问要花 {TALK_AP_COST} 点行动点,"
            "但你已经没精力了。(可『离开酒馆 / 快进』开启新的一天)"
        )

    world = repo.get_world_state(game_id)
    rel = repo.get_relationship(game_id, npc_id, "player")
    recent = repo.get_recent_memories(game_id, npc_id)
    longterm = repo.get_longterm_memories(game_id, npc_id)
    today_event = repo.get_event_by_day(game_id, world.current_day)
    today_summary = today_event.summary if today_event else ""

    system, user = prompts.build_dialogue_prompt(
        npc, rel, recent, longterm, world, today_summary, player_message
    )
    result = llm.chat_json(system, user, DialogueResult)

    # 强制关系增量的 from 指向当前 NPC、to 指向 player
    result.relationship_delta.from_npc = npc_id
    result.relationship_delta.to_npc = "player"
    relationship_engine.apply_relationship_deltas(
        repo, game_id, [result.relationship_delta]
    )

    # 记录玩家说过的话(确保 NPC "记得"被问过什么)
    memory_engine.write_memory(
        repo, game_id, npc_id, world.current_day,
        content=f"玩家对我说:{player_message}",
        mtype=MemoryType.DIALOGUE, importance=45, related_npc="player",
    )
    # LLM 认为值得额外记住的内容
    if result.memory_write and result.memory_write.content:
        mw = result.memory_write
        memory_engine.write_memory(
            repo, game_id, npc_id, world.current_day,
            content=mw.content, mtype=MemoryType.DIALOGUE,
            importance=mw.importance, emotional_tag=mw.emotional_tag,
            related_npc="player",
        )

    memory_engine.compress_memories_if_needed(
        repo, game_id, npc_id, world.current_day, llm
    )
    return result


def _pick_initiator(repo: Repository, game_id: str) -> Optional[Tuple[NPC, str]]:
    """挑出最想主动找玩家搭话的 NPC,并推断其动机。

    冲动分 = 压力 + 对玩家最强烈的单一情绪;达到阈值才返回,否则没人主动开口。
    返回 (npc, motive) 或 None。
    """
    best_npc: Optional[NPC] = None
    best_motive = ""
    best_score = -1

    for npc in repo.get_all_npcs(game_id):
        rel = repo.get_relationship(game_id, npc.id, "player")
        # 对玩家最突出的那一种情绪决定搭话的"调子"
        emotions = {
            "怀疑": rel.suspicion,
            "怨恨": rel.resentment,
            "亲近": rel.affection,
            "畏惧": rel.fear,
        }
        top_emo, top_val = max(emotions.items(), key=lambda kv: kv[1])
        score = npc.stress + top_val

        if score <= best_score:
            continue

        # 情绪足够强时由情绪定调,否则归于"压力大、心事重重"
        if top_val >= 30:
            motive = {
                "怀疑": "你对这位常客心存戒备,想旁敲侧击试探他到底知道多少、是不是在打听你的事。",
                "怨恨": "你对他积着不满,忍不住想冷言质问或发作几句。",
                "亲近": "你对他有几分信任,想找个人倾诉或开口求助。",
                "畏惧": "你对他有所忌惮,虽然紧张,却忍不住想探明他的来意。",
            }[top_emo]
        else:
            motive = "你最近压力很大、心事重重,见到熟客,情绪不自觉地流露出来。"

        best_npc, best_motive, best_score = npc, motive, score

    if best_npc is not None and best_score >= INITIATIVE_THRESHOLD:
        return best_npc, best_motive
    return None


def npc_initiate_talk(
    repo: Repository, llm: LLMClient, game_id: str
) -> Optional[Tuple[NPC, DialogueResult]]:
    """玩家进门时,让最有冲动的 NPC 主动开口搭话。

    没有人达到搭话阈值时返回 None。成功时应用关系增量、写入记忆,
    并返回 (开口的 NPC, 对话结果)。
    """
    picked = _pick_initiator(repo, game_id)
    if picked is None:
        return None
    npc, motive = picked

    world = repo.get_world_state(game_id)
    rel = repo.get_relationship(game_id, npc.id, "player")
    recent = repo.get_recent_memories(game_id, npc.id)
    longterm = repo.get_longterm_memories(game_id, npc.id)
    today_event = repo.get_event_by_day(game_id, world.current_day)
    today_summary = today_event.summary if today_event else ""

    system, user = prompts.build_initiative_prompt(
        npc, rel, recent, longterm, world, today_summary, motive
    )
    result = llm.chat_json(system, user, DialogueResult)

    # 强制关系增量指向 (npc -> player) 并落地
    result.relationship_delta.from_npc = npc.id
    result.relationship_delta.to_npc = "player"
    relationship_engine.apply_relationship_deltas(
        repo, game_id, [result.relationship_delta]
    )

    # NPC 记得自己主动开了口(影响后续连续性)
    memory_engine.write_memory(
        repo, game_id, npc.id, world.current_day,
        content=f"我主动找玩家搭了话:{result.reply}",
        mtype=MemoryType.DIALOGUE, importance=40, related_npc="player",
    )
    if result.memory_write and result.memory_write.content:
        mw = result.memory_write
        memory_engine.write_memory(
            repo, game_id, npc.id, world.current_day,
            content=mw.content, mtype=MemoryType.DIALOGUE,
            importance=mw.importance, emotional_tag=mw.emotional_tag,
            related_npc="player",
        )

    memory_engine.compress_memories_if_needed(
        repo, game_id, npc.id, world.current_day, llm
    )
    return npc, result


def generate_intention(
    repo: Repository, llm: LLMClient, game_id: str, npc_id: str
) -> NPCIntention:
    """生成某 NPC 的今日意图。"""
    npc = repo.get_npc(game_id, npc_id)
    if npc is None:
        raise ValueError(f"NPC 不存在: {npc_id}")

    world = repo.get_world_state(game_id)
    recent = repo.get_recent_memories(game_id, npc_id)
    longterm = repo.get_longterm_memories(game_id, npc_id)

    system, user = prompts.build_intention_prompt(npc, recent, longterm, world)
    intention = llm.chat_json(system, user, NPCIntention)
    intention.npc_id = npc_id  # 强制对齐
    return intention


def reflect(
    repo: Repository, llm: LLMClient, game_id: str, npc_id: str, day: int
) -> Optional[ReflectionResult]:
    """让某 NPC 进行一次自我反思:总结处境、写入长期记忆、按需更新当前目标。

    反思结果作为一条高重要度长期记忆留存(importance>=80 → 长期),
    使 NPC 的行为具备跨天的连贯意图,而非每日孤立决策。
    """
    npc = repo.get_npc(game_id, npc_id)
    if npc is None:
        return None

    world = repo.get_world_state(game_id)
    recent = repo.get_recent_memories(game_id, npc_id)
    longterm = repo.get_longterm_memories(game_id, npc_id)

    system, user = prompts.build_reflection_prompt(npc, recent, longterm, world)
    result = llm.chat_json(system, user, ReflectionResult)
    result.npc_id = npc_id  # 强制对齐

    # 反思总结写入长期记忆(高重要度)
    if result.summary:
        memory_engine.write_memory(
            repo, game_id, npc_id, day,
            content=f"[反思] {result.summary}",
            mtype=MemoryType.REFLECTION, importance=82,
            emotional_tag=result.mood or None,
        )
    # 若 NPC 决定调整目标,落地新目标
    if result.updated_goal:
        repo.update_npc_goal(game_id, npc_id, result.updated_goal)

    return result
