"""后台自动演化守护进程。

与 CLI 不同:本进程不依赖玩家进入酒馆,而是作为常驻服务按固定节拍
轮询库中所有存档,基于现实流逝时间(world_state.last_advanced_at)
自动补推世界——做到"就算你不开 CLI,小镇也在继续生活"。

实现要点:
- 复用 world_engine.sync_with_real_time,因此演化逻辑与 CLI 的"惰性补推"
  完全一致,只是改为主动、持续地结算,避免两套实现产生分歧。
- 节拍内若不足 1 个游戏日,sync 会直接返回(days<=0),不调用 LLM,
  因此空转零 API 成本。
- 捕获 SIGTERM/SIGINT 优雅退出,适配 `docker stop`。
- 演化进度基于时间戳锚点,进程重启不会丢进度、也不会重复补推。

用法:
    python -m town_tavern.daemon
"""
import signal
import sys
import time
from datetime import datetime, timezone

from .config import (
    DAEMON_AUTO_CREATE, DAEMON_TICK_SECONDS, EMAIL_ENABLED, EMAIL_INTERVAL_HOURS,
    REAL_SECONDS_PER_DAY,
)
from .engine import world_engine
from .engine.reporting import build_player_reports_for_games
from .llm.client import LLMClient, LLMError
from .models.event import Event
from .notify.email_reporter import RunReporter, email_configured
from .storage.repository import Repository

# 收到终止信号后置为 False,让主循环优雅收尾
_running = True


def _handle_signal(signum, _frame) -> None:
    """SIGTERM/SIGINT 处理:标记退出,由主循环自行收尾。"""
    global _running
    _running = False
    _log(f"收到信号 {signum},准备优雅退出……")


def _log(msg: str) -> None:
    """统一带时间戳的日志输出(立即刷新,便于 docker logs 实时查看)。"""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[daemon {ts}Z] {msg}", flush=True)


def _interruptible_sleep(seconds: int) -> None:
    """分段 sleep,使收到退出信号时能尽快响应,而不必等满整个节拍。"""
    for _ in range(max(1, seconds)):
        if not _running:
            return
        time.sleep(1)


def _world_snapshot(repo: Repository) -> str:
    """汇总所有存档的世界 + NPC 当前状态,作为邮件里的"现状快照"。

    #1:在开发者数值快照之上,先附一段【玩家视角】(接 build_player_report 玩家层),
    让汇报邮件也能看到"玩家进游戏会看到的样子",而不再只有 flag/数值的开发者视图。
    """
    lines = []
    player_view = build_player_reports_for_games(repo)
    if player_view and player_view != "(暂无存档)":
        lines.append("【玩家视角 · 你进酒馆会看到的样子】")
        lines.append(player_view)
        lines.append("")
        lines.append("【开发者数值快照(仅供你自查,玩家看不到)】")
    for gid in repo.list_games():
        world = repo.get_world_state(gid)
        lines.append(f"〔存档 {gid}〕{world.summary_text()}")
        for n in repo.get_all_npcs(gid):
            lines.append(f"   · {n.name}({n.job}) 压力{n.stress}/100 目标:{n.current_goal}")
    return "\n".join(lines) if lines else "(暂无存档)"


def _try_send_report(reporter: RunReporter, repo: Repository, force: bool = False) -> None:
    """到点(或强制)时发送一封运行汇报邮件;任何异常只记录,绝不影响演化。"""
    if not (force and reporter.enabled) and not reporter.due():
        return
    try:
        sent = reporter.flush(state_summary=_world_snapshot(repo), force=force)
        if sent:
            _log("已发送一封运行汇报邮件 ✉")
    except Exception as e:  # 邮件是旁路功能,失败不能拖垮守护进程
        _log(f"发送汇报邮件失败(已忽略,下次重试):{e!r}")


def run() -> None:
    """守护进程主入口。"""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    repo = Repository()
    try:
        llm = LLMClient()
    except LLMError as e:
        _log(f"启动失败:{e}")
        sys.exit(1)

    games = repo.list_games()
    if not games and DAEMON_AUTO_CREATE:
        gid = repo.create_game()
        _log(f"库中无存档,已自动创建新世界 game_id={gid}")
        games = [gid]

    # 运行汇报邮件:累积"谁做了什么/说了什么",每隔 EMAIL_INTERVAL_HOURS 小时发一封
    reporter = RunReporter()
    if EMAIL_ENABLED:
        ready, why = email_configured()
        if ready:
            _log(f"邮件汇报已开启:每 {EMAIL_INTERVAL_HOURS} 小时发送一次运行汇报。")
        else:
            _log(f"邮件汇报开关已开,但配置不全({why}),本次将不发送。")
    else:
        _log("邮件汇报未开启(设 EMAIL_ENABLED=1 可开)。")

    _log(
        f"守护启动:节拍 {DAEMON_TICK_SECONDS}s | 现实 {REAL_SECONDS_PER_DAY}s = 游戏 1 天 "
        f"| 当前监管 {len(games)} 个存档"
    )

    while _running:
        try:
            # 每轮重新拉取存档列表,以便运行期间通过 CLI 新建的存档也被纳入演化
            for gid in repo.list_games():
                def _on_progress(stage: str, _gid: str = gid) -> None:
                    # 逐阶段实时日志:让你看到"一天是怎么一步步拼出来的"
                    _log(f"[{_gid}] {stage}")
                    reporter.record(f"[{_gid}] {stage}")

                def _on_step(_i: int, ev: Event, _gid: str = gid) -> None:
                    world = repo.get_world_state(_gid)
                    line = f"[{_gid}] ✓ 第{ev.day}天完成 → {world.summary_text()}"
                    _log(line)
                    reporter.record(line)
                    reporter.note_day_done()

                events = world_engine.sync_with_real_time(
                    repo, llm, gid, on_step=_on_step, on_progress=_on_progress
                )
                if events:
                    world = repo.get_world_state(gid)
                    _log(f"[{gid}] 本轮补推 {len(events)} 天 → {world.summary_text()}")
        except Exception as e:  # 单轮异常不应让守护进程崩溃
            _log(f"演化出错(已忽略,下轮重试):{e!r}")

        # 到点则发送一封运行汇报邮件
        _try_send_report(reporter, repo)

        _interruptible_sleep(DAEMON_TICK_SECONDS)

    # 退出前若本周期还有未发送的内容,补发最后一封,免得丢失尾段进展
    _try_send_report(reporter, repo, force=True)
    _log("已优雅退出。")


if __name__ == "__main__":
    run()
