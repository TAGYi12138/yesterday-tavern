"""记忆数据模型。

记忆按 NPC 单独存储,构造 prompt 时只读取该 NPC 自己的记忆,
确保"每个 NPC 只能记住自己知道的事"。

#5(渐进版):一条记忆可拆成「观察事实 / 个人推测 / 可信度」三层,先以 JSON 字符串
存进既有 content 字段(不改表)。纯字符串记忆视为"只有观察、无推测",完全向后兼容。
"""
import json
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class MemoryType(str, Enum):
    """记忆类型枚举。"""

    PLAYER_ACTION = "player_action"   # 玩家行为
    SECRET = "secret"                 # 秘密
    CONFLICT = "conflict"             # 冲突
    FAVOR = "favor"                   # 恩惠
    BETRAYAL = "betrayal"             # 背叛
    RUMOR = "rumor"                   # 传闻
    SYSTEM_EVENT = "system_event"     # 系统事件
    DIALOGUE = "dialogue"             # 对话记忆
    REFLECTION = "reflection"         # 自我反思(对处境的内心总结)


class Memory(BaseModel):
    """单条记忆。"""

    id: Optional[int] = Field(default=None, description="数据库自增主键")
    npc_id: str = Field(..., description="记忆归属的 NPC id")
    day: int = Field(..., description="发生在第几天")
    type: MemoryType = Field(default=MemoryType.SYSTEM_EVENT, description="记忆类型")
    content: str = Field(..., description="记忆内容")
    importance: int = Field(default=50, description="重要程度 0-100")
    emotional_tag: Optional[str] = Field(default=None, description="情绪标签,如紧张/愤怒")
    related_npc: Optional[str] = Field(default=None, description="关联的 NPC id 或 player")
    is_long_term: bool = Field(default=False, description="是否为长期记忆")


# ---------------------------------------------------------------------------
# #5 渐进版:观察事实 / 个人推测 / 可信度 —— 以 JSON 字符串塞进 content,不改表。
# ---------------------------------------------------------------------------
def make_memory_content(
    observed: str,
    interpretation: Optional[str] = None,
    confidence: Optional[int] = None,
) -> str:
    """把三层记忆编码成 JSON 字符串(存入 content)。

    仅给出 observed 时,会退化为【纯字符串】(保持库内可读、与旧数据一致);
    一旦带上 interpretation/confidence,才编码为 JSON 结构。
    """
    observed = (observed or "").strip()
    interp = (interpretation or "").strip()
    if not interp and confidence is None:
        return observed
    payload: Dict[str, Any] = {"observed": observed}
    if interp:
        payload["interpretation"] = interp
    if confidence is not None:
        payload["confidence"] = int(confidence)
    return json.dumps(payload, ensure_ascii=False)


def parse_memory_content(content: Optional[str]) -> Dict[str, Any]:
    """把 content 解析成 {observed, interpretation, confidence}。

    纯字符串记忆 → observed=原文, interpretation=None, confidence=None(向后兼容)。
    只有"是 dict 且含 observed 键"的 JSON 才当作结构化记忆,避免误判普通中文句子。
    """
    text = (content or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            obj = None
        if isinstance(obj, dict) and "observed" in obj:
            conf = obj.get("confidence")
            return {
                "observed": str(obj.get("observed", "")).strip(),
                "interpretation": (
                    str(obj["interpretation"]).strip()
                    if obj.get("interpretation")
                    else None
                ),
                "confidence": (int(conf) if conf is not None else None),
            }
    return {"observed": text, "interpretation": None, "confidence": None}


def render_memory_for_npc(content: Optional[str]) -> str:
    """把一条记忆渲染成"给该 NPC 看"的文本:事实与推测分开,推测带可信度。

    纯观察 → 直接返回原文;含推测 → "我确定:… / 我推测:…(可信度 N)"。
    """
    p = parse_memory_content(content)
    if not p["interpretation"]:
        return p["observed"]
    conf = f"(可信度 {p['confidence']})" if p["confidence"] is not None else ""
    return f"我确定:{p['observed']} / 我推测:{p['interpretation']}{conf}"
