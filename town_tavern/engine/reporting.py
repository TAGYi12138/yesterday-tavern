"""三种日志视角(#1):debug / player / npc_private。

同一天发生的事,要按"谁在看"分成三种报告——接入玩家前先把这条边界打好:

- build_debug_report:开发者视角。系统真实状态全摊开——世界数值、各冲突状态机、
  剧情 flag、当日冲突转移日志。用于调试,【绝不】直接给玩家看。
- build_player_report:玩家视角。只输出"能被观察到的现象":谁在场、谁没来、神态,
  以及当天明面上听说的公开事件。【不含】flag/状态机/英文 id/私密事件。
  因为接入玩家后,玩家看到什么,决定他能做什么。
- build_npc_private_report:某个 NPC 的私人视角。严守知识隔离——只含该 NPC 自己
  当天的记忆,不串别人的心事。

三者都【只读】,不改任何状态。
"""
from typing import List

from ..models.conflict import state_sentence
from ..storage.repository import Repository
from .conversation_engine import _humanize_ids

# 玩家不该看到的开发者术语(出现即说明串进了系统视角,用于自检/单测)。
_DEV_TERMS = (
    "RESOLVED_", "NEGOTIATING", "EXCHANGE_ATTEMPT", "MONEY_SHOWN", "flag_",
    "secondary_clue", "exposure", "truth_pressure", "state",
)


def build_debug_report(repo: Repository, game_id: str, day: int) -> str:
    """开发者视角:系统真实状态全摊开(世界数值 / 冲突状态机 / flag / 当日转移日志)。"""
    world = repo.get_world_state(game_id)
    lines: List[str] = [f"==== Debug 报告 · 第{day}天 ===="]
    lines.append(world.summary_text())
    lines.append(world.debug_state_text())
    lines.append(f"真相压力{world.truth_pressure}/阶段{world.truth_stage} | 危机连续{world.crisis_days}天")

    conflicts = repo.get_all_conflicts(game_id)
    if conflicts:
        lines.append("-- 冲突状态机 --")
        for c in conflicts:
            tag = "已落槌" if c.is_resolved() else f"进行中(本态第{c.age_in_state}天)"
            lines.append(
                f"  [{c.kind}] {c.state.value} · {tag} · 参与:{','.join(c.participants)}"
            )

    flags = {k: v for k, v in repo.get_all_flags(game_id).items() if v}
    if flags:
        lines.append("-- 已置位 flag --")
        lines.append("  " + ", ".join(sorted(flags)))

    todays_logs = []
    for c in conflicts:
        for log in repo.get_conflict_logs(game_id, c.id):
            if log["day"] == day:
                todays_logs.append(f"  [{c.id}] {log['from_state']} → {log['to_state']}({log['reason']})")
    if todays_logs:
        lines.append("-- 当日冲突转移 --")
        lines.extend(todays_logs)

    return "\n".join(lines)


def build_player_report(repo: Repository, game_id: str, day: int) -> str:
    """玩家视角:只显示能被观察到的现象——谁没来、谁在场神态如何、明面听说的事。

    刻意【不含】任何系统术语:冲突状态机、flag、英文 id、私密事件一律不出现。
    """
    npcs = repo.get_all_npcs(game_id)
    name_of = {n.id: n.name for n in npcs}

    lines: List[str] = [f"【第{day}天 · 你在酒馆里看到的】"]

    # 1) 谁没来(hiding/away 视为不在场)——玩家只看到"今天没来",不知道系统状态原因。
    absent = [n.name for n in npcs if not n.is_present()]
    for name in absent:
        lines.append(f"{name}今天没来。")

    # 2) 当天明面上的公开事件(私密事件不剧透),并兜底把英文 id 换成中文名。
    public_events = repo.get_events_in_range(game_id, day, day, only_public=True)
    for ev in public_events:
        lines.append(_humanize_ids(ev.summary, name_of))

    if len(lines) == 1:
        lines.append("酒馆里一切如常,没什么特别的动静。")
    return "\n".join(lines)


def build_npc_private_report(repo: Repository, game_id: str, npc_id: str, day: int) -> str:
    """某 NPC 的私人视角:只含其本人当天的记忆(严守知识隔离),并附其本人参与的冲突状态。

    用于调试/将来给"扮演该 NPC"的视图;绝不串入别人的记忆或别人冲突的细节。
    """
    npc = repo.get_npc(game_id, npc_id)
    name = npc.name if npc else npc_id
    lines: List[str] = [f"【第{day}天 · {name}的视角(仅其本人所知)】"]

    mems = repo.get_memories_by_day(game_id, npc_id, day)
    if mems:
        lines.append("-- 我今天记住的 --")
        for m in mems:
            lines.append(f"  · {m.content}")

    # 只列该 NPC 本人参与的冲突(知识隔离),给一句人话状态。
    own = [c for c in repo.get_all_conflicts(game_id) if npc_id in c.participants]
    if own:
        lines.append("-- 我牵涉的事 --")
        for c in own:
            s = state_sentence(c.state)
            if s:
                lines.append(f"  · {s}")

    if len(lines) == 1:
        lines.append("  (今天没什么记在心上的事。)")
    return "\n".join(lines)
