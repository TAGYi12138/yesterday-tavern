"""酒馆社交对话模式的结构化模型(Generative Agents 风格)。

核心角色与"代笔"防线:
- NPC 决策(TurnDecision):每轮自行决定找谁说话 / 独自做什么。
- 对话回复(ConversationReply):被点名者【只为自己说话】,只写自己对对方的感受,
  绝不替对方写反应——从根上杜绝"一个人代笔另一个人"的漏洞。
- 旁白观察(NarratorObservation):纯观察者,只记录"谁和谁有来往 + 神态",
  严禁泄露对话具体内容、严禁做价值判断。
- 独自行动裁决(SoloOutcome):当事人不能自判结果,由裁决器读权威世界状态后给出
  (数值再被程序 clamp)。
"""
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from .action import MemoryWrite
from .event import EventConsequences, RelationshipDelta


class TurnKind(str, Enum):
    """一个 NPC 某轮的行为种类。"""

    TALK = "TALK"   # 找某人对话
    SOLO = "SOLO"   # 独自行动(暗中调查 / 掩盖线索 / 回避某人)


class TurnDecision(BaseModel):
    """某个 NPC 在某一轮里的行为决策(LLM 输出)。"""

    kind: TurnKind = Field(default=TurnKind.TALK, description="TALK 找人说话 / SOLO 独自行动")
    target: str = Field(default="", description="TALK 时:对话对象 npc_id;SOLO 时留空")
    content: str = Field(
        default="",
        description="TALK 时:你主动开口要说/要问的话;SOLO 时:你打算独自去做的事",
    )
    solo_verb: str = Field(
        default="",
        description="SOLO 时的动作类别:INVESTIGATE 暗中调查 / CONCEAL 掩盖线索 / AVOID 回避",
    )
    reason: str = Field(default="", description="你为什么这么做(引用记忆/目标)")


class ConversationReply(BaseModel):
    """被点名者对一次搭话的回复(LLM 输出)。

    relationship_delta 必须是【自己 → 对方】,只代表"我此刻对他的感受变化",
    不得替对方写任何东西。
    """

    reply: str = Field(..., description="你的口吻回复,自然中文")
    visible_reaction: str = Field(default="", description="旁人能看到的神情/动作,如 皱眉/凑近压低声音")
    relationship_delta: RelationshipDelta = Field(
        default_factory=lambda: RelationshipDelta(**{"from": "", "to": ""}),
        description="你(from)对发话者(to)的感受变化,-8~8",
    )
    memory_write: Optional[MemoryWrite] = Field(
        default=None, description="若这次对话值得你记住,写下你记住的内容"
    )
    wants_to_continue: bool = Field(
        default=False,
        description="这场对话是否未尽:你觉得还有要追问/回应/交代的就 true,话已说完、无意再聊就 false",
    )


class ConversationFollowup(BaseModel):
    """进阶版多回合对话里,发话者(A)在听到对方(B)的回复后,是否继续追问及追问内容。

    只代表 A 自己接下来要说的话,绝不替 B 写任何反应。continue_talking 为 false
    或 utterance 为空时,这场对话即收口。
    """

    continue_talking: bool = Field(
        default=False,
        description="你是否还想继续这场对话:还有要追问/回应/交代的就 true,话已说尽或没必要再说就 false",
    )
    utterance: str = Field(
        default="",
        description="continue_talking 为 true 时:你接下来要对他说/追问的话(自然中文,60字内);否则留空",
    )


class ObservedInteraction(BaseModel):
    """旁白观察到的一次互动(只含可观察信息,绝无对话内容)。

    引擎 B(B1+B2):在"神态"之外增加一个【可见的核心动作/具体细节】(event_core)——
    一个看得见的物件或动作结果(如『阿财把一张折过的纸条塞给老陈』),让当日纪事
    从"眼神闪烁"的监控录像升级为有事件核、有记忆点的剧情日志。仍严禁泄露对话内容。
    """

    actors: List[str] = Field(default_factory=list, description="被观察到有来往/行动的 npc_id")
    event_core: str = Field(
        default="",
        description="这次来往里【看得见】的核心动作或具体细节(一个物件/一个动作结果),"
        "不含任何听到的对话内容,如 『阿财把一张折过的纸条塞给老陈后匆匆离开』",
    )
    demeanor: str = Field(..., description="可观察到的神态与互动方向,如 『阿财凑近老陈低声说着什么,老陈脸色发白』")


class NarratorObservation(BaseModel):
    """旁白 AI 对一轮社交的公开观察汇总(LLM 输出)。"""

    notes: List[ObservedInteraction] = Field(default_factory=list)


class SoloOutcome(BaseModel):
    """独自行动的裁决结果(裁决 LLM 输出 / 或程序生成)。

    narration 是只有当事人知道的私密经过;consequences 的数值会被程序 clamp;
    discovery 若非空表示查到了线索(写入当事人私密记忆,可能推进主线)。
    """

    narration: str = Field(..., description="这次独自行动的私密经过/结果,一句话")
    discovery: str = Field(default="", description="若查到具体线索就写下,否则留空")
    consequences: EventConsequences = Field(default_factory=EventConsequences)
