"""冲突状态机数据模型(P0)。

一桩"必须落槌"的对峙。当前只用于录音交易(deal_recording):赌徒阿龙手里
那段码头录音,究竟卖给记者、被警察截下、还是中途出岔子——必须在有限天数内
产生【不可逆】的结果,而不是无限期僵持。

红线:任何结局都【不写】玩家真相进度(athou_truth_progress);只产出
flag / 关系 / NPC 离场 / 真相压力,真相仍归玩家亲自揭开。
"""
from enum import Enum
from typing import List

from pydantic import BaseModel, Field


class ConflictState(str, Enum):
    """冲突状态机的状态。前 5 个为非终态(可推进),RESOLVED_* 为终态(已落槌)。"""

    NEGOTIATING = "NEGOTIATING"                       # 谈判试探:开价、放风
    PARTIAL_PROOF_GIVEN = "PARTIAL_PROOF_GIVEN"       # 给了部分证据(截图/片段)取信
    MONEY_SHOWN = "MONEY_SHOWN"                       # 亮出筹码/钱
    MEETING_SET = "MEETING_SET"                       # 约定了交接的时间地点
    EXCHANGE_ATTEMPT = "EXCHANGE_ATTEMPT"             # 尝试交接(临门一脚)
    # --- 终态(不可逆结局)---
    RESOLVED_SUCCESS = "RESOLVED_SUCCESS"                       # 录音成功转交记者
    RESOLVED_BETRAYAL = "RESOLVED_BETRAYAL"                     # 一方反水/卷款跑路
    RESOLVED_INTERRUPTED = "RESOLVED_INTERRUPTED"              # 被警察当场截断/扣下
    RESOLVED_EVIDENCE_COMPROMISED = "RESOLVED_EVIDENCE_COMPROMISED"  # 证据损毁/失窃


# 非终态的推进顺序(命中推进信号则进下一阶;末位推进即落槌)
PROGRESS_ORDER: List[ConflictState] = [
    ConflictState.NEGOTIATING,
    ConflictState.PARTIAL_PROOF_GIVEN,
    ConflictState.MONEY_SHOWN,
    ConflictState.MEETING_SET,
    ConflictState.EXCHANGE_ATTEMPT,
]

RESOLVED_STATES = {
    ConflictState.RESOLVED_SUCCESS,
    ConflictState.RESOLVED_BETRAYAL,
    ConflictState.RESOLVED_INTERRUPTED,
    ConflictState.RESOLVED_EVIDENCE_COMPROMISED,
}


def is_resolved(state: ConflictState) -> bool:
    """该状态是否为终态(已落槌)。"""
    return state in RESOLVED_STATES


def next_progress_state(state: ConflictState) -> ConflictState:
    """返回推进一阶后的状态;若已在末位非终态(EXCHANGE_ATTEMPT)则返回自身。

    末位推进意味着"临门一脚成了",由引擎转为某个 RESOLVED_*(通常成功)。
    """
    if state not in PROGRESS_ORDER:
        return state
    idx = PROGRESS_ORDER.index(state)
    if idx + 1 < len(PROGRESS_ORDER):
        return PROGRESS_ORDER[idx + 1]
    return state


class Conflict(BaseModel):
    """一桩冲突的运行期视图。"""

    id: str = Field(..., description="冲突唯一标识,如 deal_recording")
    kind: str = Field(..., description="冲突类型标签,如 deal_recording")
    participants: List[str] = Field(default_factory=list, description="参与者 npc_id")
    state: ConflictState = Field(default=ConflictState.NEGOTIATING)
    age_in_state: int = Field(default=0, description="在当前状态已停留的天数")
    max_stall_days: int = Field(default=5, description="单状态最多拖延天数,超过强制落槌")
    created_day: int = Field(default=1, description="冲突创建于第几天")

    def is_resolved(self) -> bool:
        return is_resolved(self.state)
