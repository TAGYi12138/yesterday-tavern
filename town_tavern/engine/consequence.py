"""统一后果应用器(FOUNDATION)。

所有"会改变世界状态"的事件都把副作用打包成一份 `Consequence` 契约,交由
`apply_consequence` 落库。这是整个引擎里唯一被允许集中改世界状态的入口,因此
也是集中拦截红线的地方:

  ★ 红线:`athou_truth_progress`(玩家真相进度)【绝不】由后果写入。
    任何 world_changes 里出现该键都会被丢弃并记一条 blocked 说明——真相只能由
    玩家亲自行动揭开,自运行后果只能堆 flag / 关系 / 压力 / 离场。

返回一个可读的 `applied` 摘要(list[str]),便于事件叙事与测试断言。
"""
from typing import List

from ..config import (
    NPC_AWAY_DEFAULT_DAYS,
    NPC_HIDING_DEFAULT_DAYS,
    PLAYER_STAT_MAX,
    PLAYER_STAT_MIN,
)
from ..models.consequence import FORBIDDEN_WORLD_KEYS, Consequence
from ..models.memory import MemoryType
from ..storage.repository import Repository, _clamp
from .memory_engine import write_memory

# 默认离场天数(按状态查表;契约里给了 duration_days 则优先)
_STATUS_DEFAULT_DAYS = {
    "away": NPC_AWAY_DEFAULT_DAYS,
    "hiding": NPC_HIDING_DEFAULT_DAYS,
}


def _flag_key(name: str) -> str:
    """剧情 flag 在 world_state 里的存储键(统一加前缀,避免与数值键串台)。"""
    return name if name.startswith("flag_") else f"flag_{name}"


def get_flag(repo: Repository, game_id: str, name: str) -> bool:
    v = repo.get_world_value(game_id, _flag_key(name))
    return v == "1"


def _apply_world_change(repo: Repository, game_id: str, key: str, delta: int) -> str:
    """把单个受控世界数值增量落库(各自带 clamp/平台规则)。返回可读摘要。"""
    if key == "boss_debt":
        new = repo.add_boss_debt(game_id, delta)
        return f"阿财欠债{delta:+d}→{new}"
    if key == "police_exposure_risk":
        new = repo.add_exposure(game_id, delta)
        return f"老陈曝光{delta:+d}→{new}"
    if key == "truth_pressure":
        new = repo.add_truth_pressure(game_id, delta)
        return f"真相压力{delta:+d}→{new}"
    if key == "global_tension":
        cur = repo.get_world_state(game_id).global_tension
        new = _clamp(cur + delta, PLAYER_STAT_MIN, PLAYER_STAT_MAX)
        repo.set_world_value(game_id, "global_tension", new)
        return f"全局紧张度{delta:+d}→{new}"
    if key == "crisis_days":
        cur = repo.get_world_state(game_id).crisis_days
        new = max(0, cur + delta)
        repo.set_world_value(game_id, "crisis_days", new)
        return f"危机天数{delta:+d}→{new}"
    # 未知数值键:不臆测语义,安全跳过(记一条说明,避免静默吞掉)。
    return f"[忽略未知世界键 {key}{delta:+d}]"


def apply_consequence(
    repo: Repository,
    game_id: str,
    consequence: Consequence,
    day: int,
    source: str = "",
) -> List[str]:
    """把一份后果契约统一落库,返回已应用副作用的可读摘要列表。

    顺序:flags → npc_status → relationship_changes → world_changes → memories。
    `day` 为当前游戏天,用于换算 NPC 离场到期天与记忆归属天。
    """
    applied: List[str] = []

    # 1) flags(剧情 flag,幂等置位)
    for name, value in consequence.flags.items():
        repo.set_world_value(game_id, _flag_key(name), "1" if value else "0")
        applied.append(f"flag:{name}={'1' if value else '0'}")

    # 2) NPC 出场状态(离场/蛰伏/复位)
    for ch in consequence.npc_status:
        if ch.status == "active":
            repo.set_npc_status(game_id, ch.npc_id, "active", 0)
            applied.append(f"{ch.npc_id} 回到在场")
            continue
        dur = ch.duration_days or _STATUS_DEFAULT_DAYS.get(ch.status, 0)
        until = day + dur if dur > 0 else 0
        repo.set_npc_status(game_id, ch.npc_id, ch.status, until)
        tail = f"至第{until}天" if until else "(无限期)"
        applied.append(f"{ch.npc_id}→{ch.status}{tail}")

    # 3) 关系增量
    for rc in consequence.relationship_changes:
        repo.apply_relationship_delta(
            game_id, rc.from_npc, rc.to_npc,
            trust=rc.trust, fear=rc.fear, resentment=rc.resentment,
            affection=rc.affection, suspicion=rc.suspicion,
        )
        applied.append(f"关系 {rc.from_npc}->{rc.to_npc} 已调整")

    # 4) 世界数值增量(★ 红线:丢弃 athou_truth_progress)
    for key, delta in consequence.world_changes.items():
        if key in FORBIDDEN_WORLD_KEYS:
            applied.append(f"[拦截] 拒写真相进度 {key}(真相归玩家)")
            continue
        applied.append(_apply_world_change(repo, game_id, key, int(delta)))

    # 5) 记忆(严守知识隔离:只写给指定 NPC 自己)
    for mem in consequence.memories:
        write_memory(
            repo, game_id, mem.npc_id, day, mem.content,
            mtype=MemoryType.SYSTEM_EVENT, importance=mem.importance,
            emotional_tag=mem.emotional_tag or None, related_npc=mem.related_npc,
        )
        applied.append(f"记忆→{mem.npc_id}")

    return applied
