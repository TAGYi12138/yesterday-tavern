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
    """冲突状态机的状态。非终态(可推进)在前,RESOLVED_* 为终态(已落槌)。

    单一枚举同时容纳两条冲突线(录音交易 / 账本摊牌),靠每个 kind 各自的
    PROGRESS_ORDER 区分推进路径;DB 仍以字符串存储,新增成员向后兼容。
    """

    # --- 录音交易(deal_recording)非终态 ---
    NEGOTIATING = "NEGOTIATING"                       # 谈判试探:开价、放风
    PARTIAL_PROOF_GIVEN = "PARTIAL_PROOF_GIVEN"       # 给了部分证据(截图/片段)取信
    MONEY_SHOWN = "MONEY_SHOWN"                       # 亮出筹码/钱
    MEETING_SET = "MEETING_SET"                       # 约定了交接的时间地点
    EXCHANGE_ATTEMPT = "EXCHANGE_ATTEMPT"             # 尝试交接(临门一脚)
    # --- 录音交易终态(不可逆结局)---
    RESOLVED_SUCCESS = "RESOLVED_SUCCESS"                       # 录音成功转交记者
    RESOLVED_BETRAYAL = "RESOLVED_BETRAYAL"                     # 一方反水/卷款跑路
    RESOLVED_INTERRUPTED = "RESOLVED_INTERRUPTED"              # 被警察当场截断/扣下
    RESOLVED_EVIDENCE_COMPROMISED = "RESOLVED_EVIDENCE_COMPROMISED"  # 证据损毁/失窃

    # --- 账本摊牌(boss_sister_ledger)非终态 ---
    SUSPICION = "SUSPICION"                           # 淑芬起疑:账本对不上
    LEDGER_FOUND = "LEDGER_FOUND"                     # 翻到了对不上的账本/借据
    PARTIAL_CONFESSION = "PARTIAL_CONFESSION"         # 阿财含糊承认了一部分
    DEMAND_TRUTH = "DEMAND_TRUTH"                     # 淑芬当面逼问全部真相
    # --- 账本摊牌终态(不可逆结局)---
    RESOLVED_TRUST = "RESOLVED_TRUST"                 # 兄妹坦诚相对、决定一起扛
    RESOLVED_BREAKDOWN = "RESOLVED_BREAKDOWN"         # 摊牌崩裂、家裂开了
    RESOLVED_COVERUP = "RESOLVED_COVERUP"             # 阿财继续粉饰、淑芬被瞒住


# 各 kind 的非终态推进顺序(命中推进信号则进下一阶;末位推进即落槌)
PROGRESS_ORDERS: dict[str, List[ConflictState]] = {
    "deal_recording": [
        ConflictState.NEGOTIATING,
        ConflictState.PARTIAL_PROOF_GIVEN,
        ConflictState.MONEY_SHOWN,
        ConflictState.MEETING_SET,
        ConflictState.EXCHANGE_ATTEMPT,
    ],
    "boss_sister_ledger": [
        ConflictState.SUSPICION,
        ConflictState.LEDGER_FOUND,
        ConflictState.PARTIAL_CONFESSION,
        ConflictState.DEMAND_TRUTH,
    ],
}

# 兼容旧引用:默认指向录音交易的推进链。
PROGRESS_ORDER: List[ConflictState] = PROGRESS_ORDERS["deal_recording"]

RESOLVED_STATES = {
    ConflictState.RESOLVED_SUCCESS,
    ConflictState.RESOLVED_BETRAYAL,
    ConflictState.RESOLVED_INTERRUPTED,
    ConflictState.RESOLVED_EVIDENCE_COMPROMISED,
    ConflictState.RESOLVED_TRUST,
    ConflictState.RESOLVED_BREAKDOWN,
    ConflictState.RESOLVED_COVERUP,
}

# #2:把(kind, state)映射成一句"人话状态/结果",注入相关 NPC 决策上下文,
# 防止冲突已结算后 NPC 第二天还像没发生一样继续谈旧交易。
STATE_SENTENCE: dict[str, str] = {
    # 录音交易
    "NEGOTIATING": "录音买卖刚在桌面下试探开价,还没见真章。",
    "PARTIAL_PROOF_GIVEN": "录音的片段/截图已亮了一点取信，但东西还没交。",
    "MONEY_SHOWN": "买家已亮出筹码，钱在桌面上，只差交接。",
    "MEETING_SET": "双方约好了交接的时间地点，箭在弦上。",
    "EXCHANGE_ATTEMPT": "正在尝试交接录音，临门一脚。",
    "RESOLVED_SUCCESS": "录音已经成功转交记者，东西不在原主手里了，别再当成能反复叫卖的筹码。",
    "RESOLVED_BETRAYAL": "交易已经黄了——有人卷款/反水跑路，这桩买卖谈崩了，别再装作还能照旧谈。",
    "RESOLVED_INTERRUPTED": "交易已被老陈当场截断、录音被扣，谁也别再假装那盘录音还在自己手里随时能卖。",
    "RESOLVED_EVIDENCE_COMPROMISED": "关键录音已损毁/失窃作废，主证没了，别再围着那盘录音谈。",
    # 账本摊牌
    "SUSPICION": "淑芬已经起疑：账本对不上，但还没摊开。",
    "LEDGER_FOUND": "对不上的账本/借据已经被翻到，瞒不住了。",
    "PARTIAL_CONFESSION": "阿财已经含糊认了一部分，但没全说。",
    "DEMAND_TRUTH": "淑芬正在当面逼问全部真相，躲不过去了。",
    "RESOLVED_TRUST": "兄妹已经把账本的事摊开说清、决定一起扛，别再彼此试探隐瞒。",
    "RESOLVED_BREAKDOWN": "账本摊牌已经闹崩，家里裂了道口子，别再装作什么都没发生。",
    "RESOLVED_COVERUP": "账本的事被阿财继续粉饰压下、淑芬暂时被瞒住，这一页先翻过去了。",
}


def state_sentence(state: ConflictState) -> str:
    """返回某状态对应的一句人话描述(供注入 NPC 上下文)。"""
    return STATE_SENTENCE.get(state.value, "")


def is_resolved(state: ConflictState) -> bool:
    """该状态是否为终态(已落槌)。"""
    return state in RESOLVED_STATES


def progress_order_for(kind: str) -> List[ConflictState]:
    """返回某冲突 kind 的非终态推进链(未知 kind 退回录音交易链)。"""
    return PROGRESS_ORDERS.get(kind, PROGRESS_ORDERS["deal_recording"])


def next_progress_state(state: ConflictState, kind: str = "deal_recording") -> ConflictState:
    """返回推进一阶后的状态;若已在该 kind 推进链末位则返回自身。

    末位推进意味着"临门一脚成了",由引擎转为某个 RESOLVED_*。
    """
    order = progress_order_for(kind)
    if state not in order:
        return state
    idx = order.index(state)
    if idx + 1 < len(order):
        return order[idx + 1]
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
