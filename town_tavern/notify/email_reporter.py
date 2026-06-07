"""QQ 邮箱运行汇报。

职责:
1. send_email —— 通过 QQ 邮箱 SMTP(SSL,465)发一封中文邮件(授权码鉴权)。
2. RunReporter —— 在守护进程运行期间累积"谁做了什么/说了什么"的时间线,
   每隔 EMAIL_INTERVAL_HOURS 小时汇总成一封可读邮件发出。

设计:
- 所有敏感配置(发件邮箱/授权码/收件邮箱)只来自 .env,不写进代码。
- 发信失败只记录、绝不让守护进程崩溃(汇报是旁路功能,不能影响世界演化)。
"""
import re
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

# ---------------------------------------------------------------------------
# 剧本化:把守护进程累积的"时间线文本"解析成易读的剧本(谁对谁说了什么 + 旁白)
# ---------------------------------------------------------------------------
# 每条时间线都被守护进程加了 [存档id] 前缀,这里先剥掉。
_GID_RE = re.compile(r"^\[[^\]]+\]\s*")
# 一天开始:`第N天 · 酒馆里的人各自开始今天的活动……`
_DAYSTART_RE = re.compile(r"^第(\d+)天\s*·\s*酒馆里的人")
# 某一轮社交:`第N天 · 第r/R轮社交……(并发度 X)`
_ROUND_RE = re.compile(r"^第(\d+)天\s*·\s*第(\d+)/(\d+)轮社交")
# 对话:`· 对话 甲 ➜ 乙｜甲说:「…」｜乙答:「…」｜神态:…`
_TALK_RE = re.compile(
    r"^·\s*对话\s*(.+?)\s*➜\s*(.+?)｜(?:.+?)说:「(.*?)」｜(?:.+?)答:「(.*?)」｜神态:(.*)$"
)
# 独自行动:`· 甲 独自暗中调查、打探 → 叙述…[ 查到:…]`
_SOLO_RE = re.compile(r"^·\s*(.+?)\s*独自(.+?)\s*→\s*(.+)$")
# 按兵不动:`· 甲 今日按兵不动(…)`
_IDLE_RE = re.compile(r"^·\s*(.+?)\s*今日按兵不动")
# 旁白:`[旁白] …`
_NARRATOR_RE = re.compile(r"^\[旁白\]\s*(.+)$")
# 当日纪事/事件:`→ 当日纪事《标题》`(下一行通常是概述)
_DAYNOTE_RE = re.compile(r"^→\s*当日(?:纪事|事件)《(.+?)》")
# 一天完成:`✓ 第N天完成 → 概述`
_DAYDONE_RE = re.compile(r"^✓\s*第(\d+)天完成\s*→\s*(.+)$")
# 数值后果行(关系/压力/进度等):剧本里不展示,过滤掉
_DATA_PREFIXES = ("关系:", "压力:", "阿土真相进度", "老陈曝光风险", "触发旗标:", "(本次无显著")


def _strip_gid(text: str) -> str:
    return _GID_RE.sub("", (text or "")).strip()


def _is_data_line(text: str) -> bool:
    return text.startswith(_DATA_PREFIXES)


def render_screenplay(lines: List[Tuple[str, str]]) -> str:
    """把 (时间, 文本) 时间线解析成剧本格式。无法解析则作为旁白保留,绝不丢信息。"""
    out: List[str] = []
    i, n = 0, len(lines)
    while i < n:
        text = _strip_gid(lines[i][1])
        i += 1
        if not text:
            continue

        m = _ROUND_RE.match(text)
        if m:
            out.append("")
            out.append(f"—— 第 {m.group(2)} 轮 ——")
            continue

        m = _DAYSTART_RE.match(text)
        if m:
            out.append("")
            out.append("═" * 24)
            out.append(f"　　第 {m.group(1)} 天")
            out.append("═" * 24)
            continue

        m = _TALK_RE.match(text)
        if m:
            asker, target, utter, reply, react = m.groups()
            out.append(f"{asker}（对{target}说）：{utter}")
            react = (react or "").strip()
            prefix = f"（{react}）" if react and react != "神色如常" else ""
            out.append(f"{target}：{prefix}{reply}")
            continue

        m = _SOLO_RE.match(text)
        if m:
            actor, verb, rest = m.groups()
            out.append(f"〔旁白〕{actor}独自{verb}——{rest}")
            continue

        m = _IDLE_RE.match(text)
        if m:
            out.append(f"〔旁白〕{m.group(1)}今日按兵不动。")
            continue

        m = _NARRATOR_RE.match(text)
        if m:
            out.append(f"〔旁白〕{m.group(1)}")
            continue

        m = _DAYDONE_RE.match(text)
        if m:
            out.append("")
            out.append(f"✦ 第 {m.group(1)} 天落幕：{m.group(2)}")
            continue

        m = _DAYNOTE_RE.match(text)
        if m:
            title = m.group(1)
            summary = ""
            if i < n:
                nxt = _strip_gid(lines[i][1])
                if nxt and not _is_data_line(nxt) and not (
                    _ROUND_RE.match(nxt) or _DAYSTART_RE.match(nxt)
                    or _DAYNOTE_RE.match(nxt) or _DAYDONE_RE.match(nxt)
                ):
                    summary = re.sub(r"^经过:", "", nxt)
                    i += 1
            out.append("")
            out.append(f"▌当日纪事《{title}》")
            if summary:
                out.append(f"　{summary}")
            continue

        # 数值后果:剧本里略去
        if _is_data_line(text):
            continue

        # 兜底:未识别的散文,作为旁白保留,避免丢失信息
        out.append(f"〔旁白〕{text}")

    return "\n".join(out).strip()


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
        # 剧本化正文:谁对谁说了什么(甲说→乙答)+ 旁白叙述,便于阅读理解。
        script = render_screenplay(self._lines)
        if script:
            timeline = "\n【酒馆剧本(谁和谁说了什么)】\n" + script
        else:
            # 兜底:解析不出剧本时退回原始流水,绝不丢信息。
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
