"""关系引擎。

负责把结构化的关系增量(RelationshipDelta)应用到仓储层,
并保证取值被裁剪到合法区间(由 Repository 负责 clamp)。
"""
from typing import List

from ..models.event import RelationshipDelta
from ..storage.repository import Repository


def apply_relationship_deltas(
    repo: Repository, game_id: str, deltas: List[RelationshipDelta]
) -> List[str]:
    """应用一组关系增量,返回人类可读的变化描述列表(用于反馈给玩家)。"""
    notes: List[str] = []
    for d in deltas:
        # from/to 通过别名解析,这里直接用字段
        repo.apply_relationship_delta(
            game_id, d.from_npc, d.to_npc,
            trust=d.trust, fear=d.fear, resentment=d.resentment,
            affection=d.affection, suspicion=d.suspicion,
        )
        notes.extend(_describe_delta(d))
    return notes


def _describe_delta(d: RelationshipDelta) -> List[str]:
    """把一条关系增量翻译成中文描述。"""
    target = "玩家" if d.to_npc == "player" else d.to_npc
    parts = []
    mapping = {
        "信任": d.trust, "恐惧": d.fear, "怨恨": d.resentment,
        "好感": d.affection, "怀疑": d.suspicion,
    }
    for label, value in mapping.items():
        if value:
            sign = "上升" if value > 0 else "下降"
            parts.append(f"{d.from_npc} 对 {target} 的{label}{sign}")
    return parts
