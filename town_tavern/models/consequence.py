"""统一后果契约(FOUNDATION)。

所有"会改变世界状态"的事件(危机硬事件、冲突落槌、关键对话裁决……)都产出
同一份 JSON 契约,交给 engine.consequence.apply_consequence 统一落库。好处:
- 单一入口集中拦截红线(绝不写 athou_truth_progress);
- 副作用可被穷举、可测试、可回放;
- 任何调用方都不再各自直接戳数据库。

契约字段:
  flags                 —— 置位的剧情 flag(world_state 里以 "flag_<name>" 存)
  npc_status            —— NPC 离场/蛰伏(active/hiding/away + 持续天数)
  relationship_changes  —— 单向关系五维增量
  world_changes         —— 受控的世界数值增量(athou_truth_progress 被硬性丢弃)
  memories              —— 写给【特定 NPC 自己】的记忆(严守知识隔离)
"""
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

# 世界数值里【绝对禁止】被后果写入的键:真相进度只归玩家亲自揭开。
FORBIDDEN_WORLD_KEYS = frozenset({"athou_truth_progress"})


class NpcStatusChange(BaseModel):
    """让某 NPC 进入/退出某出场状态。"""

    npc_id: str
    status: str = Field(..., description="active/hiding/away")
    # 持续天数(>0);引擎据此换算 status_until_day。0/None 表示用该状态的默认天数。
    duration_days: Optional[int] = Field(default=None)


class RelationshipChange(BaseModel):
    """一条单向关系的五维增量。"""

    from_npc: str
    to_npc: str
    trust: int = 0
    fear: int = 0
    resentment: int = 0
    affection: int = 0
    suspicion: int = 0


class MemorySpec(BaseModel):
    """写给某个 NPC 自己的一条记忆(只它本人知道)。"""

    npc_id: str
    content: str
    importance: int = 50
    emotional_tag: str = ""
    related_npc: Optional[str] = None


class Consequence(BaseModel):
    """统一后果契约。所有字段都可选,缺省即"无此类副作用"。"""

    flags: Dict[str, bool] = Field(default_factory=dict)
    npc_status: List[NpcStatusChange] = Field(default_factory=list)
    relationship_changes: List[RelationshipChange] = Field(default_factory=list)
    # 受控世界数值增量(如 police_exposure_risk / truth_pressure / boss_debt / global_tension)
    world_changes: Dict[str, int] = Field(default_factory=dict)
    memories: List[MemorySpec] = Field(default_factory=list)
