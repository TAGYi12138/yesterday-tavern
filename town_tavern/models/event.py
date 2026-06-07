"""事件数据模型 + 事件后果结构。

事件类型固定枚举,LLM 只负责填充细节,不能凭空发明新的事件类别,
以此防止"剧情每天乱飞"。
"""
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class EventType(str, Enum):
    """固定事件模板类型(现已退化为"分类标签",真正驱动内容的是 ActionVerb)。"""

    DEBT_PRESSURE = "DEBT_PRESSURE"                   # 债务催逼
    POLICE_WARNING = "POLICE_WARNING"                 # 警察警告
    REPORTER_INVESTIGATION = "REPORTER_INVESTIGATION" # 记者调查
    GAMBLER_BLACKMAIL = "GAMBLER_BLACKMAIL"           # 赌徒勒索
    SISTER_SUSPICION = "SISTER_SUSPICION"             # 妹妹起疑
    ATHOU_CLUE = "ATHOU_CLUE"                         # 阿土线索浮现
    DAILY_LIFE = "DAILY_LIFE"                         # 酒馆社交日常(对话模式的当日纪事)


class ActionVerb(str, Enum):
    """事件的"动作语法":谁对谁做了什么。

    用固定动作词表 + 合法角色组合(actor × verb × target)代替原先的 6 种
    固定剧情模板,组合空间远大于模板,涌现更强,但仍由程序裁决、不致失控。
    """

    CONFRONT = "CONFRONT"          # 当面对峙/质问
    THREATEN = "THREATEN"          # 威胁施压
    BRIBE = "BRIBE"                # 收买/塞钱安抚
    PERSUADE = "PERSUADE"          # 劝说/拉拢
    INVESTIGATE = "INVESTIGATE"    # 暗中调查/打探
    CONCEAL = "CONCEAL"            # 掩盖/销毁线索
    CONFIDE = "CONFIDE"            # 私下倾诉/求助
    DEAL = "DEAL"                  # 谈交易/讲条件
    AVOID = "AVOID"                # 回避/疏远
    EXPOSE = "EXPOSE"              # 揭露/摊牌


class RelationshipDelta(BaseModel):
    """单条关系变化(增量)。"""

    from_npc: str = Field(..., alias="from")
    to_npc: str = Field(..., alias="to")
    trust: int = 0
    fear: int = 0
    resentment: int = 0
    affection: int = 0
    suspicion: int = 0

    model_config = {"populate_by_name": True}


class StressDelta(BaseModel):
    """单个 NPC 的压力变化。"""

    npc: str
    delta: int


class EventConsequences(BaseModel):
    """事件造成的后果集合。"""

    relationships: List[RelationshipDelta] = Field(default_factory=list)
    stress: List[StressDelta] = Field(default_factory=list)
    flags: Dict[str, bool] = Field(default_factory=dict)
    athou_progress_delta: int = Field(default=0, description="阿土主线进度增量")
    exposure_delta: int = Field(
        default=0, description="老陈曝光风险增量(由程序按动作裁决,非 LLM 填写)"
    )


class Event(BaseModel):
    """一天发生的主要事件。"""

    id: Optional[int] = Field(default=None)
    day: int
    type: EventType
    title: str
    summary: str
    actors: List[str] = Field(default_factory=list, description="涉及的 NPC id 列表")
    consequences: EventConsequences = Field(default_factory=EventConsequences)
    visibility: str = Field(default="public", description="public/private")


class EventDraft(BaseModel):
    """LLM 生成事件时的输出结构(不含 id/day,由引擎补全)。

    type 与 visibility 现由程序根据动作语法(verb)裁决,故给默认值,
    LLM 只需产出 title/summary/actors/consequences。
    """

    type: EventType = Field(default=EventType.ATHOU_CLUE)
    title: str
    summary: str
    actors: List[str] = Field(default_factory=list)
    consequences: EventConsequences = Field(default_factory=EventConsequences)
    visibility: str = Field(default="public")

    def to_event(self, day: int) -> Event:
        """补全 day 字段,转为正式 Event。"""
        return Event(
            day=day, type=self.type, title=self.title, summary=self.summary,
            actors=self.actors, consequences=self.consequences,
            visibility=self.visibility,
        )
