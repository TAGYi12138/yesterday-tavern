"""记忆引擎。

负责:
- 写入记忆(单 NPC 隔离)
- 压缩记忆:当某 NPC 短期记忆过多时,把较早的若干条合并为一条长期记忆

压缩用 LLM 总结;若 LLM 不可用则退化为简单拼接,保证主流程不被阻塞。
"""
import logging
from typing import List, Optional

from ..config import LONGTERM_MEMORY_LIMIT, MEMORY_COMPRESS_THRESHOLD
from ..models.memory import Memory, MemoryType
from ..storage.repository import Repository

logger = logging.getLogger(__name__)

# 空/废记忆过滤:杜绝形如 "[记忆梳理]"(压缩 LLM 吐空时产生)的空壳进入长期记忆,
# 污染 NPC 次日决策。阈值按"去掉模板前缀/冒号后的纯内容长度"衡量,取小值只杀真正
# 空壳,不误伤合法短句(中文一句话往往就 6-12 字)。
MIN_MEMORY_CONTENT_LEN = 4
INVALID_MEMORY_CONTENTS = {"", "[记忆梳理]", "[反思]", "None", "null", "none", "NULL"}
_TEMPLATE_TOKENS = ("[记忆梳理]", "[反思]", "：", ":")


def normalize_memory_content(content: Optional[str]) -> str:
    """去首尾空白;None → 空串。"""
    if not content:
        return ""
    return content.strip()


def is_valid_memory_content(content: Optional[str]) -> bool:
    """内容是否值得写入。空、纯模板头、过短(去模板后)一律判为无效。"""
    content = normalize_memory_content(content)
    if content in INVALID_MEMORY_CONTENTS:
        return False
    stripped = content
    for tok in _TEMPLATE_TOKENS:
        stripped = stripped.replace(tok, "")
    stripped = stripped.strip()
    return len(stripped) >= MIN_MEMORY_CONTENT_LEN


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
) -> Optional[int]:
    """写入一条记忆,返回记忆 id;内容空/无效则【跳过不写】并返回 None。

    所有引擎写记忆都走这里,因此空壳记忆在源头被统一拦截,不会进入决策上下文。
    重要度高的直接标记为长期记忆。
    """
    if not is_valid_memory_content(content):
        logger.warning(
            "跳过空/无效记忆: npc=%s day=%s type=%s content=%r",
            npc_id, day, getattr(mtype, "value", mtype), content,
        )
        return None
    memory = Memory(
        npc_id=npc_id, day=day, type=mtype, content=normalize_memory_content(content),
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

    summary = normalize_memory_content(_summarize(to_compress, llm))
    content = normalize_memory_content(f"[记忆梳理] {summary}")
    # LLM 吐空/无效时,绝不写入空壳长期记忆,也【不删除】原始记忆(留待下次再压),
    # 避免既丢数据又留垃圾。
    if not is_valid_memory_content(content):
        logger.warning(
            "压缩产出空/无效,跳过本次压缩(保留原始记忆): npc=%s day=%s", npc_id, day,
        )
        return False
    # 写入压缩后的长期记忆
    repo.add_memory(
        game_id,
        Memory(
            npc_id=npc_id, day=day, type=MemoryType.SYSTEM_EVENT,
            content=content, importance=70, is_long_term=True,
        ),
    )
    # 删除被压缩的原始短期记忆
    repo.delete_memories([m.id for m in to_compress if m.id is not None])
    return True


def purge_invalid_memories(repo: Repository, game_id: Optional[str] = None) -> int:
    """清理库中已存在的空/无效记忆(空内容、纯模板头、去模板后过短)。

    返回删除条数。用于修复历史脏数据(对应一次性清理脚本)。
    """
    rows = repo.list_all_memories(game_id)
    bad_ids = [m.id for m in rows if m.id is not None and not is_valid_memory_content(m.content)]
    if bad_ids:
        repo.delete_memories(bad_ids)
        logger.info("已清理 %d 条空/无效记忆", len(bad_ids))
    return len(bad_ids)


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
