"""世界状态数据模型。

全局 key-value 状态在数据库中以 world_state 表存储,
这里提供一个便捷的运行期视图。
"""
from typing import List, Tuple

from pydantic import BaseModel, Field

from ..config import (
    DEBT_CRITICAL, DEBT_DANGER, DEBT_WARN,
    EXPOSURE_CRITICAL, EXPOSURE_DANGER, EXPOSURE_WATCH,
)

# 真相压力阶段(C1):latent(潜伏)→ stirring(暗涌)→ closing_in(逼近)→ boiling(沸点)
# 仅由 NPC 自运行累积,用于加剧危机局势;不代表玩家已揭开真相。
TRUTH_STAGE_THRESHOLDS = [
    (0, "latent"),
    (30, "stirring"),
    (55, "closing_in"),
    (80, "boiling"),
]

# ---------------------------------------------------------------------------
# 引擎 A:阶段阈值表(复用既有危险阈值,升序排列:(下界, 阶段名))
# ---------------------------------------------------------------------------
# 曝光风险阶段:normal → watching(留意)→ suppressing(主动压制)→ crisis(灭口/摊牌)
EXPOSURE_STAGE_THRESHOLDS: List[Tuple[int, str]] = [
    (0, "normal"),
    (EXPOSURE_WATCH, "watching"),
    (EXPOSURE_DANGER, "suppressing"),
    (EXPOSURE_CRITICAL, "crisis"),
]
# 债务阶段:stable(周转)→ pressing(催债趋紧)→ closing(钱庄逼近)→ seizing(濒临卖店/查封)
DEBT_STAGE_THRESHOLDS: List[Tuple[int, str]] = [
    (0, "stable"),
    (DEBT_WARN, "pressing"),
    (DEBT_DANGER, "closing"),
    (DEBT_CRITICAL, "seizing"),
]


def stage_of(
    value: int,
    thresholds: List[Tuple[int, str]],
    current_stage: str = "",
    hysteresis: int = 0,
) -> str:
    """通用阶段机:根据 value 落在哪个阈值区间返回阶段名,并带迟滞(hysteresis)。

    迟滞规则:升阶/持平立即生效;降阶时,必须跌破【当前阶段下界 - hysteresis】
    才允许退阶,避免数值在临界点反复抖动。current_stage 为上一次记录的阶段。
    """
    names = [n for _, n in thresholds]
    # 自然阶段:value 满足的最高下界
    natural_idx = 0
    for i, (lo, _name) in enumerate(thresholds):
        if value >= lo:
            natural_idx = i

    if current_stage not in names:
        return names[natural_idx]

    cur_idx = names.index(current_stage)
    if natural_idx >= cur_idx:
        # 升阶或持平:立即生效
        return names[natural_idx]
    # 降阶:需跌破"当前阶段下界 - 迟滞"才放行
    cur_lo = thresholds[cur_idx][0]
    if value < cur_lo - hysteresis:
        return names[natural_idx]
    return current_stage


class WorldState(BaseModel):
    """全局世界状态(运行期视图)。"""

    current_day: int = Field(default=1, description="当前天数")
    global_tension: int = Field(default=20, description="全局紧张度 0-100")
    # athou_truth_progress 即"玩家揭开真相的进度":只因玩家行动增加,无玩家时恒为 0(C1)
    athou_truth_progress: int = Field(default=0, description="阿土真相进度(玩家驱动)0-100")
    police_exposure_risk: int = Field(default=30, description="老陈曝光风险 0-100")
    boss_debt: int = Field(default=300000, description="阿财欠债金额")
    # 引擎 C(C1):真相压力——NPC 自运行可累积(有平台上限),只驱动危机局势,不揭真相
    truth_pressure: int = Field(default=0, description="真相压力(NPC 驱动)0-100")
    # 引擎 A:带迟滞的阶段标签(由 _daily_world_tick 每天结算后持久化)
    exposure_stage: str = Field(default="normal", description="曝光风险阶段(带迟滞)")
    debt_stage: str = Field(default="stable", description="债务阶段(带迟滞)")
    truth_stage: str = Field(default="latent", description="真相压力阶段(带迟滞)")
    # #3:曝光阶段【通用】连续天数——当前 exposure_stage 已持续几天(任意阶段都计数)。
    # 每日由 update_exposure_stage 统一维护:阶段不变则 +1,阶段切换则重置为 1。
    exposure_stage_days: int = Field(default=0, description="当前曝光阶段已持续的天数(通用)")
    # PR4(保留兼容):危机连续天数。语义 = exposure_stage_days if stage==crisis else 0,
    # 由 update_exposure_stage 派生维护,杜绝"suppressing 阶段却 crisis_days>0"的语义错位。
    crisis_days: int = Field(default=0, description="连续处于曝光危机阶段的天数(派生兼容字段)")
    # #4:危机生命周期阶段机——none(无危机)→ active(危机中,逐级硬事件)→
    # cooling(烧到顶后降温,主动压低曝光)→ aftermath(余波宽限,不再强触发硬事件)→ none。
    crisis_phase: str = Field(default="none", description="危机生命周期阶段:none/active/cooling/aftermath")

    def tension_target(self) -> int:
        """PR6:按当前三条态势【阶段】算出全局紧张度的"目标档位"(0-100)。

        紧张度不再只靠事件 +1/+3 单调累积,而是朝一个由 曝光/债务/真相压力 阶段
        决定的目标值平滑靠拢(取三者档位的最大值)。事件可制造短时高于目标的尖峰,
        随后自然回落到目标附近,既"会喘气"又能反映真实局势。
        """
        exposure_tier = {"normal": 15, "watching": 40, "suppressing": 65, "crisis": 85}
        debt_tier = {"stable": 10, "pressing": 30, "closing": 55, "seizing": 75}
        truth_tier = {"latent": 10, "stirring": 35, "closing_in": 55, "boiling": 80}
        return max(
            exposure_tier.get(self.exposure_stage, 15),
            debt_tier.get(self.debt_stage, 10),
            truth_tier.get(self.truth_stage, 10),
        )

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

    def debug_state_text(self) -> str:
        """调试视图:展示自运行变量及其阶段(仅供 CLI/日志,不进 NPC 提示词)。"""
        return (
            f"[调试] 曝光{self.police_exposure_risk}/阶段{self.exposure_stage} | "
            f"债务{self.boss_debt}/阶段{self.debt_stage} | "
            f"真相压力{self.truth_pressure}/阶段{self.truth_stage} | "
            f"玩家真相进度{self.athou_truth_progress}"
        )

    def crisis_directive(self) -> str:
        """按当前曝光/债务【阶段】生成给 NPC 决策用的"局势压力"指令。

        这是对话模式里"阶段事件池"的等价物:不直接塞事件,而是把"此刻老陈/阿财
        应当采取的姿态"作为态势压力注入决策提示,引导 NPC 自然做出对应行为。
        注意:这只推动【局势】(防守/施压),绝不揭示阿土真相——真相仍归玩家。
        """
        lines: List[str] = []
        # #4:危机降温/余波期——局势已过顶峰,老陈收敛锋芒;此时【覆盖】曝光阶段施压口径,
        # 但债务/真相暗流仍各自照常(它们与危机生命周期相互独立)。
        if self.crisis_phase == "cooling":
            lines.append("【局势·老陈】风声烧到顶后开始收敛,老陈暂收锋芒、不再主动出击,转为观望舔伤。")
        elif self.crisis_phase == "aftermath":
            lines.append("【局势·老陈】风波刚过、余波未平,老陈按兵不动避风头,众人惊魂未定、暂得喘息。")
        else:
            # 曝光阶段 → 老陈(police)的防守姿态逐级升级
            exposure_map = {
                "watching": "老陈已起疑,会暗中监视、试探、敲打可能知情的人,但尚未撕破脸。",
                "suppressing": "老陈感到威胁,会主动压制证人、核查可疑者底细、威胁相关人闭嘴或封店。",
                "crisis": "老陈濒临败露,会不择手段灭口软肋、施压知情者、甚至准备栽赃或摊牌(但不会主动交代阿土的真相)。",
            }
            if self.exposure_stage in exposure_map:
                lines.append("【局势·老陈】" + exposure_map[self.exposure_stage])
        # 债务阶段 → 阿财(boss)/相关人的反应逐级升级
        debt_map = {
            "pressing": "债务催得紧,阿财开始翻账本、四处周转,情绪焦躁。",
            "closing": "讨债人逼近,阿财考虑借新还旧,甚至动了出卖他人秘密换钱的念头。",
            "seizing": "酒馆濒临被接管/查封,阿财孤注一掷,什么都做得出来。",
        }
        if self.debt_stage in debt_map:
            lines.append("【局势·阿财】" + debt_map[self.debt_stage])
        # 真相压力阶段 → 整体暗流强度(知情者更躁动,但谁都不会主动点破真相)
        truth_map = {
            "stirring": "镇上隐隐有些关于那桩旧事的暗涌,知情的人开始坐立不安。",
            "closing_in": "线索似乎在悄悄收拢,几个知情者彼此提防、试探,气氛越来越紧。",
            "boiling": "真相像快烧开的水,知情者人人自危、各怀心事,但谁也不敢、不愿先挑破。",
        }
        if self.truth_stage in truth_map:
            lines.append("【局势·暗流】" + truth_map[self.truth_stage])
        return "\n".join(lines)
