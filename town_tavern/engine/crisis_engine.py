"""危机倒计时引擎(P1)。

当老陈曝光风险绷到 crisis 阶段,局势不再只是"维持高张力等待玩家",而是按
连续危机天数(crisis_days)逐级触发【硬事件】,把世界推向不可逆:

  第1天:威胁证人        —— 老陈敲打可能知情者(淑芬/赌徒),逼其闭嘴
  第2天:搜查扣押        —— 强行搜查,赌徒被迫蛰伏
  第3天:失踪/抢证       —— 关键人离场,证据出岔子(必给二级线索)
  第4天起:强制结算      —— 录音交易被强行落槌(交给冲突引擎裁定结局)

红线:这些硬事件【只】堆 flag / 关系 / 压力 / 离场,绝不写 athou_truth_progress;
凡涉及证据损毁/抢夺,必伴随 secondary_clue_available,杜绝死路。
每级事件靠 flag 保证只触发一次。
"""
from typing import List

from ..config import CRISIS_ESCALATION_INTERVAL
from ..models.consequence import (
    Consequence, MemorySpec, NpcStatusChange, RelationshipChange,
)
from ..storage.repository import Repository
from . import conflict_engine
from .consequence import apply_consequence, get_flag

_WITNESS = "sister"
_HOLDER = "gambler"
_POLICE = "police"

# 每个危机等级:(触发 flag, 后果工厂, 摘要)。flag 用于保证只触发一次。
_CRISIS_FLAG_L1 = "crisis_witness_threatened"
_CRISIS_FLAG_L2 = "crisis_raid"
_CRISIS_FLAG_L3 = "crisis_disappearance"
_CRISIS_FLAG_L4 = "crisis_forced_deal"


def _level1_consequence() -> Consequence:
    return Consequence(
        flags={_CRISIS_FLAG_L1: True},
        world_changes={"truth_pressure": 8, "global_tension": 8},
        relationship_changes=[
            RelationshipChange(from_npc=_WITNESS, to_npc=_POLICE, fear=15),
        ],
        memories=[
            MemorySpec(npc_id=_WITNESS, content="今天被警觉的老陈拐着弯敲打了一番,话里全是'别多嘴'的意思,后背发凉。", importance=78, emotional_tag="恐惧", related_npc=_POLICE),
        ],
    )


def _level2_consequence() -> Consequence:
    return Consequence(
        flags={_CRISIS_FLAG_L2: True},
        npc_status=[NpcStatusChange(npc_id=_HOLDER, status="hiding", duration_days=2)],
        world_changes={"truth_pressure": 6, "global_tension": 10},
        relationship_changes=[
            RelationshipChange(from_npc=_HOLDER, to_npc=_POLICE, fear=18),
        ],
        memories=[
            MemorySpec(npc_id=_HOLDER, content="风声太紧,有人来翻查,只能先躲一阵子,东西也得藏好。", importance=80, emotional_tag="慌乱", related_npc=_POLICE),
        ],
    )


def _level3_consequence() -> Consequence:
    return Consequence(
        # 关键人离场 + 证据出岔子 → 必给二级线索(红线#2)
        flags={_CRISIS_FLAG_L3: True, "secondary_clue_available": True},
        npc_status=[NpcStatusChange(npc_id=_HOLDER, status="away", duration_days=3)],
        world_changes={"truth_pressure": 10, "global_tension": 12},
        memories=[
            MemorySpec(npc_id=_HOLDER, content="不能再待下去了,连夜走人,临走把一条线索托给了能信的人。", importance=82, emotional_tag="决绝", related_npc=None),
        ],
    )


def tick_crisis(repo: Repository, game_id: str, day: int) -> List[str]:
    """每日结算危机倒计时:按等级触发尚未触发过的硬事件。

    crisis_days 的维护已统一上移到 world_engine.update_exposure_stage(#3),
    这里只【读取】派生后的 crisis_days(crisis 阶段才 >0),据此触发逐级硬事件。
    须在 update_exposure_stage 之后调用。返回可读摘要行。
    """
    lines: List[str] = []
    world = repo.get_world_state(game_id)
    # P0:世界已停在爆点等玩家时——冻结危机升级,不再触发新硬事件(保持临界不加码)。
    if world.is_awaiting_player():
        return lines
    # crisis_days 已由 update_exposure_stage 维护:crisis 阶段=连续天数,否则=0。
    crisis_days = world.crisis_days

    if crisis_days <= 0:
        # 离开危机:复位各级触发 flag,以便下次危机能重新逐级升级。
        for fl in (_CRISIS_FLAG_L1, _CRISIS_FLAG_L2, _CRISIS_FLAG_L3, _CRISIS_FLAG_L4):
            if get_flag(repo, game_id, fl):
                repo.set_world_value(game_id, f"flag_{fl}", "0")
        return lines

    # #4:危机已进入【降温/余波】阶段——即便曝光仍/又落在 crisis 区间,也不再强触发
    # 硬事件,给局势喘息(逐级升级只在 active 阶段进行)。降温/退出由 tick_crisis_phase 管。
    if world.crisis_phase in ("cooling", "aftermath"):
        return lines

    # 逐级硬事件(各靠 flag 保证只触发一次)。
    # 升级节奏可配置:第 n 级在 crisis_days 达到 n*间隔 时触发。debug_fast 间隔=1 即
    # 逐天升级(历史行为);demo_normal/slow_burn 把硬事件之间拉开(间隔=2/3天)。
    step = CRISIS_ESCALATION_INTERVAL
    if crisis_days >= 1 * step and not get_flag(repo, game_id, _CRISIS_FLAG_L1):
        applied = apply_consequence(repo, game_id, _level1_consequence(), day, source="crisis")
        lines.append("[危机·第1级] 老陈威胁证人闭嘴。 " + "; ".join(applied))
    if crisis_days >= 2 * step and not get_flag(repo, game_id, _CRISIS_FLAG_L2):
        applied = apply_consequence(repo, game_id, _level2_consequence(), day, source="crisis")
        lines.append("[危机·第2级] 强行搜查,赌徒被迫蛰伏。 " + "; ".join(applied))
    if crisis_days >= 3 * step and not get_flag(repo, game_id, _CRISIS_FLAG_L3):
        applied = apply_consequence(repo, game_id, _level3_consequence(), day, source="crisis")
        lines.append("[危机·第3级] 关键人连夜离场,留下二级线索。 " + "; ".join(applied))
    if crisis_days >= 4 * step and not get_flag(repo, game_id, _CRISIS_FLAG_L4):
        forced = conflict_engine.force_resolve_deal_recording(
            repo, game_id, day, reason="危机升级到顶:局势不容再拖,交易被强行了结"
        )
        repo.set_world_value(game_id, f"flag_{_CRISIS_FLAG_L4}", "1")
        if forced:
            lines.append("[危机·第4级] " + forced)
        else:
            lines.append("[危机·第4级] 强制结算:录音交易已无可结算项。")

    return lines


_AFTERMATH_LEFT_KEY = "crisis_aftermath_days_left"


def tick_crisis_phase(repo: Repository, game_id: str, day: int) -> List[str]:
    """#4:危机生命周期阶段机——让危机会"降温"并留下"余波",不再一直在 crisis 烧。

    转移:none →(进入 crisis)→ active →(烧到顶级 L4)→ cooling →(曝光跌出
    crisis)→ aftermath →(宽限天数耗尽)→ none。

    - active:正常逐级硬事件(由 tick_crisis 负责)。
    - cooling:每天主动压低曝光风险(CRISIS_COOLING_EXPOSURE_DROP),把局势带出 crisis;
      此阶段起 tick_crisis 不再强触发硬事件。
    - aftermath:宽限若干天(CRISIS_AFTERMATH_DAYS),期间即便曝光回弹到 crisis 也不重燃
      硬事件,给世界喘息;并复位各级 flag,使日后新危机能重新逐级升级。

    须在 update_exposure_stage / tick_crisis 之后调用。返回可读摘要行。
    """
    from ..config import CRISIS_AFTERMATH_DAYS, CRISIS_COOLING_EXPOSURE_DROP

    lines: List[str] = []
    world = repo.get_world_state(game_id)
    phase = world.crisis_phase
    stage = world.exposure_stage

    def _set_phase(p: str) -> None:
        repo.set_world_value(game_id, "crisis_phase", p)

    def _reset_crisis_flags() -> None:
        for fl in (_CRISIS_FLAG_L1, _CRISIS_FLAG_L2, _CRISIS_FLAG_L3, _CRISIS_FLAG_L4):
            if get_flag(repo, game_id, fl):
                repo.set_world_value(game_id, f"flag_{fl}", "0")

    # 0) 进入危机:none → active。
    # P0:awaiting_player(已停在爆点等玩家)时【不】重燃危机——这正是无人值守下
    # 曝光被事件反复顶回 crisis、危机 none→active→cooling→aftermath→none 无限循环的根因。
    if phase == "none":
        if stage == "crisis" and not world.is_awaiting_player():
            _set_phase("active")
            lines.append("[危机·阶段] 局势进入危机,老陈开始不择手段。")
        return lines

    # 1) active:烧到顶级(L4 已触发)即转入降温。
    if phase == "active":
        if get_flag(repo, game_id, _CRISIS_FLAG_L4):
            _set_phase("cooling")
            lines.append("[危机·降温] 局势烧到了顶,风头开始收敛,危机进入降温。")
        return lines

    # 2) cooling:每天压低曝光,直到跌出 crisis;一旦退阶即转入余波。
    if phase == "cooling":
        if stage == "crisis":
            new_exposure = repo.add_exposure(game_id, -CRISIS_COOLING_EXPOSURE_DROP)
            lines.append(f"[危机·降温] 风声渐弱,老陈暂收锋芒(曝光降至{new_exposure})。")
        else:
            _set_phase("aftermath")
            repo.set_world_value(game_id, _AFTERMATH_LEFT_KEY, CRISIS_AFTERMATH_DAYS)
            _reset_crisis_flags()
            lines.append("[危机·余波] 这场风波暂告段落,镇上松了口气,但伤痕还在。")
        return lines

    # 3) aftermath:宽限倒计时;期间不重燃硬事件(由 tick_crisis 中的 phase 判定保证)。
    if phase == "aftermath":
        raw = repo.get_world_value(game_id, _AFTERMATH_LEFT_KEY)
        left = int(raw) if raw is not None else 0
        left -= 1
        if left <= 0:
            _set_phase("none")
            repo.set_world_value(game_id, _AFTERMATH_LEFT_KEY, 0)
            lines.append("[危机·余波] 日子渐渐归于寻常,危机的余波散去。")
        else:
            repo.set_world_value(game_id, _AFTERMATH_LEFT_KEY, left)
        return lines

    return lines
