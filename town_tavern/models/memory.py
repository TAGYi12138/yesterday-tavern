"""记忆数据模型。

记忆按 NPC 单独存储,构造 prompt 时只读取该 NPC 自己的记忆,
确保"每个 NPC 只能记住自己知道的事"。
"""
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MemoryType(str, Enum):
    """记忆类型枚举。"""

    PLAYER_ACTION = "player_action"   # 玩家行为
    SECRET = "secret"                 # 秘密
    CONFLICT = "conflict"             # 冲突
    FAVOR = "favor"                   # 恩惠
    BETRAYAL = "betrayal"             # 背叛
    RUMOR = "rumor"                   # 传闻
    SYSTEM_EVENT = "system_event"     # 系统事件
    DIALOGUE = "dialogue"             # 对话记忆
    REFLECTION = "reflection"         # 自我反思(对处境的内心总结)


class Memory(BaseModel):
    """单条记忆。"""

    id: Optional[int] = Field(default=None, description="数据库自增主键")
    npc_id: str = Field(..., description="记忆归属的 NPC id")
    day: int = Field(..., description="发生在第几天")
    type: MemoryType = Field(default=MemoryType.SYSTEM_EVENT, description="记忆类型")
    content: str = Field(..., description="记忆内容")
    importance: int = Field(default=50, description="重要程度 0-100")
    emotional_tag: Optional[str] = Field(default=None, description="情绪标签,如紧张/愤怒")
    related_npc: Optional[str] = Field(default=None, description="关联的 NPC id 或 player")
    is_long_term: bool = Field(default=False, description="是否为长期记忆")
