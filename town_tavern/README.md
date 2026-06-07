# 昨日酒馆 · 后端 Demo

一个纯命令行的「小镇模拟引擎」:跑通「对话 → 行动 → 推进天数 → NPC 依据记忆与关系演化」的核心闭环。

> 核心理念:**状态机决定世界稳不稳,记忆系统决定 NPC 像不像活人,LLM 只负责表达。**
> 千万不要让 LLM 控制一切 —— **LLM 负责想象,程序负责裁决。**

## 技术栈

- Python 3.10+
- SQLite(标准库,无需额外安装)
- DeepSeek API(通过 OpenAI 兼容 SDK 调用)
- Pydantic v2(结构化校验)

## 目录结构

```text
town_tavern/
├── main.py              # 启动入口
├── config.py            # API Key、模型名、引擎参数
├── cli.py               # 命令行交互
├── models/              # Pydantic 数据模型
├── data/                # NPC 初始设定与世界种子(JSON)
├── engine/              # 核心引擎(world/npc/event/memory/relationship/action)
├── llm/                 # DeepSeek 调用封装 + prompt 模板
└── storage/             # SQLite 连接与仓储
```

## 安装

```bash
pip install -r requirements.txt
```

## 配置

运行前必须设置 DeepSeek API Key(绝不硬编码进代码):

```bash
export DEEPSEEK_API_KEY=你的key
# 可选:覆盖默认模型名 / 地址
export DEEPSEEK_MODEL=deepseek-v4-pro
export DEEPSEEK_BASE_URL=https://api.deepseek.com
```

> ⚠️ 模型名默认 `deepseek-v4-pro`。若 DeepSeek 官方实际模型名不同(如 `deepseek-chat`),
> 用 `DEEPSEEK_MODEL` 环境变量覆盖即可。

## 运行

```bash
# 在 "Yesterday's Tavern" 目录下执行
python -m town_tavern.main
```

菜单:

```text
1. 查看今日状态   —— 当天氛围 + 各 NPC 可见情绪
2. 和 NPC 对话    —— 输入角色 id 与一句话
3. 玩家行动       —— ASK/TELL/HELP/THREATEN/GIVE_MONEY/REPORT
4. 推进一天       —— NPC 生成意图 → 事件生成 → 后果落地 → 记忆更新
0. 退出
```

## 5 个 NPC

| id | 姓名 | 身份 | 核心张力 |
|----|------|------|----------|
| boss | 阿财 | 酒馆老板 | 欠债三十万,弟弟阿土失踪 |
| police | 老陈 | 派出所警员 | 收过黑钱,隐瞒阿土失踪当晚见过他 |
| reporter | 小林 | 小报记者 | 想曝光老陈,手握转账复印件 |
| gambler | 阿龙 | 讨债人 | 手握码头争执录音,两头开价 |
| sister | 淑芬 | 阿财妹妹 | 发现账本不对,怀疑哥哥欠债 |

## 设计要点(防坑)

- **AI 改设定** → 固定设定存 `npcs` 表,每个 prompt 都带 `GUARDRAIL` 护栏。
- **NPC 越界知情** → 记忆按 `npc_id` 隔离,构造 prompt 只读该 NPC 自己的记忆。
- **剧情乱飞** → 事件类型固定 6 种枚举,LLM 只填细节。
- **行动无后果** → `ActionImpact.has_any_consequence()` 强校验,无后果则重判。

## 数据库

SQLite 文件默认生成在 `town_tavern/town_tavern.db`,删除该文件即可重置全部存档。
所有运行期状态按 `game_id` 隔离,支持多局。
