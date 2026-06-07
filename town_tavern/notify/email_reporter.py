"""QQ 邮箱运行汇报。

职责:
1. send_email —— 通过 QQ 邮箱 SMTP(SSL,465)发一封中文邮件(授权码鉴权)。
2. RunReporter —— 在守护进程运行期间累积"谁做了什么/说了什么"的时间线,
   每隔 EMAIL_INTERVAL_HOURS 小时汇总成一封可读邮件发出。

设计:
- 所有敏感配置(发件邮箱/授权码/收件邮箱)只来自 .env,不写进代码。
- 发信失败只记录、绝不让守护进程崩溃(汇报是旁路功能,不能影响世界演化)。
"""
import smtplib
import ssl
import time
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from typing import List, Optional, Tuple

from ..config import (
    EMAIL_AUTH_CODE, EMAIL_ENABLED, EMAIL_INTERVAL_HOURS, EMAIL_SMTP_HOST,
    EMAIL_SMTP_PORT, EMAIL_TO, EMAIL_USER,
)


def email_configured() -> Tuple[bool, str]:
    """检查邮件配置是否齐全。返回 (是否就绪, 缺失说明)。"""
    missing = []
    if not EMAIL_USER:
        missing.append("EMAIL_USER")
    if not EMAIL_AUTH_CODE:
        missing.append("EMAIL_AUTH_CODE")
    if not EMAIL_TO:
        missing.append("EMAIL_TO")
    if missing:
        return False, "缺少配置:" + ", ".join(missing)
    return True, ""


def send_email(subject: str, body: str) -> None:
    """通过 QQ 邮箱 SMTP(SSL)发送一封纯文本中文邮件。

    失败时抛出异常,由调用方决定是否吞掉(守护进程里会吞掉以保运行)。
    """
    ready, why = email_configured()
    if not ready:
        raise RuntimeError(f"邮件未配置完整:{why}")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    raw = msg.as_string()

    # 优先用配置端口的 SSL(QQ 默认 465);失败再回退到 587 STARTTLS。
    # 某些网络/环境下 465 直连 SSL 会被中途断开,587 STARTTLS 往往更稳。
    last_err: Optional[Exception] = None
    ctx = ssl.create_default_context()

    def _via_ssl(port: int) -> None:
        with smtplib.SMTP_SSL(EMAIL_SMTP_HOST, port, timeout=30, context=ctx) as server:
            server.login(EMAIL_USER, EMAIL_AUTH_CODE)
            server.sendmail(EMAIL_USER, [EMAIL_TO], raw)

    def _via_starttls(port: int) -> None:
        with smtplib.SMTP(EMAIL_SMTP_HOST, port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.ehlo()
            server.login(EMAIL_USER, EMAIL_AUTH_CODE)
            server.sendmail(EMAIL_USER, [EMAIL_TO], raw)

    # 依次尝试:配置端口 SSL → 465 SSL → 587 STARTTLS,任一成功即返回。
    # 但若是【认证失败(535/密码错)】则立即失败、不再换端口重试,
    # 以免把 QQ 账号撞进"登录频率限制"。只有连接层错误才继续回退。
    attempts = [
        ("SSL", EMAIL_SMTP_PORT, _via_ssl),
        ("SSL", 465, _via_ssl),
        ("STARTTLS", 587, _via_starttls),
    ]
    tried = set()
    for mode, port, fn in attempts:
        if (mode, port) in tried:
            continue
        tried.add((mode, port))
        try:
            fn(port)
            return
        except smtplib.SMTPAuthenticationError as e:
            raise RuntimeError(
                f"QQ 邮箱拒绝登录(认证失败 {e.smtp_code}):请确认已开启 SMTP 服务、"
                f"且 EMAIL_AUTH_CODE 是有效的【SMTP 授权码】。原始信息:{e.smtp_error!r}"
            ) from e
        except Exception as e:  # 连接层错误:换下一种传输方式
            last_err = e

    raise RuntimeError(f"所有 SMTP 方式均失败,最后错误:{last_err!r}")


class RunReporter:
    """累积运行时间线,按间隔汇总发送邮件。"""

    def __init__(self, interval_hours: Optional[float] = None):
        # 汇报间隔(秒)。默认取 config(默认 1 小时),允许构造时覆盖。
        hours = EMAIL_INTERVAL_HOURS if interval_hours is None else interval_hours
        self.interval_seconds = max(60.0, float(hours) * 3600.0)
        self.enabled = EMAIL_ENABLED
        self._lines: List[Tuple[str, str]] = []   # (HH:MM:SS, 文本)
        self._days_advanced = 0
        self._started_at = datetime.now()
        self._last_sent_at = time.monotonic()

    # ------------------------------------------------------------------
    # 累积
    # ------------------------------------------------------------------
    def record(self, line: str) -> None:
        """记录一条时间线文本(去掉首尾空白,空行忽略)。"""
        text = (line or "").strip()
        if not text:
            return
        self._lines.append((datetime.now().strftime("%H:%M:%S"), text))

    def note_day_done(self, n: int = 1) -> None:
        """累加本汇报周期内推进完成的游戏天数。"""
        self._days_advanced += n

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    def due(self) -> bool:
        """是否到达发送间隔(且开启了邮件、且有内容)。"""
        if not self.enabled:
            return False
        if not self._lines:
            return False
        return (time.monotonic() - self._last_sent_at) >= self.interval_seconds

    def _build_body(self, state_summary: str) -> str:
        """把累积时间线 + 世界快照拼成可读正文。"""
        now = datetime.now()
        header = (
            f"【昨日酒馆 · 运行汇报】\n"
            f"汇报时间:{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"本周期推进:{self._days_advanced} 个游戏日\n"
            f"自上次汇报累计 {len(self._lines)} 条动态\n"
            f"{'=' * 40}\n"
        )
        state = f"\n【当前世界快照】\n{state_summary}\n{'=' * 40}\n" if state_summary else ""
        timeline = "\n【这段时间发生了什么(谁做了什么、说了什么)】\n" + "\n".join(
            f"[{ts}] {text}" for ts, text in self._lines
        )
        return header + state + timeline

    def flush(self, state_summary: str = "", force: bool = False) -> bool:
        """汇总并发送邮件;成功后清空缓冲、重置计时。

        force=True 时无视间隔强制发送(用于退出前/手动触发)。
        返回是否实际发送。任何异常都向上抛由调用方处理。
        """
        if not self.enabled:
            return False
        if not self._lines and not force:
            return False
        days = self._days_advanced
        subject = (
            f"【昨日酒馆】运行汇报 · 推进{days}天 · "
            f"{datetime.now().strftime('%m-%d %H:%M')}"
        )
        body = self._build_body(state_summary)
        send_email(subject, body)
        # 发送成功才清空,避免失败丢内容
        self._lines.clear()
        self._days_advanced = 0
        self._last_sent_at = time.monotonic()
        return True
