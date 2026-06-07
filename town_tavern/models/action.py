"""玩家行动模型 + LLM 影响评估的结构化输出模型。

原则:LLM 负责想象,程序负责裁决。
所有 LLM 返回的影响都必须落到这些结构化模型里,再由引擎应用。
"""
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .event import ActionVerb, EventConsequences, RelationshipDelta, StressDelta


class ActionType(str, Enum):
    """玩家可执行的 6 种行动。"""

    ASK = "ASK"               # 询问
    TELL = "TELL"             # 告诉某人一条消息
    HELP = "HELP"             # 帮助某人
    THREATEN = "THREATEN"     # 威胁某人
    GIVE_MONEY = "GIVE_MONEY" # 给钱
    REPORT = "REPORT"         # 举报


class PlayerAction(BaseModel):
    """一次玩家行动。"""

    type: ActionType
    target: str = Field(..., description="目标 NPC id")
    content: str = Field(default="", description="行动附带内容,如告知/询问的具体信息")
    amount: int = Field(default=0, description="GIVE_MONEY 时的金额")


class ReflectionResult(BaseModel):
    """NPC 反思的结构化输出:对自身处境的总结 + 可能更新的目标与心境。"""

    npc_id: str
    summary: str = Field(..., description="对自己当前处境的内心总结(将写入长期记忆)")
    updated_goal: str = Field(default="", description="新的当前目标;留空则不改变")
    mood: str = Field(default="", description="当前心境标签,如 焦虑/孤注一掷/麻木")


class MemoryWrite(BaseModel):
    """LLM 评估产生的一条待写入记忆。"""

    npc_id: str
    content: str
    importance: int = 50
    emotional_tag: Optional[str] = None


class ActionImpact(BaseModel):
    """LLM 对玩家行动影响的结构化裁决结果。

    校验铁律:每个行动必须至少产生一个后果(见 has_any_consequence)。
    """

    relationship_changes: List[RelationshipDelta] = Field(default_factory=list)
    stress_changes: List[StressDelta] = Field(default_factory=list)
    memory_writes: List[MemoryWrite] = Field(default_factory=list)
    goal_changes: Dict[str, str] = Field(
        default_factory=dict, description="{npc_id: 新目标}"
    )
    flags: Dict[str, bool] = Field(default_factory=dict)
    athou_progress_delta: int = Field(default=0)
    narration: str = Field(default="", description="给玩家看的行动结果旁白")

    def has_any_consequence(self) -> bool:
        """是否至少产生了一个后果。"""
        return bool(
            self.relationship_changes
            or self.stress_changes
            or self.memory_writes
            or self.goal_changes
            or self.flags
            or self.athou_progress_delta
        )


class DialogueResult(BaseModel):
    """对话接口的结构化返回。"""

    reply: str = Field(..., description="NPC 的口吻回复")
    visible_reaction: str = Field(default="", description="可见的肢体/神情反应")
    relationship_delta: RelationshipDelta = Field(
        default_factory=lambda: RelationshipDelta(**{"from": "", "to": "player"})
    )
    memory_write: Optional[MemoryWrite] = Field(
        default=None, description="本次对话是否值得 NPC 记住"
    )


class NPCIntention(BaseModel):
    """advance_day 时每个 NPC 生成的今日意图。"""

    npc_id: str
    intention: str = Field(..., description="今日想做的事")
    target: str = Field(default="", description="意图针对的对象 id 或 player")
    risk_level: int = Field(default=0, description="风险等级 0-100")
    reason: str = Field(default="", description="动机说明")


class ActionResolutionDraft(BaseModel):
    """LLM 把某 NPC 的核心动作 (actor,verb,target) 展开成
    "这个人今天具体做了什么 + 小后果"的输出结构。

    动作框架(谁对谁做什么)由程序裁决给定,LLM 只负责把它写具体并给出
    小幅后果。曝光增量由程序另行裁决,不信任 LLM。
    """

    narration: str = Field(..., description="这个 NPC 今天具体做了什么(一句话,中文)")
    consequences: EventConsequences = Field(default_factory=EventConsequences)


class ResolvedAction(BaseModel):
    """单个 NPC 把今日意图落成的一次具体行动(含程序裁决后的小后果)。

    这是"群像行动层"的产物:每个 NPC 各有自己今天做的事与后果,
    再由 event_engine 综合众人行动合成当日焦点事件。
    """

    actor_id: str
    verb: ActionVerb
    target_id: str = Field(default="")
    narration: str = Field(default="")
    consequences: EventConsequences = Field(default_factory=EventConsequences)
    visibility: str = Field(default="public", description="public/private")
    weight: float = Field(default=0.0, description="程序计算的重要度,用于挑选焦点事件")
