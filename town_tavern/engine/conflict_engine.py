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
    CONFLICT_MAX_DAYS, CONFLICT_MIN_DAYS, DEAL_RECORDING_MAX_STALL_DAYS,
    EXPOSURE_CRITICAL, EXPOSURE_DANGER,
)
from ..models.conflict import (
    Conflict, ConflictState, is_resolved, next_progress_state, state_sentence,
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
# 第二条冲突:账本摊牌(boss_sister_ledger)—— 阿财(瞒债)↔ 淑芬(查账)的家庭线。
# 复用同一套状态机骨架与统一后果应用器,吃同样的红线(绝不写真相进度)与节奏地板。
# ---------------------------------------------------------------------------
BOSS_SISTER_LEDGER = "boss_sister_ledger"
_BOSS = "boss"        # 阿财:瞒着妹妹欠了地下钱庄的钱
_SISTER = "sister"    # 淑芬:早觉得账本对不上,迟早摊牌
# 触发阈值:家里气氛(全局紧张度)起来、或阿财压力高到藏不住时,这条线浮现。
_LEDGER_TRIGGER_TENSION = 25
_LEDGER_TRIGGER_BOSS_STRESS = 75


# ---------------------------------------------------------------------------
# #3:冲突落槌后【强制重写参与者目标】(硬约束),根治"结算后目标回流"。
# ---------------------------------------------------------------------------
# 软提示(STATE_SENTENCE 注入 prompt)挡不住 LLM 复读旧交易——current_goal 是
# 持久字段,落槌时若不改写,NPC 第二天仍会照着旧目标(如"卖旧录音跑路")行动。
# 这里在终态【确定性】地把相关角色目标改写成"只能围绕残局/二级线索/补救",
# 从源头杜绝回流。按 (终态 → {npc_id: 新目标}) 映射,只改该冲突的真正参与者。
_RESOLUTION_GOALS: dict[ConflictState, dict[str, str]] = {
    # --- 录音交易 ---
    ConflictState.RESOLVED_SUCCESS: {
        _HOLDER: "录音已脱手、钱已到手,如今只想拿钱避风头、撇清干系,绝不再提那盘带子、更不再找人兜售它。",
        _BUYER: "录音已到手,接下来只想着核实内容、保护线人、谋划怎么用,不再纠缠那桩交易本身。",
    },
    ConflictState.RESOLVED_BETRAYAL: {
        _HOLDER: "交易黄了、人也得远遁避祸,只想躲过这阵风声,绝不回头再碰这桩买卖。",
        _BUYER: "被反水坑了一道,转而另寻突破口、设法追回损失,绝不再信旧渠道、不再谈那盘录音。",
    },
    ConflictState.RESOLVED_INTERRUPTED: {
        _HOLDER: "录音当场被老陈截下,如今只能蛰伏避风头、从残局里找回点筹码,绝不再假装那盘带子还在自己手里能卖。",
        _BUYER: "交接被截、录音被扣,只能另找二级线索接着查,不再围着那盘已经没了的录音打转。",
    },
    ConflictState.RESOLVED_EVIDENCE_COMPROMISED: {
        _HOLDER: "主证已损毁作废,只能认栽避风头,不再围着那盘没用的录音兜售。",
        _BUYER: "录音作废,改从其他人、二手线索上找补,不再纠结那桩已经黄掉的交易。",
    },
    # --- 账本摊牌 ---
    ConflictState.RESOLVED_TRUST: {
        _BOSS: "账本的事已和妹妹摊开,转而和她一起想办法应对债务,不再瞒她、不再独自硬扛。",
        _SISTER: "哥哥已经坦白账本,转而和他一起面对欠债,不再独自查账试探。",
    },
    ConflictState.RESOLVED_BREAKDOWN: {
        _BOSS: "和妹妹闹崩了,只能各自硬扛,先顾着自保、想法子填债窟窿。",
        _SISTER: "和哥哥闹崩、心也冷了,转向自保与自己的打算,不再指望这个家。",
    },
    ConflictState.RESOLVED_COVERUP: {
        _BOSS: "账本暂时糊弄过去、瞒住了妹妹,接下来只想着设法填账、别再露馅。",
        _SISTER: "账本的事被哥哥搪塞了过去,我暂且作罢,但心里存着疑,留意往后的破绽。",
    },
}


def rewrite_goals_on_resolution(
    repo: Repository, game_id: str, conflict: Conflict, resolved_state: ConflictState
) -> List[str]:
    """#3:冲突落槌时,把【该冲突参与者】的 current_goal 硬改写为终态后的新目标。

    只改这桩冲突真正的参与者(知识隔离,不殃及旁人),且只在有映射的终态执行。
    返回被改写的可读说明行(供日志/摘要),无改写则返回空列表。
    """
    goals = _RESOLUTION_GOALS.get(resolved_state)
    if not goals:
        return []
    lines: List[str] = []
    for npc_id in conflict.participants:
        new_goal = goals.get(npc_id)
        if not new_goal:
            continue
        repo.update_npc_goal(game_id, npc_id, new_goal)
        npc = repo.get_npc(game_id, npc_id)
        name = npc.name if npc else npc_id
        lines.append(f"{name}的目标已随结算改写")
    return lines


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
    days_since_created: int = 999,
    min_days: int = 0,
) -> Tuple[ConflictState, str, Optional[Consequence]]:
    """决定本日冲突如何变化。返回(新状态, 原因, 终态后果或 None)。

    优先级:
    1. 已是终态 → 不动。
    2. 老陈逼近到 crisis(曝光≥CRITICAL)→ 当场截断(RESOLVED_INTERRUPTED)。
    3. 关键人物不在场 → 僵持(age+1,不进阶)。
    4. 单状态拖延超过 max_stall_days → 强制落槌(按局势裁定结局)。
    5. 有推进信号 → 进一阶;若已在末位(EXCHANGE_ATTEMPT)再推进 → 成功落槌。
    6. 否则 → 僵持(age+1)。

    节奏地板(min_days):冲突自创建起未满 min_days 时,【不允许】通过"临门一脚"提早
    落槌(防止世界爆太快);但拖延封顶(北极星上限)与老陈危机截断不受此限制。
    默认 min_days=0,即不设地板(历史行为,保持向后兼容)。
    """
    state = conflict.state
    if is_resolved(state):
        return state, "已落槌", None

    # 2) 老陈濒临败露,会不惜代价当场截断交易(危机截断不受节奏地板限制)
    if exposure_risk >= EXPOSURE_CRITICAL or exposure_stage == "crisis":
        c, _summary = _resolution_consequence(ConflictState.RESOLVED_INTERRUPTED)
        return ConflictState.RESOLVED_INTERRUPTED, "老陈逼近至摊牌,当场截断交接", c

    # 3) 关键人物缺席:无法推进
    if not (holder_present and buyer_present):
        who = "赌徒" if not holder_present else "记者"
        return state, f"{who}不在场,交易僵持", None

    # 4) 拖延封顶 → 强制结算(北极星:不无限僵持,封顶是硬上限,不受地板限制)
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
            # 节奏地板:未满 min_days 时不让交易提早落槌,在临门一脚处再压一压。
            if days_since_created < min_days:
                return state, "万事俱备,但火候未到,交接再压一压", None
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
    # #3:落槌即硬改写参与者目标,杜绝次日仍复读旧交易。
    rewrite_goals_on_resolution(repo, game_id, conflict, forced)
    repo.add_conflict_log(
        game_id, DEAL_RECORDING, day, from_state.value, forced.value,
        trigger_event="危机强制结算", reason=reason,
        consequence_summary="; ".join(applied),
    )
    return f"[冲突·录音交易] {from_state.value} → {forced.value}(强制结算:{reason})"


def _ledger_resolution_consequence(state: ConflictState) -> Tuple[Consequence, str]:
    """账本摊牌的终态 → 后果契约 + 一句中文摘要(同样不写真相进度)。"""
    if state == ConflictState.RESOLVED_TRUST:
        return (
            Consequence(
                flags={"ledger_reconciled": True},
                world_changes={"global_tension": -8},
                relationship_changes=[
                    RelationshipChange(from_npc=_SISTER, to_npc=_BOSS, trust=25, resentment=-10),
                    RelationshipChange(from_npc=_BOSS, to_npc=_SISTER, trust=15),
                ],
                memories=[
                    MemorySpec(npc_id=_BOSS, content="账本的事终于跟妹妹摊开说了,她没走,说一起扛——这些年第一次松口气。", importance=80, emotional_tag="释然", related_npc=_SISTER),
                    MemorySpec(npc_id=_SISTER, content="哥哥总算把欠债的事说清了,我气他瞒我,但更怕这个家散——决定一起想办法。", importance=80, emotional_tag="心疼", related_npc=_BOSS),
                ],
            ),
            "兄妹坦诚相对、决定一起扛:家里那道裂缝暂时弥合,全局紧张度回落。",
        )
    if state == ConflictState.RESOLVED_BREAKDOWN:
        return (
            Consequence(
                flags={"family_breakdown": True},
                world_changes={"global_tension": 12},
                relationship_changes=[
                    RelationshipChange(from_npc=_SISTER, to_npc=_BOSS, trust=-30, resentment=25),
                ],
                memories=[
                    MemorySpec(npc_id=_SISTER, content="他瞒了我这么久、欠了这么多——我们大吵一架,我不知道这个家还算不算家。", importance=85, emotional_tag="心碎", related_npc=_BOSS),
                    MemorySpec(npc_id=_BOSS, content="妹妹把话挑明了,我没接住,她摔门走了,酒馆里头一次这么静。", importance=85, emotional_tag="懊悔", related_npc=_SISTER),
                ],
            ),
            "摊牌崩裂:兄妹信任骤降、怨恨升起,家裂开一道口子,全局紧张度上扬。",
        )
    # RESOLVED_COVERUP
    return (
        Consequence(
            flags={"ledger_coverup": True},
            world_changes={"global_tension": 4},
            relationship_changes=[
                RelationshipChange(from_npc=_BOSS, to_npc=_SISTER, fear=8),
            ],
            memories=[
                MemorySpec(npc_id=_BOSS, content="又把账糊弄过去了,妹妹半信半疑——这层窗户纸早晚还得破,但今天先撑住。", importance=70, emotional_tag="心虚", related_npc=_SISTER),
            ],
        ),
        "阿财继续粉饰、淑芬暂被瞒住:这一页先翻过去,但窗户纸迟早要破。",
    )


def decide_ledger_transition(
    conflict: Conflict,
    *,
    tension: int,
    boss_present: bool,
    sister_present: bool,
    has_progress_signal: bool,
    days_since_created: int = 999,
    min_days: int = 0,
) -> Tuple[ConflictState, str, Optional[Consequence]]:
    """账本摊牌的纯决策函数(可单测,无需 LLM/DB)。返回(新状态, 原因, 终态后果或 None)。

    优先级:
    1. 已落槌 → 不动。
    2. 兄妹任一不在场 → 僵持(无法摊牌)。
    3. 单状态拖延封顶 → 强制落槌(北极星硬上限)。
    4. 有推进信号 → 进一阶;在末位(DEMAND_TRUTH)再推进 → 按张力裁定结局。
    5. 否则僵持。
    节奏地板同录音线:未满 min_days 不允许提早从 DEMAND_TRUTH 落槌(封顶不受限)。
    """
    state = conflict.state
    if is_resolved(state):
        return state, "已落槌", None

    if not (boss_present and sister_present):
        who = "阿财" if not boss_present else "淑芬"
        return state, f"{who}不在场,账本的事摊不开", None

    # 拖延封顶:不无限僵持。张力极高→崩裂;否则被阿财糊弄过去(掩盖)。
    if conflict.age_in_state >= conflict.max_stall_days:
        if tension >= 70:
            forced, reason = ConflictState.RESOLVED_BREAKDOWN, "拖延封顶且家里气氛紧绷,终于摊牌崩裂"
        else:
            forced, reason = ConflictState.RESOLVED_COVERUP, "拖延封顶,阿财又把账糊弄了过去"
        c, _summary = _ledger_resolution_consequence(forced)
        return forced, reason, c

    if has_progress_signal:
        if state == ConflictState.DEMAND_TRUTH:
            if days_since_created < min_days:
                return state, "话到嘴边,兄妹谁也没先开口,再僵一僵", None
            # 临门一脚的结局由家里张力裁定:高→崩裂,中→重建信任,低→被糊弄掩盖。
            if tension >= 70:
                resolved, reason = ConflictState.RESOLVED_BREAKDOWN, "逼到摊牌,话太重,家裂了"
            elif tension >= 40:
                resolved, reason = ConflictState.RESOLVED_TRUST, "终于把话说开,兄妹决定一起扛"
            else:
                resolved, reason = ConflictState.RESOLVED_COVERUP, "阿财服软又遮掩,这事暂被压下"
            c, _summary = _ledger_resolution_consequence(resolved)
            return resolved, reason, c
        return next_progress_state(state, BOSS_SISTER_LEDGER), "账本的事又往前戳破了一层", None

    return state, "今日相安无事,账本的事没挑明", None


def ensure_boss_sister_ledger_conflict(repo: Repository, game_id: str, day: int) -> Optional[Conflict]:
    """家里气氛/阿财压力到阈值时,惰性创建账本摊牌冲突(SUSPICION)。一桩只发生一次。"""
    existing = repo.get_conflict(game_id, BOSS_SISTER_LEDGER)
    if existing is not None:
        return existing
    world = repo.get_world_state(game_id)
    boss = repo.get_npc(game_id, _BOSS)
    triggered = (
        world.global_tension >= _LEDGER_TRIGGER_TENSION
        or (boss is not None and boss.stress >= _LEDGER_TRIGGER_BOSS_STRESS)
    )
    if not triggered:
        return None
    conflict = Conflict(
        id=BOSS_SISTER_LEDGER, kind=BOSS_SISTER_LEDGER,
        participants=[_BOSS, _SISTER], state=ConflictState.SUSPICION,
        age_in_state=0, max_stall_days=CONFLICT_MAX_DAYS,
        created_day=day,
    )
    repo.upsert_conflict(game_id, conflict)
    repo.add_conflict_log(
        game_id, BOSS_SISTER_LEDGER, day, "(none)", ConflictState.SUSPICION.value,
        trigger_event="家务浮现", reason="家里气氛紧绷/阿财压力藏不住,账本对不上的事开始发酵",
    )
    return conflict


def _detect_ledger_progress_signal(repo: Repository, game_id: str, day: int) -> bool:
    """从阿财/淑芬各自当天记忆里探测账本线是否被往前戳破(严守知识隔离)。"""
    keywords = ("账", "账本", "借据", "欠", "钱庄", "坦白", "摊牌", "瞒", "积蓄", "对不上")
    for npc_id in (_BOSS, _SISTER):
        for mem in repo.get_memories_by_day(game_id, npc_id, day):
            if any(k in mem.content for k in keywords):
                return True
    return False


def describe_conflicts_for_npc(repo: Repository, game_id: str, npc_id: str) -> str:
    """#6:按角色分层地为某 NPC 汇总冲突上下文,用于注入决策。

    - 参与者视角:本人卷入的冲突给出【完整状态】(含已结算),据此说话,别谈作废旧情节。
    - 旁观者视角:本人未参与的冲突【只给可观察到的模糊提示】(谁最近不露面、谁像在等人),
      绝不泄露冲突状态机/交易细节/结算结果——严守知识隔离,只凭"在场/缺席"这类明面现象。

    返回多行文本(无任何相关信息则返回空串)。
    """
    npcs = repo.get_all_npcs(game_id)
    name_of = {n.id: n.name for n in npcs}
    present_of = {n.id: n.is_present() for n in npcs}

    own_lines: List[str] = []
    bystander_lines: List[str] = []
    for conflict in repo.get_all_conflicts(game_id):
        label = "录音交易" if conflict.kind == DEAL_RECORDING else "账本摊牌"
        if npc_id in conflict.participants:
            sentence = state_sentence(conflict.state)
            if not sentence:
                continue
            status = "已了结" if conflict.is_resolved() else "进行中"
            others = "、".join(
                name_of.get(p, p) for p in conflict.participants if p != npc_id
            )
            who = f"(与{others})" if others else ""
            own_lines.append(f"- 【{label}·{status}】{who} {sentence}")
        else:
            hint = _bystander_conflict_hint(conflict, name_of, present_of)
            if hint:
                bystander_lines.append("- " + hint)

    blocks: List[str] = []
    if own_lines:
        blocks.append(
            "你正卷入的事(请据此说话,别谈已经了结/作废的旧情节):\n" + "\n".join(own_lines)
        )
    if bystander_lines:
        blocks.append(
            "你旁观到的零星动静(只是表象,你并不知道内情,别替别人把话说死):\n"
            + "\n".join(bystander_lines)
        )
    return "\n".join(blocks)


def _bystander_conflict_hint(conflict, name_of: dict, present_of: dict) -> str:
    """旁观者只能看到的模糊提示:依据参与者"在场/缺席"这类明面现象,不含任何冲突内情。

    例:某参与者蛰伏/离场而另一参与者还在场 → "阿龙最近不太露面,小林像是一直在等谁"。
    无可观察到的反差则不提(返回空串),避免凭空生成旁观信息。
    """
    absent = [name_of.get(p, p) for p in conflict.participants if not present_of.get(p, True)]
    present = [name_of.get(p, p) for p in conflict.participants if present_of.get(p, True)]
    if not absent:
        return ""
    who_gone = "、".join(absent)
    if present:
        who_wait = "、".join(present)
        return f"{who_gone}最近不太露面,{who_wait}像是一直在等谁。"
    return f"{who_gone}最近不太露面。"


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


def _decide_for_conflict(repo: Repository, game_id: str, day: int, conflict: Conflict):
    """按 kind 分派到对应纯决策函数,返回(新状态, 原因, 后果)。"""
    world = repo.get_world_state(game_id)
    days_since_created = day - conflict.created_day
    if conflict.kind == BOSS_SISTER_LEDGER:
        boss = repo.get_npc(game_id, _BOSS)
        sister = repo.get_npc(game_id, _SISTER)
        return decide_ledger_transition(
            conflict,
            tension=world.global_tension,
            boss_present=bool(boss and boss.is_present()),
            sister_present=bool(sister and sister.is_present()),
            has_progress_signal=_detect_ledger_progress_signal(repo, game_id, day),
            days_since_created=days_since_created,
            min_days=CONFLICT_MIN_DAYS,
        )
    # 默认:录音交易
    holder = repo.get_npc(game_id, _HOLDER)
    buyer = repo.get_npc(game_id, _BUYER)
    return decide_transition(
        conflict,
        exposure_risk=world.police_exposure_risk,
        exposure_stage=world.exposure_stage,
        holder_present=bool(holder and holder.is_present()),
        buyer_present=bool(buyer and buyer.is_present()),
        has_progress_signal=_detect_progress_signal(repo, game_id, day),
        days_since_created=days_since_created,
        min_days=CONFLICT_MIN_DAYS,
    )


def tick_conflicts(repo: Repository, game_id: str, day: int) -> List[str]:
    """每日推进所有未落槌冲突一格(录音交易 + 账本摊牌)。返回可读摘要行。"""
    lines: List[str] = []
    ensure_deal_recording_conflict(repo, game_id, day)
    ensure_boss_sister_ledger_conflict(repo, game_id, day)

    for conflict in repo.get_active_conflicts(game_id):
        label = "账本摊牌" if conflict.kind == BOSS_SISTER_LEDGER else "录音交易"
        new_state, reason, consequence = _decide_for_conflict(repo, game_id, day, conflict)
        from_state = conflict.state

        consequence_summary = ""
        if consequence is not None:
            applied = apply_consequence(repo, game_id, consequence, day, source=conflict.id)
            consequence_summary = "; ".join(applied)

        if new_state != from_state:
            conflict.state = new_state
            conflict.age_in_state = 0
            repo.upsert_conflict(game_id, conflict)
            # #3:一旦落槌(进入终态),硬改写参与者目标,从源头杜绝结算后目标回流。
            if is_resolved(new_state):
                rewrite_goals_on_resolution(repo, game_id, conflict, new_state)
            repo.add_conflict_log(
                game_id, conflict.id, day, from_state.value, new_state.value,
                trigger_event="每日推进", reason=reason,
                consequence_summary=consequence_summary,
            )
            lines.append(f"[冲突·{label}] {from_state.value} → {new_state.value}({reason})")
        else:
            conflict.age_in_state += 1
            repo.upsert_conflict(game_id, conflict)
            lines.append(f"[冲突·{label}] 维持 {from_state.value}(第{conflict.age_in_state}天:{reason})")

    return lines
