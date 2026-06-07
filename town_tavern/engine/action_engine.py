"""玩家行动引擎。

处理 6 种行动(ASK/TELL/HELP/THREATEN/GIVE_MONEY/REPORT):
校验行动点/金钱成本 → LLM 评估结构化影响 → 校验"至少一个后果" →
应用后果(关系/压力/记忆/目标/旗标/主线)→ 结算玩家代价(AP/金钱/声望/嫌疑)。

设计要点:玩家"每次介入都有代价"。行动点(AP)每天有限,给钱要花自己的钱,
举报会抬高老陈曝光风险与自身嫌疑——杜绝"无成本操纵世界"。
"""
from typing import List, Tuple

from ..config import ATHOU_PROGRESS_MAX, ATHOU_PROGRESS_MIN
from ..llm import prompts
from ..llm.client import LLMClient, LLMError
from ..models.action import ActionImpact, ActionType, PlayerAction
from ..models.memory import MemoryType
from ..storage.repository import Repository
from . import memory_engine, relationship_engine

# 每种行动消耗的行动点(AP)。普通对话不在此处(见 npc_engine 的免费额度)。
AP_COST = {
    ActionType.ASK: 1,        # 深入询问
    ActionType.TELL: 1,       # 传播消息
    ActionType.HELP: 2,       # 帮助
    ActionType.THREATEN: 2,   # 威胁
    ActionType.GIVE_MONEY: 1, # 给钱(另有金钱成本)
    ActionType.REPORT: 2,     # 举报
}


class ActionCostError(Exception):
    """行动点不足 / 金钱不足等"代价无法支付"的错误。"""


def handle_player_action(
    repo: Repository, llm: LLMClient, game_id: str, action: PlayerAction
) -> Tuple[ActionImpact, List[str]]:
    """处理一次玩家行动,返回 (影响, 人类可读的效果说明列表)。

    先校验玩家是否付得起代价(AP/金钱),再调用 LLM,避免浪费 API。
    """
    target = repo.get_npc(game_id, action.target)
    if target is None:
        raise ValueError(f"行动目标 NPC 不存在: {action.target}")

    # 代价预检(在调用 LLM 之前):行动点 + 金钱
    cost = AP_COST.get(action.type, 1)
    player = repo.get_player_state(game_id)
    if player.energy < cost:
        raise ActionCostError(
            f"行动点不足:该行动需要 {cost} 点,你今天只剩 {player.energy} 点。"
            "(可『离开酒馆 / 快进』开启新的一天)"
        )
    if action.type == ActionType.GIVE_MONEY and action.amount > player.money:
        raise ActionCostError(
            f"钱不够:你想给 {action.amount} 元,但身上只有 {player.money} 元。"
        )

    world = repo.get_world_state(game_id)
    rel = repo.get_relationship(game_id, action.target, "player")

    system, user = prompts.build_action_impact_prompt(
        actor_desc="玩家",
        target_npc=target,
        rel_target_to_player=rel,
        world=world,
        action_type=action.type.value,
        content=action.content,
        amount=action.amount,
    )
    impact = llm.chat_json(system, user, ActionImpact)

    # 铁律:行动必须至少产生一个后果
    if not impact.has_any_consequence():
        raise LLMError("行动未产生任何后果,违反设计铁律(已要求 LLM 重判)。")

    effects = _apply_impact(repo, game_id, action, impact, world.current_day)

    # 结算玩家代价:扣行动点(此处必然成功,前面已校验)
    repo.spend_player_energy(game_id, cost)
    effects.extend(_settle_player_cost(repo, game_id, action))
    return impact, effects


def _settle_player_cost(
    repo: Repository, game_id: str, action: PlayerAction
) -> List[str]:
    """结算行动对玩家自身状态(金钱/声望/嫌疑)及联动世界变量的影响。"""
    effects: List[str] = []

    if action.type == ActionType.GIVE_MONEY and action.amount:
        # 给钱:扣玩家的钱;若给的是阿财,直接帮他抵债
        repo.add_player_money(game_id, -action.amount)
        if action.target == "boss":
            new_debt = repo.add_boss_debt(game_id, -action.amount)
            effects.append(f"阿财的债务减少到 {new_debt}")
        repo.add_player_reputation(game_id, 2)

    elif action.type == ActionType.REPORT:
        # 举报:抬高老陈曝光风险,也让自己更受警惕
        new_exp = repo.add_exposure(game_id, 20)
        repo.add_player_suspicion(game_id, 15)
        effects.append(f"老陈的曝光风险升到 {new_exp}")
        effects.append("你的嫌疑上升了")

    elif action.type == ActionType.THREATEN:
        repo.add_player_suspicion(game_id, 8)
        repo.add_player_reputation(game_id, -5)

    elif action.type == ActionType.HELP:
        repo.add_player_reputation(game_id, 5)

    return effects


def _apply_impact(
    repo: Repository, game_id: str, action: PlayerAction,
    impact: ActionImpact, day: int,
) -> List[str]:
    """把结构化影响落地,并收集中文效果说明。"""
    effects: List[str] = []

    # 1. 关系变化
    effects.extend(
        relationship_engine.apply_relationship_deltas(
            repo, game_id, impact.relationship_changes
        )
    )

    # 2. 压力变化
    for s in impact.stress_changes:
        repo.update_npc_stress(game_id, s.npc, s.delta)
        if s.delta:
            effects.append(f"{s.npc} 压力{'上升' if s.delta > 0 else '下降'}{abs(s.delta)}")

    # 3. GIVE_MONEY 行动:直接结算金钱
    if action.type == ActionType.GIVE_MONEY and action.amount:
        repo.update_npc_money(game_id, action.target, action.amount)
        effects.append(f"给了 {action.target} {action.amount} 元")

    # 4. 记忆写入
    for mw in impact.memory_writes:
        memory_engine.write_memory(
            repo, game_id, mw.npc_id, day, content=mw.content,
            mtype=MemoryType.PLAYER_ACTION, importance=mw.importance,
            emotional_tag=mw.emotional_tag, related_npc="player",
        )

    # 5. 目标变化
    for npc_id, goal in impact.goal_changes.items():
        if goal:
            repo.update_npc_goal(game_id, npc_id, goal)
            effects.append(f"{npc_id} 的目标发生了变化")

    # 6. 旗标
    for flag, value in impact.flags.items():
        repo.set_world_value(game_id, f"flag_{flag}", "1" if value else "0")
        effects.append(f"世界旗标 {flag} = {value}")

    # 7. 阿土主线进度
    if impact.athou_progress_delta:
        _advance_athou(repo, game_id, impact.athou_progress_delta, effects)

    return effects


def _advance_athou(
    repo: Repository, game_id: str, delta: int, effects: List[str]
) -> None:
    """推进阿土主线进度,裁剪到合法区间。"""
    cur = repo.get_world_state(game_id).athou_truth_progress
    new = max(ATHOU_PROGRESS_MIN, min(ATHOU_PROGRESS_MAX, cur + delta))
    if new != cur:
        repo.set_world_value(game_id, "athou_truth_progress", new)
        effects.append(f"阿土真相进度 {cur} → {new}")
