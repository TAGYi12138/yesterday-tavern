"""命令行交互入口。

活世界:进入酒馆时按现实流逝时间自动补推世界并展示"回归简报";
也提供"离开酒馆/快进 N 天"手动入口。玩家的对话与行动会影响后续自动演化。
这是后端 Demo 的"壳",真正的逻辑都在 engine 层。
"""
from datetime import datetime, timezone
from typing import Optional

from .config import MAX_MANUAL_ADVANCE_DAYS, REAL_SECONDS_PER_DAY
from .engine import action_engine, npc_engine, world_engine
from .engine.action_engine import AP_COST, ActionCostError
from .llm.client import LLMClient, LLMError
from .models.action import ActionType, PlayerAction
from .models.event import Event
from .storage.repository import Repository

# 行动类型菜单
ACTION_LABELS = {
    "1": (ActionType.ASK, "询问"),
    "2": (ActionType.TELL, "告诉某人一条消息"),
    "3": (ActionType.HELP, "帮助某人"),
    "4": (ActionType.THREATEN, "威胁某人"),
    "5": (ActionType.GIVE_MONEY, "给钱"),
    "6": (ActionType.REPORT, "举报"),
}


def _print_npcs(repo: Repository, game_id: str) -> None:
    """打印所有 NPC 及其可见情绪(由压力粗略映射)。"""
    print("\n--- 在场角色 ---")
    for npc in repo.get_all_npcs(game_id):
        mood = _mood_from_stress(npc.stress)
        print(f"  [{npc.id}] {npc.name}({npc.job}) 神情:{mood}")


def _mood_from_stress(stress: int) -> str:
    """把压力值粗略映射为可见情绪。"""
    if stress >= 80:
        return "濒临崩溃"
    if stress >= 65:
        return "明显紧张"
    if stress >= 45:
        return "心事重重"
    return "看似平静"


def _print_player(repo: Repository, game_id: str) -> None:
    """打印玩家自身状态(行动点/金钱/声望/嫌疑)。"""
    player = repo.get_player_state(game_id)
    print(f"\n--- 你的状态 ---\n  {player.summary_text()}")


def _show_state(repo: Repository, llm: LLMClient, game_id: str) -> None:
    """查看今日状态。"""
    world = repo.get_world_state(game_id)
    print("\n========== 今日酒馆 ==========")
    print(world.summary_text())
    print(world.debug_state_text())
    summary = world_engine.build_today_summary(repo, llm, game_id)
    print(f"\n氛围:{summary}")
    _print_player(repo, game_id)
    _print_npcs(repo, game_id)


def _do_talk(repo: Repository, llm: LLMClient, game_id: str) -> None:
    """与 NPC 对话。"""
    _print_npcs(repo, game_id)
    npc_id = input("和谁对话?输入角色 id:").strip()
    if not repo.get_npc(game_id, npc_id):
        print("没有这个角色。")
        return
    message = input("你说:").strip()
    if not message:
        return
    try:
        result = npc_engine.talk_to_npc(repo, llm, game_id, npc_id, message)
    except ActionCostError as e:
        print(f"[精力不足] {e}")
        return
    except (LLMError, ValueError) as e:
        print(f"[对话失败] {e}")
        return
    print(f"\n{result.visible_reaction}")
    print(f"「{result.reply}」")
    _print_player(repo, game_id)


def _do_action(repo: Repository, llm: LLMClient, game_id: str) -> None:
    """执行玩家行动。"""
    player = repo.get_player_state(game_id)
    print(f"\n--- 选择行动(当前行动点 {player.energy}/{player.max_energy}) ---")
    for key, (atype, label) in ACTION_LABELS.items():
        print(f"  {key}. {label}(消耗 {AP_COST.get(atype, 1)} 行动点)")
    choice = input("行动编号:").strip()
    if choice not in ACTION_LABELS:
        print("无效选择。")
        return
    action_type, _ = ACTION_LABELS[choice]

    _print_npcs(repo, game_id)
    target = input("目标角色 id:").strip()
    if not repo.get_npc(game_id, target):
        print("没有这个角色。")
        return

    content = input("行动内容/要传达或询问的信息:").strip()
    amount = 0
    if action_type == ActionType.GIVE_MONEY:
        try:
            amount = int(input("给多少钱:").strip() or "0")
        except ValueError:
            amount = 0

    action = PlayerAction(type=action_type, target=target, content=content, amount=amount)
    try:
        impact, effects = action_engine.handle_player_action(repo, llm, game_id, action)
    except ActionCostError as e:
        print(f"[无法行动] {e}")
        return
    except (LLMError, ValueError) as e:
        print(f"[行动失败] {e}")
        return

    print(f"\n结果:{impact.narration or '(无旁白)'}")
    if effects:
        print("影响:")
        for e in effects:
            print(f"  - {e}")
    _print_player(repo, game_id)


def _step_progress(day_index: int, event: Event) -> None:
    """advance_world 的每日进度回调:实时打印这一天发生了什么。"""
    print(f"  · 第{event.day}天 [{event.title}]")


def _do_skip(repo: Repository, llm: LLMClient, game_id: str) -> None:
    """离开酒馆 / 快进若干天:世界自行连续演化,回来读简报。"""
    raw = input(f"离开酒馆几天?(1-{MAX_MANUAL_ADVANCE_DAYS}):").strip()
    try:
        days = int(raw)
    except ValueError:
        print("请输入数字。")
        return
    if days < 1:
        print("至少 1 天。")
        return
    days = min(days, MAX_MANUAL_ADVANCE_DAYS)

    print(f"\n你离开了酒馆。世界在你不在时继续运转({days} 天)……")
    try:
        world_engine.advance_world(repo, llm, game_id, days, on_step=_step_progress)
    except (LLMError, ValueError) as e:
        print(f"[推进失败] {e}")
        return
    # 刷新现实时间锚点,避免回来后又被现实时间重复补推
    repo.set_last_advanced_at(game_id, datetime.now(timezone.utc))

    print("\n你回到了酒馆。")
    briefing = world_engine.build_return_briefing(repo, game_id)
    if briefing:
        print("\n" + briefing)


def _enter_tavern(repo: Repository, llm: LLMClient, game_id: str) -> None:
    """进入酒馆:按现实流逝时间自动补推世界,并展示回归简报。"""
    print("\n（正在结算你离开期间世界的变化……）")
    try:
        events = world_engine.sync_with_real_time(
            repo, game_id=game_id, llm=llm, on_step=_step_progress
        )
    except (LLMError, ValueError) as e:
        print(f"[结算失败] {e}")
        events = []
    briefing = world_engine.build_return_briefing(repo, game_id)
    if briefing:
        print("\n" + briefing)
    elif not events:
        print("距离上次没过多久,酒馆还是老样子。")

    # 进门时,情绪最强烈的 NPC 可能主动开口搭话
    try:
        initiated = npc_engine.npc_initiate_talk(repo, llm, game_id)
    except (LLMError, ValueError):
        initiated = None
    if initiated:
        npc, result = initiated
        print(f"\n你刚进门,{npc.name}就朝你走来。")
        if result.visible_reaction:
            print(f"({result.visible_reaction})")
        print(f"{npc.name}:「{result.reply}」")

    _print_player(repo, game_id)


def _select_or_create_game(repo: Repository) -> Optional[str]:
    """启动时选择已有存档或新建。"""
    games = repo.list_games()
    if games:
        print("已有存档:", ", ".join(games))
        ans = input("输入存档 id 继续,或回车新建:").strip()
        if ans and repo.game_exists(ans):
            return ans
    game_id = repo.create_game()
    print(f"已创建新游戏,game_id = {game_id},当前第 1 天。")
    return game_id


def run() -> None:
    """CLI 主循环。"""
    print("=" * 40)
    print("  昨日酒馆 · 后端 Demo (CLI)")
    print("=" * 40)

    repo = Repository()
    try:
        llm = LLMClient()
    except LLMError as e:
        print(f"\n[启动失败] {e}")
        return

    print(f"（活世界:现实约每 {REAL_SECONDS_PER_DAY} 秒,酒馆里就过去一天）")

    game_id = _select_or_create_game(repo)
    if not game_id:
        return

    # 进入酒馆:先结算离开期间世界的自动演化,再展示回归简报
    _enter_tavern(repo, llm, game_id)

    menu = (
        "\n请选择操作:\n"
        "  1. 查看今日状态\n"
        "  2. 和 NPC 对话\n"
        "  3. 玩家行动(影响故事走向)\n"
        "  4. 离开酒馆 / 快进若干天\n"
        "  0. 退出\n"
        "> "
    )
    while True:
        choice = input(menu).strip()
        if choice == "1":
            _show_state(repo, llm, game_id)
        elif choice == "2":
            _do_talk(repo, llm, game_id)
        elif choice == "3":
            _do_action(repo, llm, game_id)
        elif choice == "4":
            _do_skip(repo, llm, game_id)
        elif choice == "0":
            print("（你离开了。世界仍会继续运转,下次回来再看变化。）")
            print("再见。")
            break
        else:
            print("无效选择。")
