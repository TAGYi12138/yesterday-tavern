"""玩家已知线索模型(#3)。

【真相归玩家】的护栏:系统内部有很多真实 flag(如 recording_seized、recording_lost),
但这些 flag 的变化【不等于】玩家知道了。玩家只有通过观察 / 询问 / 偷听 / 交易 / 被告知
才会"获得"一条线索,记入 player_known_clues。

因此本模型刻意与系统 flag 解耦:它表达的是"玩家以为自己知道什么、有多大把握",
而不是世界的客观真相——把握度(certainty)可以低、可以错,真假仍由玩家去验证。
"""
from pydantic import BaseModel, Field


class PlayerClue(BaseModel):
    """玩家通过自身行动获得的一条线索。"""

    id: str = Field(..., description="线索唯一标识,如 clue_along_recording_exists")
    title: str = Field(..., description="玩家视角的线索描述,如『阿龙手里可能有一段录音』")
    source: str = Field(default="", description="获取途径:观察/询问/偷听/交易/被告知")
    certainty: int = Field(default=50, description="玩家对该线索的把握 0-100(可低、可错)")
    day_found: int = Field(default=0, description="发现于第几天")
