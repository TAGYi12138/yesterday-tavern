"""冲突状态机引擎(P0 · 北极星)。

只负责一桩冲突:录音交易(deal_recording)。赌徒(gambler)手里那段码头录音,
要卖给记者(reporter);老陈(police)在暗处随曝光升级而干预。这桩交易必须在
有限天数内【落槌】(产生不可逆结局),而不是无限期僵持。

设计要点:
- `decide_transition` 是【纯函数】:给定冲突当前态 + 局势输入,返回(新态, 原因,
  后果契约)。便于离线单测(无需 LLM / DB)。
- `tick_conflicts` 是包装器:每天从 repo 读真实局势喂给纯函数,再用统一后果应用器
  落库,并写冲突转移日志。
- 红线:任何结局都【不写】athou_truth_progress;证据损毁/截断类结局【必定】伴随
  secondary_clue_available=True,杜绝死路。
"""
from typing import List, Optional, Tuple

from ..config import (
    DEAL_RECORDING_MAX_STALL_DAYS, EXPOSURE_CRITICAL, EXPOSURE_DANGER,
)
from ..models.conflict import (
    Conflict, ConflictState, is_resolved, next_progress_state,
)
from ..models.consequence import (
    Consequence, MemorySpec, NpcStatusChange, RelationshipChange,
)
from ..storage.repository import Repository
from .consequence import apply_consequence

DEAL_RECORDING = "deal_recording"
# 录音交易的关键参与者:持有者(赌徒)与买家(记者)。两者缺一不可推进。
_HOLDER = "gambler"
_BUYER = "reporter"
_INTERFERER = "police"

# 触发阈值:真相压力或曝光起来后,这桩买卖才会浮出水面被启动。
_TRIGGER_TRUTH_PRESSURE = 30
_TRIGGER_EXPOSURE_STAGES = {"watching", "suppressing", "crisis"}


# ---------------------------------------------------------------------------
# 结局后果工厂(全部走统一契约;红线在 apply_consequence 内再兜一层)
# ---------------------------------------------------------------------------
def _resolution_consequence(state: ConflictState) -> Tuple[Consequence, str]:
    """把某个终态映射为后果契约 + 一句中文摘要。"""
    if state == ConflictState.RESOLVED_SUCCESS:
        return (
            Consequence(
                flags={"recording_delivered": True},
                world_changes={
                    "police_exposure_risk": 20, "truth_pressure": 15,
                    "global_tension": 12,
                },
                relationship_changes=[
                    RelationshipChange(from_npc=_BUYER, to_npc=_HOLDER, trust=15),
                    RelationshipChange(from_npc=_INTERFERER, to_npc=_BUYER, suspicion=20),
                ],
                memories=[
                    MemorySpec(npc_id=_HOLDER, content="那盘录音终于脱手交了出去,钱也到手,但心里发慌。", importance=75, emotional_tag="紧张", related_npc=_BUYER),
                    MemorySpec(npc_id=_BUYER, content="拿到了码头那段录音,手在抖——这东西能掀翻很多人。", importance=80, emotional_tag="亢奋", related_npc=_HOLDER),
                ],
            ),
            "录音成功转交记者:曝光风险与真相压力骤升,老陈对记者起强烈戒心。",
        )
    if state == ConflictState.RESOLVED_BETRAYAL:
        return (
            Consequence(
                flags={"deal_betrayed": True},
                npc_status=[NpcStatusChange(npc_id=_HOLDER, status="away", duration_days=3)],
                world_changes={"global_tension": 10, "truth_pressure": 8},
                relationship_changes=[
                    RelationshipChange(from_npc=_BUYER, to_npc=_HOLDER, trust=-30, resentment=25),
                ],
                memories=[
                    MemorySpec(npc_id=_BUYER, content="说好的交易,对方临阵卷了钱/东西跑了,被狠狠耍了一道。", importance=80, emotional_tag="愤怒", related_npc=_HOLDER),
                ],
            ),
            "一方反水卷款跑路:赌徒避走数日,记者对其信任崩塌、怨恨大涨。",
        )
    if state == ConflictState.RESOLVED_INTERRUPTED:
        return (
            Consequence(
                # 证据交接被截断 → 必给二级线索,杜绝死路(红线#2)
                flags={"deal_interrupted": True, "secondary_clue_available": True},
                npc_status=[NpcStatusChange(npc_id=_HOLDER, status="hiding", duration_days=2)],
                world_changes={
                    "police_exposure_risk": -10, "global_tension": 8, "truth_pressure": 5,
                },
                relationship_changes=[
                    RelationshipChange(from_npc=_HOLDER, to_npc=_INTERFERER, fear=20),
                ],
                memories=[
                    MemorySpec(npc_id=_HOLDER, content="交接时被人当场截了,吓得赶紧躲起来,但还留了个后手没说。", importance=80, emotional_tag="恐惧", related_npc=_INTERFERER),
                ],
            ),
            "交接被当场截断:赌徒蛰伏,老陈暂时压下风声,但留下了二级线索。",
        )
    # RESOLVED_EVIDENCE_COMPROMISED
    return (
        Consequence(
            # 证据损毁/失窃 → 必给二级线索,杜绝死路(红线#2)
            flags={"recording_lost": True, "secondary_clue_available": True},
            world_changes={"global_tension": 6, "truth_pressure": 5},
            relationship_changes=[
                RelationshipChange(from_npc=_BUYER, to_npc=_HOLDER, resentment=10),
            ],
            memories=[
                MemorySpec(npc_id=_HOLDER, content="那盘录音坏了/丢了,关键的一段没了,只剩些零碎旁证。", importance=75, emotional_tag="懊丧", related_npc=None),
            ],
        ),
        "证据损毁/失窃:主录音作废,但仍留下可追的二级线索。",
    )


# ---------------------------------------------------------------------------
# 纯决策函数(可单测)
# ---------------------------------------------------------------------------
def decide_transition(
    conflict: Conflict,
    *,
    exposure_risk: int,
    exposure_stage: str,
    holder_present: bool,
    buyer_present: bool,
    has_progress_signal: bool,
) -> Tuple[ConflictState, str, Optional[Consequence]]:
    """决定本日冲突如何变化。返回(新状态, 原因, 终态后果或 None)。

    优先级:
    1. 已是终态 → 不动。
    2. 老陈逼近到 crisis(曝光≥CRITICAL)→ 当场截断(RESOLVED_INTERRUPTED)。
    3. 关键人物不在场 → 僵持(age+1,不进阶)。
    4. 单状态拖延超过 max_stall_days → 强制落槌(按局势裁定结局)。
    5. 有推进信号 → 进一阶;若已在末位(EXCHANGE_ATTEMPT)再推进 → 成功落槌。
    6. 否则 → 僵持(age+1)。
    """
    state = conflict.state
    if is_resolved(state):
        return state, "已落槌", None

    # 2) 老陈濒临败露,会不惜代价当场截断交易
    if exposure_risk >= EXPOSURE_CRITICAL or exposure_stage == "crisis":
        c, _summary = _resolution_consequence(ConflictState.RESOLVED_INTERRUPTED)
        return ConflictState.RESOLVED_INTERRUPTED, "老陈逼近至摊牌,当场截断交接", c

    # 3) 关键人物缺席:无法推进
    if not (holder_present and buyer_present):
        who = "赌徒" if not holder_present else "记者"
        return state, f"{who}不在场,交易僵持", None

    # 4) 拖延封顶 → 强制结算(北极星:不无限僵持)
    if conflict.age_in_state >= conflict.max_stall_days:
        if exposure_risk >= EXPOSURE_DANGER:
            forced = ConflictState.RESOLVED_INTERRUPTED
            reason = "拖延封顶且曝光高企,交易被迫流产/截断"
        elif state == ConflictState.EXCHANGE_ATTEMPT:
            forced = ConflictState.RESOLVED_SUCCESS
            reason = "拖延封顶,临门一脚强行完成交接"
        else:
            forced = ConflictState.RESOLVED_EVIDENCE_COMPROMISED
            reason = "拖延封顶,夜长梦多,证据出了岔子"
        c, _summary = _resolution_consequence(forced)
        return forced, reason, c

    # 5) 有推进信号:进一阶,或在末位完成交接
    if has_progress_signal:
        if state == ConflictState.EXCHANGE_ATTEMPT:
            c, _summary = _resolution_consequence(ConflictState.RESOLVED_SUCCESS)
            return ConflictState.RESOLVED_SUCCESS, "临门一脚,交接完成", c
        return next_progress_state(state), "交易向前推进了一步", None

    # 6) 无信号:僵持
    return state, "今日无实质进展,交易僵持", None


# ---------------------------------------------------------------------------
# 包装器:读局势 → 决策 → 落库 → 写日志
# ---------------------------------------------------------------------------
def ensure_deal_recording_conflict(repo: Repository, game_id: str, day: int) -> Optional[Conflict]:
    """局势达到触发阈值时,惰性创建录音交易冲突(NEGOTIATING)。

    已存在(无论是否已落槌)则不重复创建——一桩买卖只发生一次。
    """
    existing = repo.get_conflict(game_id, DEAL_RECORDING)
    if existing is not None:
        return existing
    world = repo.get_world_state(game_id)
    triggered = (
        world.truth_pressure >= _TRIGGER_TRUTH_PRESSURE
        or world.exposure_stage in _TRIGGER_EXPOSURE_STAGES
    )
    if not triggered:
        return None
    conflict = Conflict(
        id=DEAL_RECORDING, kind=DEAL_RECORDING,
        participants=[_HOLDER, _BUYER], state=ConflictState.NEGOTIATING,
        age_in_state=0, max_stall_days=DEAL_RECORDING_MAX_STALL_DAYS,
        created_day=day,
    )
    repo.upsert_conflict(game_id, conflict)
    repo.add_conflict_log(
        game_id, DEAL_RECORDING, day, "(none)", ConflictState.NEGOTIATING.value,
        trigger_event="局势浮现", reason="真相压力/曝光升温,码头录音的买卖摆上台面",
    )
    return conflict


def force_resolve_deal_recording(
    repo: Repository, game_id: str, day: int, reason: str
) -> Optional[str]:
    """强制落槌录音交易(供危机倒计时「强制结算」调用)。

    若不存在或已落槌则返回 None。结局按当前曝光局势裁定,后果走统一契约。
    """
    conflict = repo.get_conflict(game_id, DEAL_RECORDING)
    if conflict is None or conflict.is_resolved():
        return None
    world = repo.get_world_state(game_id)
    if world.police_exposure_risk >= EXPOSURE_DANGER or world.exposure_stage == "crisis":
        forced = ConflictState.RESOLVED_INTERRUPTED
    elif conflict.state == ConflictState.EXCHANGE_ATTEMPT:
        forced = ConflictState.RESOLVED_SUCCESS
    else:
        forced = ConflictState.RESOLVED_EVIDENCE_COMPROMISED
    consequence, _summary = _resolution_consequence(forced)
    applied = apply_consequence(repo, game_id, consequence, day, source=DEAL_RECORDING)
    from_state = conflict.state
    conflict.state = forced
    conflict.age_in_state = 0
    repo.upsert_conflict(game_id, conflict)
    repo.add_conflict_log(
        game_id, DEAL_RECORDING, day, from_state.value, forced.value,
        trigger_event="危机强制结算", reason=reason,
        consequence_summary="; ".join(applied),
    )
    return f"[冲突·录音交易] {from_state.value} → {forced.value}(强制结算:{reason})"


def _detect_progress_signal(repo: Repository, game_id: str, day: int) -> bool:
    """从【参与者各自当天的记忆】里探测交易是否往前走了一步(严守知识隔离)。

    任一参与者当天写下含交易关键词的记忆即视为有推进信号。无 LLM 也可工作:
    社交对话/裁决会把这类行为写进当事人记忆,这里只读不解释。
    """
    keywords = ("录音", "交易", "交接", "买卖", "码头", "证据", "钱")
    for npc_id in (_HOLDER, _BUYER):
        for mem in repo.get_memories_by_day(game_id, npc_id, day):
            if any(k in mem.content for k in keywords):
                return True
    return False


def tick_conflicts(repo: Repository, game_id: str, day: int) -> List[str]:
    """每日推进所有未落槌冲突一格(当前仅 deal_recording)。返回可读摘要行。"""
    lines: List[str] = []
    ensure_deal_recording_conflict(repo, game_id, day)

    for conflict in repo.get_active_conflicts(game_id):
        if conflict.id != DEAL_RECORDING:
            continue  # 当前只接管录音交易
        world = repo.get_world_state(game_id)
        holder = repo.get_npc(game_id, _HOLDER)
        buyer = repo.get_npc(game_id, _BUYER)
        new_state, reason, consequence = decide_transition(
            conflict,
            exposure_risk=world.police_exposure_risk,
            exposure_stage=world.exposure_stage,
            holder_present=bool(holder and holder.is_present()),
            buyer_present=bool(buyer and buyer.is_present()),
            has_progress_signal=_detect_progress_signal(repo, game_id, day),
        )
        from_state = conflict.state

        consequence_summary = ""
        if consequence is not None:
            applied = apply_consequence(repo, game_id, consequence, day, source=DEAL_RECORDING)
            consequence_summary = "; ".join(applied)

        if new_state != from_state:
            conflict.state = new_state
            conflict.age_in_state = 0
            repo.upsert_conflict(game_id, conflict)
            repo.add_conflict_log(
                game_id, DEAL_RECORDING, day, from_state.value, new_state.value,
                trigger_event="每日推进", reason=reason,
                consequence_summary=consequence_summary,
            )
            lines.append(f"[冲突·录音交易] {from_state.value} → {new_state.value}({reason})")
        else:
            conflict.age_in_state += 1
            repo.upsert_conflict(game_id, conflict)
            lines.append(f"[冲突·录音交易] 维持 {from_state.value}(第{conflict.age_in_state}天:{reason})")

    return lines
