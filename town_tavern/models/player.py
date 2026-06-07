"""玩家自身状态模型。

玩家不再是"无成本操纵世界的幽灵":每天行动点(energy/AP)有限,
给钱要花自己的钱,行为会累积声望(reputation)与嫌疑(suspicion)。
状态以 key-value 形式存于 world_state 表,无需额外建表。
"""
from pydantic import BaseModel, Field


class PlayerState(BaseModel):
    """玩家的运行期状态(每个游戏日 energy 重置)。"""

    money: int = Field(default=3000, description="持有金钱,给钱/收买会消耗")
    reputation: int = Field(default=0, description="小镇对你的观感 0-100")
    suspicion: int = Field(default=0, description="老陈/小镇对你的警惕 0-100")
    energy: int = Field(default=3, description="当日剩余行动点")
    max_energy: int = Field(default=3, description="每日行动点上限")

    def summary_text(self) -> str:
        """生成给玩家看的状态摘要。"""
        return (
            f"行动点 {self.energy}/{self.max_energy} | 金钱 {self.money} | "
            f"声望 {self.reputation} | 嫌疑 {self.suspicion}"
        )
