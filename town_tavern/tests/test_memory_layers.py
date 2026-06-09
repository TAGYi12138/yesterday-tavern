"""#5 渐进版:记忆拆 观察事实 / 个人推测 / 可信度(JSON-in-content,不改表)。"""
from town_tavern.engine.memory_engine import (
    is_valid_memory_content, write_memory,
)
from town_tavern.llm.prompts import _memories_text
from town_tavern.models.memory import (
    Memory, MemoryType, make_memory_content, parse_memory_content,
    render_memory_for_npc,
)


def test_plain_content_parses_as_observed_only():
    p = parse_memory_content("我看见阿龙把纸扔进火盆")
    assert p["observed"] == "我看见阿龙把纸扔进火盆"
    assert p["interpretation"] is None
    assert p["confidence"] is None


def test_make_observed_only_stays_plain_string():
    # 只有 observed 时退化成纯字符串,保持库内可读、与旧数据一致
    assert make_memory_content("我今天在酒馆待了一整天") == "我今天在酒馆待了一整天"


def test_make_and_parse_structured_roundtrip():
    c = make_memory_content("阿龙把一叠纸扔进火盆", "他在销毁码头那晚的东西", 45)
    p = parse_memory_content(c)
    assert p["observed"] == "阿龙把一叠纸扔进火盆"
    assert p["interpretation"] == "他在销毁码头那晚的东西"
    assert p["confidence"] == 45


def test_render_separates_fact_from_guess():
    c = make_memory_content("阿龙今天没来,小林一直在等", "他们之间可能出了事", 40)
    text = render_memory_for_npc(c)
    assert "我确定:阿龙今天没来,小林一直在等" in text
    assert "我推测:他们之间可能出了事" in text
    assert "可信度 40" in text


def test_render_plain_returns_raw():
    assert render_memory_for_npc("小林点了头") == "小林点了头"


def test_chinese_sentence_not_misparsed_as_json():
    # 普通中文句子(非 {..} 包裹)绝不被当成结构化记忆
    p = parse_memory_content("他说:好的,成交")
    assert p["observed"] == "他说:好的,成交"
    assert p["interpretation"] is None


def test_validity_uses_observed_length():
    # observed 过短(去模板后)→ 无效;observed 足够 → 有效
    assert not is_valid_memory_content(make_memory_content("短", "一些推测", 30))
    assert is_valid_memory_content(make_memory_content("我看见他递了纸条", "可能是交易", 30))


def test_write_memory_persists_structured(game):
    repo, gid = game
    mid = write_memory(
        repo, gid, "reporter", 2, "阿龙临时失约,之后没再出现",
        mtype=MemoryType.REFLECTION, importance=60,
        interpretation="可能是老陈打断了交易,也可能阿龙跑路", confidence=60,
    )
    assert mid is not None
    mems = repo.get_memories_by_day(gid, "reporter", 2)
    assert mems
    p = parse_memory_content(mems[0].content)
    assert p["observed"] == "阿龙临时失约,之后没再出现"
    assert p["interpretation"].startswith("可能是老陈")
    assert p["confidence"] == 60


def test_memories_text_groups_facts_and_guesses():
    recent = [
        Memory(npc_id="x", day=3, type=MemoryType.REFLECTION,
               content=make_memory_content("阿龙和小林在角落压低声音交谈", "他们可能谈成了交易", 35)),
        Memory(npc_id="x", day=3, type=MemoryType.DIALOGUE, content="老陈今天来得很早"),
    ]
    text = _memories_text(recent, [])
    assert "【我确定知道的事】" in text
    assert "阿龙和小林在角落压低声音交谈" in text
    assert "老陈今天来得很早" in text          # 纯记忆进"确定知道的事"
    assert "【我的推测(可能有误,别当成事实)】" in text
    assert "他们可能谈成了交易" in text
    assert "可信度 35" in text
