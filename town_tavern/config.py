"""全局配置:API Key、模型名、引擎参数等。

所有敏感信息通过 .env 文件或环境变量注入,绝不硬编码。
运行前请在项目根目录的 .env 中填写 DEEPSEEK_API_KEY 等配置。
"""
import os
from pathlib import Path

try:
    # 用于自动加载 .env 文件;未安装时降级为仅读系统环境变量
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# ---------------------------------------------------------------------------
# 路径配置
# ---------------------------------------------------------------------------
# 项目根目录(town_tavern/)
BASE_DIR = Path(__file__).resolve().parent
# 数据文件目录
DATA_DIR = BASE_DIR / "data"
# 加载 .env(优先项目根,其次包目录),其中存放 API Key / URL 等敏感配置
if load_dotenv is not None:
    load_dotenv(BASE_DIR.parent / ".env")
    load_dotenv(BASE_DIR / ".env")

# SQLite 数据库文件路径(在 .env 加载后计算,以便读到 .env 里的覆盖值)。
# 支持用环境变量 TOWN_TAVERN_DB 覆盖,便于容器里把存档落到可挂载的卷(如 /data)。
DB_PATH = Path(os.environ.get("TOWN_TAVERN_DB", str(BASE_DIR / "town_tavern.db")))

# ---------------------------------------------------------------------------
# DeepSeek / LLM 配置
# ---------------------------------------------------------------------------
# API Key:从环境变量读取,缺失时在 client 初始化阶段报错
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
# DeepSeek API 基础地址(OpenAI 兼容接口)
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# 模型名:按用户要求默认 deepseek-v4-pro。
# 注意:若官方实际模型名不同(如 deepseek-chat),改这一行或设置环境变量即可。
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

# LLM 调用温度:叙事层可稍高,结构化裁决层在调用处单独压低
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.8"))
# 结构化输出解析失败时的重试次数
LLM_JSON_RETRIES = 1
# 请求超时(秒)
LLM_TIMEOUT = 60

# ---------------------------------------------------------------------------
# 活世界 / 自主时钟参数
# ---------------------------------------------------------------------------
# 现实时间到游戏天数的换算:现实多少秒 = 游戏 1 天(挂机长草)。
# 默认 600 秒(10 分钟)推进 1 天;可用环境变量调小以便快速体验。
REAL_SECONDS_PER_DAY = int(os.environ.get("REAL_SECONDS_PER_DAY", "600"))
# 单次进入时按现实时间自动补推的最大天数,避免久未登录一次性炸 API。
MAX_AUTO_ADVANCE_DAYS = int(os.environ.get("MAX_AUTO_ADVANCE_DAYS", "5"))
# 手动"离开/快进"时允许的最大天数。
MAX_MANUAL_ADVANCE_DAYS = int(os.environ.get("MAX_MANUAL_ADVANCE_DAYS", "10"))

# ---------------------------------------------------------------------------
# 引擎参数
# ---------------------------------------------------------------------------
# 构造对话上下文时取最近多少条短期记忆
RECENT_MEMORY_LIMIT = 5
# PR5:recent 记忆里"传闻(RUMOR)"最多保留几条,避免低价值传闻刷屏挤掉自身行动/对话记忆。
RECENT_RUMOR_CAP = int(os.environ.get("RECENT_RUMOR_CAP", "2"))
# 构造对话上下文时取多少条重要长期记忆(兼容旧逻辑/压缩用)
LONGTERM_MEMORY_LIMIT = 3
# 引擎 C(C2)记忆分槽:长期记忆按"槽位"组装,避免反思刷屏挤掉关键事实。
# 关键事实(非反思类长期记忆)槽位数 + 自我反思单独槽位数。
LONGTERM_FACT_SLOT = int(os.environ.get("LONGTERM_FACT_SLOT", "3"))
LONGTERM_REFLECTION_SLOT = int(os.environ.get("LONGTERM_REFLECTION_SLOT", "1"))
# 单个 NPC 短期记忆超过该数量时触发压缩
MEMORY_COMPRESS_THRESHOLD = 12
# 反思机制:每隔多少天,NPC 自动总结处境并更新长期目标
REFLECTION_INTERVAL_DAYS = int(os.environ.get("REFLECTION_INTERVAL_DAYS", "3"))
# 群像行动层:开启后每天为"每个 NPC"各跑一次 LLM,把其意图落成具体行动(含小后果),
# 再综合众人行动合成当日焦点事件。涌现更强、个体更鲜活,但每天 LLM 调用数翻倍。
# 关闭则回退到旧的"单一核心动作"模式(每天仅 1 次事件 LLM 调用)。
PER_NPC_ACTION_LLM = os.environ.get("PER_NPC_ACTION_LLM", "1") not in ("0", "false", "False")

# ---------------------------------------------------------------------------
# 酒馆社交对话模式(Generative Agents 风格):每天 NPC 之间多轮真实对话
# ---------------------------------------------------------------------------
# 开启后,每天用"多轮社交"驱动世界:每轮每个 NPC 自行决定【找人对话】或【独自行动】,
# 对话双方各自为自己说话(不代笔),旁白 AI 只记录"谁找了谁 + 神态"(绝不泄露内容),
# 次日各 NPC 据此(+自己的私密记忆)再决策。优先级高于 PER_NPC_ACTION_LLM。
CONVERSATION_MODE = os.environ.get("CONVERSATION_MODE", "1") not in ("0", "false", "False")
# 每天进行的社交轮数(每轮:各人决策一次 + 被点名者各回一次 + 旁白总结一次)。
CONV_ROUNDS = int(os.environ.get("CONV_ROUNDS", "2"))
# 独自行动(暗中调查/掩盖/回避)的结果是否调用"裁决 LLM"细化(读权威世界状态、受程序 clamp);
# 关闭则只用程序规则裁决数值,行动经过用中性模板描述,零额外调用。
CONV_REFEREE_LLM = os.environ.get("CONV_REFEREE_LLM", "1") not in ("0", "false", "False")
# 社交并发度:同一轮内互不依赖的 LLM 调用(各人决策 / 各条回复+裁决)并行发起的最大线程数。
# 全部 DB 读写仍在主线程串行完成,只把"纯网络调用"放到线程池,显著压低单日耗时。设为 1 即串行。
CONV_CONCURRENCY = int(os.environ.get("CONV_CONCURRENCY", "5"))

# --- 进阶版:多回合对话(高张力对子才你来我往,普通寒暄保持一来一回) ---
# 一次对话里"A 说 + B 回 = 1 个回合"。普通对子(寒暄/低张力)只进行 1 个回合,
# 高张力对子(冲突参与者 / 关系里 怀疑·怨恨·恐惧 任一较高)最多进行 TALK_MAX_TURNS_HIGH 个回合。
# 是否继续追问由 B 回复里的 wants_to_continue + A 追问时的 continue_talking 共同决定(任一收口即止),
# 因此实际回合数 ∈ [1, TALK_MAX_TURNS_HIGH],既解决"太单薄",又不会让所有人都啰嗦、token 飙升。
TALK_MAX_TURNS_BASE = int(os.environ.get("TALK_MAX_TURNS_BASE", "1"))
TALK_MAX_TURNS_HIGH = int(os.environ.get("TALK_MAX_TURNS_HIGH", "3"))
# 关系五维里"对抗维度"(怀疑/怨恨/恐惧)达到该阈值即视为高张力对子,给更长对话预算。
TALK_TENSION_THRESHOLD = int(os.environ.get("TALK_TENSION_THRESHOLD", "40"))
# 关系四维 + 怀疑度的取值区间
RELATION_MIN = 0
RELATION_MAX = 100
# 压力取值区间
STRESS_MIN = 0
STRESS_MAX = 100
# 阿土主线进度区间
ATHOU_PROGRESS_MIN = 0
ATHOU_PROGRESS_MAX = 100

# ---------------------------------------------------------------------------
# 玩家行动力 / 玩家自身状态(让"每次介入都有代价")
# ---------------------------------------------------------------------------
# 玩家每个游戏日的行动点(AP)上限。AP 耗尽需"离开/快进"开启新一天。
PLAYER_DAILY_ENERGY = int(os.environ.get("PLAYER_DAILY_ENERGY", "3"))
# 玩家初始金钱
PLAYER_START_MONEY = int(os.environ.get("PLAYER_START_MONEY", "3000"))
# 玩家状态(声望/嫌疑)区间
PLAYER_STAT_MIN = 0
PLAYER_STAT_MAX = 100

# ---------------------------------------------------------------------------
# 节奏模式(pacing):一套预设统一控制"世界推进多快",玩家 demo 可一键切换。
# 用环境变量 GAME_PACE 选择;任一单项仍可被它自己的专用环境变量覆盖(显式优先)。
#   debug_fast  —— 快速爆发,方便测试(= 现有默认行为,保持向后兼容)
#   demo_normal —— 10~15 天出大事件,适合玩家试玩
#   slow_burn   —— 30 天以上慢热
# 默认仍为 debug_fast(与历史行为一致);玩家 demo 建议设 GAME_PACE=demo_normal。
# ---------------------------------------------------------------------------
GAME_PACE = os.environ.get("GAME_PACE", "debug_fast").strip().lower()

_PACE_PRESETS = {
    # conflict_min_days:冲突自创建起至少拖几天才允许"落槌"(防止世界爆太快;危机截断可越过它)。
    # conflict_max_days:单状态最多拖几天,超过强制落槌(北极星上限)。
    # crisis_escalation_interval:危机硬事件逐级升级的间隔天数。
    # crisis_aftermath_days:危机烧到顶并降温后,进入"余波期"的宽限天数,期间不再强触发硬事件。
    "debug_fast":  {"conflict_min_days": 0, "conflict_max_days": 5, "crisis_escalation_interval": 1, "debt_interest_per_day": 5000, "exposure_decay_per_day": 3, "crisis_aftermath_days": 2},
    "demo_normal": {"conflict_min_days": 3, "conflict_max_days": 5, "crisis_escalation_interval": 2, "debt_interest_per_day": 8000, "exposure_decay_per_day": 3, "crisis_aftermath_days": 3},
    "slow_burn":   {"conflict_min_days": 5, "conflict_max_days": 8, "crisis_escalation_interval": 3, "debt_interest_per_day": 3000, "exposure_decay_per_day": 5, "crisis_aftermath_days": 4},
}
_PACE = _PACE_PRESETS.get(GAME_PACE, _PACE_PRESETS["debug_fast"])


def _paced_int(env_key: str, pace_key: str) -> int:
    """取节奏参数:显式环境变量优先,否则用当前节奏预设值。"""
    v = os.environ.get(env_key)
    return int(v) if v is not None else int(_PACE[pace_key])


# 冲突"最早可落槌"地板与"最多拖延"上限(供冲突状态机调速)。
CONFLICT_MIN_DAYS = max(0, _paced_int("CONFLICT_MIN_DAYS", "conflict_min_days"))
CONFLICT_MAX_DAYS = max(1, _paced_int("CONFLICT_MAX_DAYS", "conflict_max_days"))
# 危机逐级硬事件的升级间隔(天)。debug_fast=1 即逐天升级(历史行为)。
CRISIS_ESCALATION_INTERVAL = max(1, _paced_int("CRISIS_ESCALATION_INTERVAL", "crisis_escalation_interval"))
# #4:危机烧到顶后进入"降温→余波"的宽限天数;余波期内不再强触发硬事件,让局势喘口气。
CRISIS_AFTERMATH_DAYS = max(1, _paced_int("CRISIS_AFTERMATH_DAYS", "crisis_aftermath_days"))
# #4:降温阶段每天主动压低的曝光风险点数(足以把曝光带出 crisis 区间,触发退阶)。
CRISIS_COOLING_EXPOSURE_DROP = int(os.environ.get("CRISIS_COOLING_EXPOSURE_DROP", "30"))

# ---------------------------------------------------------------------------
# 活变量:债务 / 曝光 / 紧张度(每天自动演化,并被行为联动)
# ---------------------------------------------------------------------------
# 阿财债务每日基础利息(随节奏模式变化;DEBT_INTEREST_PER_DAY 为同义别名)
DEBT_DAILY_INTEREST = _paced_int("DEBT_DAILY_INTEREST", "debt_interest_per_day")
DEBT_INTEREST_PER_DAY = DEBT_DAILY_INTEREST
# 阿财压力高于该值时,利滚利更狠(乘数)
DEBT_STRESS_THRESHOLD = 70
DEBT_HIGH_INTEREST_MULT = 1.6
# 债务危险阈值(用于叙事升级与事件偏置)
DEBT_WARN = 350000
DEBT_DANGER = 500000
DEBT_CRITICAL = 700000
# 曝光风险危险阈值
EXPOSURE_WATCH = 50      # 老陈开始留意/监视
EXPOSURE_DANGER = 70     # 老陈主动设法掩盖
EXPOSURE_CRITICAL = 90   # 老陈可能栽赃/摊牌
# 全局紧张度每日自然衰减(避免单调累积至饱和)
TENSION_DAILY_DECAY = 2
# PR6:global_tension 改为按态势分段计算后,每日朝目标值平滑的最大步长(防跳变)。
TENSION_SMOOTH_STEP = int(os.environ.get("TENSION_SMOOTH_STEP", "15"))

# ---------------------------------------------------------------------------
# PR2:NPC 运行期状态(active/hiding/away)默认持续天数
# ---------------------------------------------------------------------------
# away(跑路/离场)默认持续天数;hiding(蛰伏/躲藏)默认持续天数。
# 期间该 NPC 退出社交决策池,到期自动回 active。
NPC_AWAY_DEFAULT_DAYS = int(os.environ.get("NPC_AWAY_DEFAULT_DAYS", "3"))
NPC_HIDING_DEFAULT_DAYS = int(os.environ.get("NPC_HIDING_DEFAULT_DAYS", "2"))

# ---------------------------------------------------------------------------
# PR3:冲突状态机(P0,当前只用于录音交易 deal_recording)
# ---------------------------------------------------------------------------
# 录音交易在某一状态最多拖延几天:超过则强制落槌(北极星:最多 5 天必产生不可逆结果)。
# 默认跟随节奏模式的 CONFLICT_MAX_DAYS;仍可用专用环境变量单独覆盖。
DEAL_RECORDING_MAX_STALL_DAYS = int(
    os.environ.get("DEAL_RECORDING_MAX_STALL_DAYS", str(CONFLICT_MAX_DAYS))
)

# ---------------------------------------------------------------------------
# 引擎 A:阈值状态机 + 衰减 + 平台(让自运行变量"会喘气、绷到高张力平台即止")
# ---------------------------------------------------------------------------
# 曝光风险每日自然衰减:无新线索时缓慢回落,避免单调贴顶("系统说摊牌、世界无反应")。
# 随节奏模式变化(EXPOSURE_DECAY_PER_DAY 为同义别名)。
EXPOSURE_DAILY_DECAY = _paced_int("EXPOSURE_DAILY_DECAY", "exposure_decay_per_day")
EXPOSURE_DECAY_PER_DAY = EXPOSURE_DAILY_DECAY
# 曝光"高张力平台":超过此值后,自运行不再无限累加,而是被额外回拉到平台附近维持紧张,
# 而非永远钉死在 100。玩家行动仍可把它顶得更高。
EXPOSURE_PLATFORM = int(os.environ.get("EXPOSURE_PLATFORM", "90"))
# 债务"平台":到达濒临卖店(DEBT_CRITICAL)后,自运行利息停止累加,转为维持高压等待玩家,
# 避免债务数字无意义地指数爆炸。
DEBT_PLATFORM = DEBT_CRITICAL
# 阶段判定迟滞:升入高阶后,需回落超过该幅度才退阶,避免临界点反复抖动(振荡)。
STAGE_HYSTERESIS = int(os.environ.get("STAGE_HYSTERESIS", "8"))

# ---------------------------------------------------------------------------
# 引擎 C(C1):真相压力(truth_pressure)
# ---------------------------------------------------------------------------
# NPC 自运行【只能】累积 truth_pressure(局势压力),用于驱动危机阶段;
# 它【不等于】玩家揭开真相(后者仍记在 athou_truth_progress,只因玩家行动增加)。
# truth_pressure 设软上限平台:绷到平台即维持高张力等待玩家,而非无限爆炸。
TRUTH_PRESSURE_MIN = 0
TRUTH_PRESSURE_MAX = 100
TRUTH_PRESSURE_PLATFORM = int(os.environ.get("TRUTH_PRESSURE_PLATFORM", "80"))

# ---------------------------------------------------------------------------
# 后台自动演化守护进程(daemon)参数
# ---------------------------------------------------------------------------
# 守护进程轮询节拍(秒):每隔多久检查一次各存档是否该按现实时间补推世界。
# 应明显小于「MAX_AUTO_ADVANCE_DAYS × REAL_SECONDS_PER_DAY」,以免长间隔被封顶截断丢天。
# 节拍内若未满 1 个游戏日则零成本空转(不调用 LLM)。
DAEMON_TICK_SECONDS = int(os.environ.get("DAEMON_TICK_SECONDS", "60"))
# 守护进程启动时若库中没有任何存档,是否自动新建一局世界。
DAEMON_AUTO_CREATE = os.environ.get("DAEMON_AUTO_CREATE", "1") not in ("0", "false", "False")

# ---------------------------------------------------------------------------
# 运行汇报邮件(QQ 邮箱 SMTP):守护进程每隔一段时间把"谁做了什么/说了什么"发到邮箱
# ---------------------------------------------------------------------------
# 总开关。授权码等敏感信息只放 .env,绝不写进代码仓库。
EMAIL_ENABLED = os.environ.get("EMAIL_ENABLED", "0") not in ("0", "false", "False")
# QQ 邮箱 SMTP(SSL)固定参数
EMAIL_SMTP_HOST = os.environ.get("EMAIL_SMTP_HOST", "smtp.qq.com")
EMAIL_SMTP_PORT = int(os.environ.get("EMAIL_SMTP_PORT", "465"))
# 发件邮箱(QQ 账号)与其 SMTP 授权码(非登录密码),均从 .env 注入
EMAIL_USER = os.environ.get("EMAIL_USER", "")
EMAIL_AUTH_CODE = os.environ.get("EMAIL_AUTH_CODE", "")
# 收件邮箱(可与发件相同)
EMAIL_TO = os.environ.get("EMAIL_TO", "")
# 汇报间隔(小时):每隔多久发送一封运行汇报。默认 1 小时,可在 .env 自行调整。
EMAIL_INTERVAL_HOURS = float(os.environ.get("EMAIL_INTERVAL_HOURS", "1"))
