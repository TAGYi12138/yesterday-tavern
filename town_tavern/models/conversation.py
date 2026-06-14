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
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

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


# ===========================================================================
# 多人讨论(群聊子流程):LLM 只产出"意图"与"单句台词",由程序仲裁谁开口、何时收场。
# ===========================================================================
# 发言意图的合法取值(供 prompt 约束与程序判定;非枚举,容忍 LLM 写近义词时退化为 observe)。
SPEAK_INTENTS = (
    "press", "probe", "deflect", "deny", "threaten",
    "appeal", "interrupt", "observe", "silent", "leave",
)
# "施压/质问/威胁/否认"类意图:会让被指向者对发话者升起戒备(suspicion/fear/resentment)。
PRESSURING_INTENTS = {"press", "probe", "threaten", "deny", "interrupt"}


class SpeakIntent(BaseModel):
    """某个在场参与者本轮"想不想说、想怎么说"的意图(LLM 输出)。

    只表达【自己的】发言意愿与切入角度,绝不含具体台词,也不替别人决定。
    private_reason 仅供调试/记忆,不外显;urgency 供程序仲裁谁这一句开口。
    """

    npc_id: str = Field(default="", description="意图归属的 NPC id")
    wants_to_speak: bool = Field(default=False, description="这一轮你是否想开口")
    urgency: int = Field(default=0, description="开口的迫切程度 0-100")
    intent: str = Field(
        default="observe",
        description="发言意图:press/probe/deflect/deny/threaten/appeal/"
        "interrupt/observe/silent/leave 之一",
    )
    target: str = Field(default="", description="你想对谁说(npc_id);无明确对象留空")
    speech_angle: str = Field(default="", description="你想从哪个角度切入(不含具体台词)")
    private_reason: str = Field(default="", description="你为什么想这么说(仅调试/记忆,不外显)")
    should_interrupt: bool = Field(default=False, description="是否想打断当前发言抢话")


class GroupUtterance(BaseModel):
    """被仲裁选中者这一句要说的话(LLM 输出,≤40 字)。

    铁律同 ConversationReply:只为自己说、不替别人说、不说自己不知道的真相、不引用系统 flag。
    """

    speaker: str = Field(default="", description="发话者 npc_id")
    target: str = Field(default="", description="这句话主要说给谁(npc_id);无则留空")
    text: str = Field(default="", description="你这一句台词,自然中文,40字以内")
    tone: str = Field(default="", description="语气,如 冷硬/试探/急切")
    intent: str = Field(default="probe", description="这句话的意图(同 SpeakIntent.intent 取值)")
    visible_reaction: str = Field(default="", description="旁人能看到的你的神态/动作")


@dataclass
class GroupDiscussionState:
    """一场多人讨论的纯内存现场状态(不入库;逐句消息以 group_id 落 timeline)。

    既作为"共享现场(shared_discussion_context)"喂给参与者,也作为程序仲裁/收场判定
    的依据。recent_transcript 只保留最近几句原话(发言者→对象:内容),不含任何隐藏真相。
    """

    group_id: str
    participants: List[str]                      # 在场参与者 npc_id
    location: str = "吧台"
    topic: str = ""                              # 这场讨论围绕的话题(中文一句)
    topic_owner: str = ""                         # 话题主要牵涉到谁(npc_id)
    rounds: int = 0                               # 已进行的轮数
    recent_transcript: List[Tuple[str, str, str]] = field(default_factory=list)
    heat: int = 0                                 # 现场热度 0-100(越高越紧绷)
    silence_rounds: int = 0                       # 连续无人开口的轮数
    last_speaker: str = ""                        # 上一句发话者 npc_id
    last_target: str = ""                         # 上一句指向的对象 npc_id
    last_intent: str = ""                         # 上一句的意图
    spoken_count: Dict[str, int] = field(default_factory=dict)  # 各人已开口次数
    left: List[str] = field(default_factory=list)               # 已离场者 npc_id
    end_reason: str = ""                          # 收场原因(no_speaker/too_many_left/max_rounds/...)

    def present(self) -> List[str]:
        """当前仍在场(未离开)的参与者。"""
        return [p for p in self.participants if p not in self.left]

    def add_utterance(self, speaker: str, target: str, text: str, intent: str) -> None:
        """登记一句已发生的台词,更新 last_*/计数/沉默轮数(保留最近 6 句原话)。"""
        self.recent_transcript.append((speaker, target, text))
        self.recent_transcript = self.recent_transcript[-6:]
        self.last_speaker = speaker
        self.last_target = target
        self.last_intent = intent
        self.spoken_count[speaker] = self.spoken_count.get(speaker, 0) + 1
        self.silence_rounds = 0
