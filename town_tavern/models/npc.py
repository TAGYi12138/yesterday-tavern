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
    # P3:压力饱和(stress>=NPC_SATURATION_STRESS)后落的心理状态,使"压力满"改变决策行为。
    # 空串=未饱和(正常);reckless/paranoid/withdrawn/confession_ready 之一=已被压到变形。
    mental_state: str = Field(default="", description="压力饱和后的心理状态(空=正常)")

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

    def mental_state_text(self) -> str:
        """P3:把心理状态渲染为注入决策提示的一句话。未饱和(空串)返回空。

        压力被烧到顶后,人不再只是"继续日常交谈",而是进入一种失常侧写:
        铤而走险/偏执/退缩/坦白边缘——该句作为"此刻你已被压到什么状态"注入 NPC 决策提示。
        """
        mapping = {
            "reckless": "你已被压到崩溃边缘:不再顾忌后果,倾向孤注一掷、铤而走险,言行比平时冲动。",
            "paranoid": "你已草木皆兵:疑心极重、风声鹤唳都以为冲着自己来,处处试探、防备身边每个人。",
            "withdrawn": "你已身心俱疲:只想缩起来避开所有人,对外界麻木、言语冷淡、提不起劲。",
            "confession_ready": "你已绷到临界:心里那些压了很久的话几乎要脱口而出,只需一个口子就可能吐露真心。",
        }
        text = mapping.get(self.mental_state, "")
        return f"【心理·越界】{text}" if text else ""
