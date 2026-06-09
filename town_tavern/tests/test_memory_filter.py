"""#2 空记忆过滤:写入源头拦截 + 历史脏数据清理。"""
from town_tavern.engine import memory_engine
from town_tavern.engine.memory_engine import (
    is_valid_memory_content, normalize_memory_content, purge_invalid_memories,
    write_memory,
)
from town_tavern.models.memory import Memory, MemoryType


# ---- 纯校验函数 ----
def test_invalid_contents_rejected():
    for bad in ["", "   ", None, "[记忆梳理]", "[记忆梳理] ", "[反思]", "None", "null",
                "：", "[记忆梳理]：", "短"]:
        assert is_valid_memory_content(bad) is False, bad


def test_valid_contents_accepted():
    for ok in ["我看见阿龙把纸扔进火盆", "[记忆梳理] 我和小林谈成了一笔交易",
               "老陈今天来得很早,坐在角落"]:
        assert is_valid_memory_content(ok) is True, ok


def test_normalize_strips_whitespace():
    assert normalize_memory_content("  你好  ") == "你好"
    assert normalize_memory_content(None) == ""


# ---- 写入源头拦截 ----
def test_write_memory_skips_empty(game):
    repo, game_id = game
    rid = write_memory(repo, game_id, "boss", 1, "[记忆梳理]")
    assert rid is None
    assert repo.list_all_memories(game_id) == []


def test_write_memory_persists_valid(game):
    repo, game_id = game
    rid = write_memory(repo, game_id, "boss", 1, "老陈今天来得很早,坐在角落")
    assert isinstance(rid, int)
    assert len(repo.list_all_memories(game_id)) == 1


def test_compression_empty_summary_does_not_write_shell(game, monkeypatch):
    """LLM 压缩吐空时,不写入空壳长期记忆,也不删除原始记忆。"""
    repo, game_id = game
    for i in range(40):
        write_memory(repo, game_id, "boss", 1, f"第{i}件小事发生了一些情况")
    before = len(repo.get_shortterm_memories(game_id, "boss"))
    monkeypatch.setattr(memory_engine, "_summarize", lambda mems, llm: "   ")
    did = memory_engine.compress_memories_if_needed(repo, game_id, "boss", 2, llm=object())
    assert did is False
    # 原始短期记忆保留,且没有冒出 "[记忆梳理]" 空壳
    after = repo.get_shortterm_memories(game_id, "boss")
    assert len(after) == before
    assert all("[记忆梳理]" not in m.content or is_valid_memory_content(m.content) for m in after)


# ---- 历史脏数据清理 ----
def test_purge_invalid_memories(game):
    repo, game_id = game
    # 直接绕过 write_memory 注入脏数据(模拟旧版本遗留)
    repo.add_memory(game_id, Memory(npc_id="boss", day=1, type=MemoryType.SYSTEM_EVENT,
                                    content="[记忆梳理]", importance=70))
    repo.add_memory(game_id, Memory(npc_id="boss", day=1, type=MemoryType.SYSTEM_EVENT,
                                    content="", importance=50))
    good = repo.add_memory(game_id, Memory(npc_id="boss", day=1, type=MemoryType.DIALOGUE,
                                           content="老陈今天来得很早,坐在角落", importance=50))
    removed = purge_invalid_memories(repo, game_id)
    assert removed == 2
    remaining = repo.list_all_memories(game_id)
    assert [m.id for m in remaining] == [good]
