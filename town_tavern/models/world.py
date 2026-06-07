"""世界状态数据模型。

全局 key-value 状态在数据库中以 world_state 表存储,
这里提供一个便捷的运行期视图。
"""
from pydantic import BaseModel, Field

from ..config import (
    DEBT_CRITICAL, DEBT_DANGER, DEBT_WARN,
    EXPOSURE_CRITICAL, EXPOSURE_DANGER, EXPOSURE_WATCH,
)


class WorldState(BaseModel):
    """全局世界状态(运行期视图)。"""

    current_day: int = Field(default=1, description="当前天数")
    global_tension: int = Field(default=20, description="全局紧张度 0-100")
    athou_truth_progress: int = Field(default=0, description="阿土真相进度 0-100")
    police_exposure_risk: int = Field(default=30, description="老陈曝光风险 0-100")
    boss_debt: int = Field(default=300000, description="阿财欠债金额")

    def debt_level(self) -> str:
        """把债务金额映射为危险等级标签。"""
        if self.boss_debt >= DEBT_CRITICAL:
            return "濒临卖店"
        if self.boss_debt >= DEBT_DANGER:
            return "钱庄逼近"
        if self.boss_debt >= DEBT_WARN:
            return "催债趋紧"
        return "尚能周转"

    def exposure_level(self) -> str:
        """把曝光风险映射为危险等级标签。"""
        if self.police_exposure_risk >= EXPOSURE_CRITICAL:
            return "随时摊牌"
        if self.police_exposure_risk >= EXPOSURE_DANGER:
            return "极力掩盖"
        if self.police_exposure_risk >= EXPOSURE_WATCH:
            return "暗中戒备"
        return "暂且安稳"

    def summary_text(self) -> str:
        """生成用于叙事/调试的世界状态摘要(含危险等级标签)。"""
        return (
            f"第{self.current_day}天 | 全局紧张度{self.global_tension} | "
            f"阿土真相进度{self.athou_truth_progress} | "
            f"老陈曝光风险{self.police_exposure_risk}({self.exposure_level()}) | "
            f"阿财欠债{self.boss_debt}({self.debt_level()})"
        )
