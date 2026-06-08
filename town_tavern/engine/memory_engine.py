"""记忆引擎。

负责:
- 写入记忆(单 NPC 隔离)
- 压缩记忆:当某 NPC 短期记忆过多时,把较早的若干条合并为一条长期记忆

压缩用 LLM 总结;若 LLM 不可用则退化为简单拼接,保证主流程不被阻塞。
"""
from typing import List, Optional

from ..config import LONGTERM_MEMORY_LIMIT, MEMORY_COMPRESS_THRESHOLD
from ..models.memory import Memory, MemoryType
from ..storage.repository import Repository


def write_memory(
    repo: Repository,
    game_id: str,
    npc_id: str,
    day: int,
    content: str,
    mtype: MemoryType = MemoryType.SYSTEM_EVENT,
    importance: int = 50,
    emotional_tag: Optional[str] = None,
    related_npc: Optional[str] = None,
) -> int:
    """写入一条记忆,返回记忆 id。重要度高的直接标记为长期记忆。"""
    memory = Memory(
        npc_id=npc_id, day=day, type=mtype, content=content,
        importance=importance, emotional_tag=emotional_tag,
        related_npc=related_npc, is_long_term=importance >= 80,
    )
    return repo.add_memory(game_id, memory)


def build_personal_yesterday_summary(
    repo: Repository, game_id: str, npc_id: str, today: int, max_items: int = 3
) -> str:
    """生成「该 NPC 自己」的昨日个人摘要(PR5),严守知识隔离。

    只读这个 NPC 在 (today-1) 当天写下的【自己的】记忆,绝不汇总他人或全局事件,
    挑重要度最高的几条拼成一句提示,供其今日决策"接得上昨天"。无昨日记忆则返回空串。
    """
    if today <= 1:
        return ""
    mems = repo.get_memories_by_day(game_id, npc_id, today - 1)
    if not mems:
        return ""
    mems.sort(key=lambda m: m.importance, reverse=True)
    picked = [m.content.strip() for m in mems[:max_items] if m.content.strip()]
    if not picked:
        return ""
    return "我昨天:" + "；".join(picked)


def compress_memories_if_needed(
    repo: Repository, game_id: str, npc_id: str, day: int, llm=None
) -> bool:
    """若该 NPC 短期记忆超过阈值,压缩最早的一批为一条长期记忆。

    返回是否执行了压缩。
    """
    shortterm = repo.get_shortterm_memories(game_id, npc_id)
    if len(shortterm) <= MEMORY_COMPRESS_THRESHOLD:
        return False

    # 保留最近 LONGTERM_MEMORY_LIMIT 条,压缩更早的
    keep = MEMORY_COMPRESS_THRESHOLD - LONGTERM_MEMORY_LIMIT
    to_compress = shortterm[: len(shortterm) - keep]
    if not to_compress:
        return False

    summary = _summarize(to_compress, llm)
    # 写入压缩后的长期记忆
    repo.add_memory(
        game_id,
        Memory(
            npc_id=npc_id, day=day, type=MemoryType.SYSTEM_EVENT,
            content=f"[记忆梳理] {summary}", importance=70, is_long_term=True,
        ),
    )
    # 删除被压缩的原始短期记忆
    repo.delete_memories([m.id for m in to_compress if m.id is not None])
    return True


def _summarize(memories: List[Memory], llm) -> str:
    """把多条记忆压缩成一句话。优先用 LLM,失败则退化为拼接。"""
    joined = "；".join(m.content for m in memories)
    if llm is None:
        # 退化方案:截断拼接
        return joined[:120]
    try:
        system = "你帮一个角色把零碎记忆梳理成一句凝练的长期印象,只输出中文一句话,80字内。"
        user = f"把以下记忆梳理成一句话:\n{joined}"
        return llm.chat_text(system, user, temperature=0.3)
    except Exception:
        return joined[:120]
