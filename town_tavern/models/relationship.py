"""关系数据模型。

不使用单一好感度,而是拆成五个维度,更贴近真实人际:
- trust       信任
- fear        恐惧
- resentment  怨恨
- affection   好感
- suspicion   怀疑(主要用于 NPC 对玩家)
"""
from pydantic import BaseModel, Field


class Relationship(BaseModel):
    """from_npc 对 to_npc 的单向关系。"""

    from_npc: str = Field(..., description="关系发出方 NPC id")
    to_npc: str = Field(..., description="关系指向方 NPC id 或 'player'")
    trust: int = Field(default=0, description="信任 0-100")
    fear: int = Field(default=0, description="恐惧 0-100")
    resentment: int = Field(default=0, description="怨恨 0-100")
    affection: int = Field(default=0, description="好感 0-100")
    suspicion: int = Field(default=0, description="怀疑 0-100")

    def summary_text(self) -> str:
        """生成用于 prompt 的关系摘要文本。"""
        target = "玩家" if self.to_npc == "player" else self.to_npc
        return (
            f"你对{target}:信任{self.trust} 恐惧{self.fear} "
            f"怨恨{self.resentment} 好感{self.affection} 怀疑{self.suspicion}"
        )
