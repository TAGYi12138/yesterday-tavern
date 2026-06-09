"""玩家已知线索引擎(#3)。

唯一的写入口径,集中守住一条规则:

    系统 flag 变化 ≠ 玩家知道
    只有玩家【观察 / 询问 / 偷听 / 交易 / 被告知】才写 player_known_clues

世界自运行(冲突状态机、危机倒计时)会改很多真实 flag,但【绝不】调用这里——
那些只是"客观发生了什么",不是"玩家知道了什么"。玩家系统接入后,玩家的每一次
主动行动才通过本模块落一条线索,从而守住"真相归玩家"。
"""
from typing import Optional

from ..models.clue import PlayerClue
from ..storage.repository import Repository

# 合法的线索来源(玩家主动行为)。非这些来源一律拒写,防止系统 flag 偷偷"教会"玩家。
PLAYER_CLUE_SOURCES = {"观察", "询问", "偷听", "交易", "被告知"}


def record_player_clue(
    repo: Repository,
    game_id: str,
    *,
    clue_id: str,
    title: str,
    source: str,
    day: int,
    certainty: int = 50,
) -> Optional[PlayerClue]:
    """玩家通过某个行动获得一条线索时调用。source 必须是 PLAYER_CLUE_SOURCES 之一。

    返回写入后的线索;source 非法则【不写】并返回 None(从源头杜绝系统 flag 直灌)。
    """
    if source not in PLAYER_CLUE_SOURCES:
        return None
    clue = PlayerClue(
        id=clue_id, title=title, source=source,
        certainty=max(0, min(100, certainty)), day_found=day,
    )
    repo.add_player_clue(game_id, clue)
    return repo.get_player_clue(game_id, clue_id)
