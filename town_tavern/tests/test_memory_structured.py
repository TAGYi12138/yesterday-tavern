"""#2 记忆在运行时真正拆 observed/interpretation/confidence(不再只是纯文本)。

压缩(_summarize+_interpret)与反思(reflect)两条写记忆链路,都应产出三层结构;
无 LLM / 不支持 chat_json / 推测为空时,退回纯 observed 文本(与旧版完全一致,零回归)。
"""
from town_tavern.engine import memory_engine, npc_engine
from town_tavern.engine.memory_engine import MemoryInterpretation, write_memory
from town_tavern.models.action import ReflectionResult
from town_tavern.models.memory import parse_memory_content


class _StructuredLLM:
    """压缩用:chat_text 给 observed 概述,chat_json 给推测层。"""

    def chat_text(self, system, user, temperature: float = 0.3) -> str:
        return "这几天我盯着账本、躲着老陈,日子越来越紧"

    def chat_json(self, system, user, schema, temperature: float = 0.3):
        return MemoryInterpretation(interpretation="我怀疑老陈已经在查我", confidence=40)


class _PlainLLM:
    """只会 chat_text(不支持结构化推测)——应退回纯 observed。"""

    def chat_text(self, system, user, temperature: float = 0.3) -> str:
        return "这几天我盯着账本、躲着老陈,日子越来越紧"


def _fill_shortterm(repo, gid, npc="boss", n=40):
    for i in range(n):
        write_memory(repo, gid, npc, 1, f"第{i}件小事发生了一些情况且我记得清楚")


def test_compression_emits_structured_memory(game):
    repo, gid = game
    _fill_shortterm(repo, gid)
    did = memory_engine.compress_memories_if_needed(
        repo, gid, "boss", 2, llm=_StructuredLLM()
    )
    assert did is True
    lt = [m for m in repo.get_longterm_memories(gid, "boss")]
    assert lt, "应写入一条压缩长期记忆"
    parsed = parse_memory_content(lt[0].content)
    # 三层都在:observed(含梳理标记)/ interpretation(推测语气)/ confidence
    assert "[记忆梳理]" in parsed["observed"]
    assert parsed["interpretation"] == "我怀疑老陈已经在查我"
    assert parsed["confidence"] == 40


def test_compression_falls_back_to_plain_without_chat_json(game):
    repo, gid = game
    _fill_shortterm(repo, gid)
    did = memory_engine.compress_memories_if_needed(
        repo, gid, "boss", 2, llm=_PlainLLM()
    )
    assert did is True
    lt = repo.get_longterm_memories(gid, "boss")
    parsed = parse_memory_content(lt[0].content)
    # 无结构化推测能力 → 退回纯 observed,interpretation/confidence 为空(向后兼容)
    assert parsed["interpretation"] is None
    assert parsed["confidence"] is None
    assert "[记忆梳理]" in parsed["observed"]


def test_interpret_returns_empty_without_llm():
    interp, conf = memory_engine._interpret("我确定看到了纸条", [], llm=None)
    assert interp == "" and conf is None


class _ReflectLLM:
    def chat_json(self, system, user, schema, temperature: float = 0.4):
        return ReflectionResult(
            npc_id="boss",
            observed="我这几天一直在翻账本、回避老陈的盘问",
            summary="我觉得这个家快撑不住了",
            confidence=55,
            updated_goal="",
            mood="焦虑",
        )


def test_reflection_writes_structured_memory(game):
    repo, gid = game
    npc_engine.reflect(repo, _ReflectLLM(), gid, "boss", 5)
    lt = repo.get_longterm_memories(gid, "boss")
    assert lt, "反思应写入一条长期记忆"
    parsed = parse_memory_content(lt[0].content)
    assert "[反思]" in parsed["observed"]
    assert parsed["interpretation"] == "我觉得这个家快撑不住了"
    assert parsed["confidence"] == 55


class _ReflectLLMNoObserved:
    def chat_json(self, system, user, schema, temperature: float = 0.4):
        return ReflectionResult(npc_id="boss", summary="我觉得撑不住了", mood="焦虑")


def test_reflection_falls_back_to_plain_without_observed(game):
    repo, gid = game
    npc_engine.reflect(repo, _ReflectLLMNoObserved(), gid, "boss", 5)
    lt = repo.get_longterm_memories(gid, "boss")
    parsed = parse_memory_content(lt[0].content)
    # 未给 observed → 退回旧写法 `[反思] {summary}` 纯文本
    assert parsed["observed"] == "[反思] 我觉得撑不住了"
    assert parsed["interpretation"] is None
