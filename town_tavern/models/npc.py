"""NPC 数据模型。

固定设定(personality/desire/fear/regret/secret 等)是 NPC 的"灵魂",
任何时候都不允许被 LLM 篡改;运行期可变状态只有 stress / money / current_goal。
"""
from pydantic import BaseModel, Field


class NPC(BaseModel):
    """单个 NPC 的完整档案。"""

    # --- 固定身份 ---
    id: str = Field(..., description="NPC 唯一标识,如 boss/police")
    name: str = Field(..., description="姓名")
    age: int = Field(..., description="年龄")
    job: str = Field(..., description="职业")

    # --- 固定人格设定(不可被 LLM 修改)---
    personality: str = Field(..., description="性格描述")
    desire: str = Field(..., description="核心欲望")
    fear: str = Field(..., description="恐惧")
    regret: str = Field(..., description="悔恨")
    secret: str = Field(..., description="秘密(只有自己知道)")

    # --- 固定的人际/表达指纹(用于增强"真实感",同样不可被 LLM 篡改)---
    first_impression: str = Field(
        default="", description="对初次见到的外乡玩家的第一印象与试探意图"
    )
    speech_style: str = Field(
        default="", description="标志性的说话与行为习惯,让对话有辨识度"
    )

    # --- 运行期可变状态 ---
    current_goal: str = Field(..., description="当前目标(可随剧情变化)")
    stress: int = Field(default=50, description="压力 0-100")
    money: int = Field(default=0, description="持有金钱")
    # PR2:运行期出场状态。active 正常在场;hiding 蛰伏躲藏;away 跑路/离场。
    # hiding/away 期间退出社交决策池;到期(status_until_day)自动回 active。
    status: str = Field(default="active", description="出场状态 active/hiding/away")
    status_until_day: int = Field(
        default=0, description="状态到期天(含):day 达到此值后自动回 active;0 表示无限期"
    )

    def is_present(self) -> bool:
        """是否在社交场中(可被点名对话/可主动行动)。hiding/away 视为不在场。"""
        return self.status == "active"

    def fixed_profile_text(self, audience: str = "") -> str:
        """生成用于 prompt 的固定档案文本(不含运行期状态)。

        first_impression(对「外乡玩家」的最初看法)只在【面对玩家】时注入——
        audience == "player" 才渲染;NPC↔NPC 的决策/对话场景一律不带这句,
        避免出现「对另一个 NPC 也喊外乡人」的身份串台(B2)。
        """
        lines = [
            f"你叫{self.name},{self.age}岁,职业是{self.job}。",
            f"性格:{self.personality}",
            f"你最想要的:{self.desire}",
            f"你最害怕的:{self.fear}",
            f"你的悔恨:{self.regret}",
            f"你的秘密(绝不轻易透露):{self.secret}",
        ]
        if self.speech_style:
            lines.append(f"你的说话与行为习惯:{self.speech_style}")
        if audience == "player" and self.first_impression:
            lines.append(f"你对眼前这个外乡玩家的最初看法:{self.first_impression}")
        return "\n".join(lines)
