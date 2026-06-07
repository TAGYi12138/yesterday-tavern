"""仓储层:封装所有表的读写,并做取值范围裁剪(clamp)。

引擎层只与 Repository 打交道,不直接写 SQL。
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from ..config import (
    DATA_DIR,
    LONGTERM_FACT_SLOT,
    LONGTERM_REFLECTION_SLOT,
    PLAYER_DAILY_ENERGY,
    PLAYER_STAT_MAX,
    PLAYER_STAT_MIN,
    PLAYER_START_MONEY,
    RECENT_MEMORY_LIMIT,
    RELATION_MAX,
    RELATION_MIN,
    STRESS_MAX,
    STRESS_MIN,
    TRUTH_PRESSURE_MAX,
    TRUTH_PRESSURE_MIN,
)
from ..models.event import Event, EventConsequences, EventType
from ..models.memory import Memory, MemoryType
from ..models.npc import NPC
from ..models.player import PlayerState
from ..models.relationship import Relationship
from ..models.world import WorldState
from .db import get_connection, init_db


def _clamp(value: int, lo: int, hi: int) -> int:
    """把数值裁剪到 [lo, hi] 区间。"""
    return max(lo, min(hi, value))


class Repository:
    """统一数据访问入口。"""

    def __init__(self, conn: Optional[sqlite3.Connection] = None):
        # 复用传入连接,或新建一个
        self.conn = conn or get_connection()
        init_db(self.conn)

    # ------------------------------------------------------------------
    # 游戏存档
    # ------------------------------------------------------------------
    def create_game(self) -> str:
        """新建一局游戏,载入 NPC 与世界种子,返回 game_id。"""
        game_id = uuid.uuid4().hex[:8]
        self.conn.execute(
            "INSERT INTO games (id, created_at) VALUES (?, ?)",
            (game_id, datetime.now(timezone.utc).isoformat()),
        )

        # 载入 NPC 初始设定
        npcs_raw = json.loads((DATA_DIR / "npcs.json").read_text(encoding="utf-8"))
        for n in npcs_raw:
            npc = NPC(**n)
            self.conn.execute(
                """
                INSERT INTO npcs (game_id, id, name, age, job, personality, desire,
                                  fear, regret, secret, first_impression, speech_style,
                                  current_goal, stress, money)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    game_id, npc.id, npc.name, npc.age, npc.job, npc.personality,
                    npc.desire, npc.fear, npc.regret, npc.secret,
                    npc.first_impression, npc.speech_style, npc.current_goal,
                    npc.stress, npc.money,
                ),
            )

        # 载入世界种子(状态 + 关系)
        seed = json.loads((DATA_DIR / "world_seed.json").read_text(encoding="utf-8"))
        for key, value in seed["world_state"].items():
            self.conn.execute(
                "INSERT INTO world_state (game_id, key, value) VALUES (?, ?, ?)",
                (game_id, key, str(value)),
            )

        # 活世界:记录现实时间锚点与玩家已读到的天数
        now_iso = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO world_state (game_id, key, value) VALUES (?, ?, ?)",
            (game_id, "last_advanced_at", now_iso),
        )
        # 玩家创建时就在第 1 天"在场",已读到第 1 天
        self.conn.execute(
            "INSERT INTO world_state (game_id, key, value) VALUES (?, ?, ?)",
            (game_id, "last_seen_day", "1"),
        )

        # 玩家自身状态初始化(金钱/声望/嫌疑/当日行动点)
        for key, value in (
            ("player_money", PLAYER_START_MONEY),
            ("player_reputation", 0),
            ("player_suspicion", 0),
            ("player_energy", PLAYER_DAILY_ENERGY),
            ("player_free_talked", ""),  # 当日已免费聊过的 NPC id(逗号分隔)
        ):
            self.conn.execute(
                "INSERT INTO world_state (game_id, key, value) VALUES (?, ?, ?)",
                (game_id, key, str(value)),
            )

        # NPC 之间关系 + NPC 对玩家关系
        all_rels = seed["initial_relationships"] + seed["initial_player_relationships"]
        for r in all_rels:
            self.conn.execute(
                """
                INSERT INTO relationships (game_id, from_npc, to_npc, trust, fear,
                                           resentment, affection, suspicion)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    game_id, r["from"], r["to"], r.get("trust", 0), r.get("fear", 0),
                    r.get("resentment", 0), r.get("affection", 0), r.get("suspicion", 0),
                ),
            )

        self.conn.commit()
        return game_id

    def game_exists(self, game_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM games WHERE id = ?", (game_id,)
        ).fetchone()
        return row is not None

    def list_games(self) -> List[str]:
        rows = self.conn.execute(
            "SELECT id FROM games ORDER BY created_at DESC"
        ).fetchall()
        return [r["id"] for r in rows]

    # ------------------------------------------------------------------
    # NPC
    # ------------------------------------------------------------------
    def get_npc(self, game_id: str, npc_id: str) -> Optional[NPC]:
        row = self.conn.execute(
            "SELECT * FROM npcs WHERE game_id = ? AND id = ?", (game_id, npc_id)
        ).fetchone()
        return self._row_to_npc(row) if row else None

    def get_all_npcs(self, game_id: str) -> List[NPC]:
        rows = self.conn.execute(
            "SELECT * FROM npcs WHERE game_id = ?", (game_id,)
        ).fetchall()
        return [self._row_to_npc(r) for r in rows]

    @staticmethod
    def _row_to_npc(row: sqlite3.Row) -> NPC:
        keys = row.keys()
        return NPC(
            id=row["id"], name=row["name"], age=row["age"], job=row["job"],
            personality=row["personality"], desire=row["desire"], fear=row["fear"],
            regret=row["regret"], secret=row["secret"],
            first_impression=row["first_impression"] if "first_impression" in keys else "",
            speech_style=row["speech_style"] if "speech_style" in keys else "",
            current_goal=row["current_goal"],
            stress=row["stress"], money=row["money"],
        )

    def update_npc_stress(self, game_id: str, npc_id: str, delta: int) -> None:
        npc = self.get_npc(game_id, npc_id)
        if not npc:
            return
        new_stress = _clamp(npc.stress + delta, STRESS_MIN, STRESS_MAX)
        self.conn.execute(
            "UPDATE npcs SET stress = ? WHERE game_id = ? AND id = ?",
            (new_stress, game_id, npc_id),
        )
        self.conn.commit()

    def update_npc_money(self, game_id: str, npc_id: str, delta: int) -> None:
        self.conn.execute(
            "UPDATE npcs SET money = money + ? WHERE game_id = ? AND id = ?",
            (delta, game_id, npc_id),
        )
        self.conn.commit()

    def update_npc_goal(self, game_id: str, npc_id: str, goal: str) -> None:
        self.conn.execute(
            "UPDATE npcs SET current_goal = ? WHERE game_id = ? AND id = ?",
            (goal, game_id, npc_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # 关系
    # ------------------------------------------------------------------
    def get_relationship(
        self, game_id: str, from_npc: str, to_npc: str
    ) -> Relationship:
        row = self.conn.execute(
            "SELECT * FROM relationships WHERE game_id = ? AND from_npc = ? AND to_npc = ?",
            (game_id, from_npc, to_npc),
        ).fetchone()
        if row:
            return Relationship(
                from_npc=row["from_npc"], to_npc=row["to_npc"], trust=row["trust"],
                fear=row["fear"], resentment=row["resentment"],
                affection=row["affection"], suspicion=row["suspicion"],
            )
        # 不存在则返回全零关系
        return Relationship(from_npc=from_npc, to_npc=to_npc)

    def apply_relationship_delta(
        self, game_id: str, from_npc: str, to_npc: str,
        trust: int = 0, fear: int = 0, resentment: int = 0,
        affection: int = 0, suspicion: int = 0,
    ) -> None:
        """对一条关系应用增量,并裁剪到合法区间;不存在则先创建。"""
        rel = self.get_relationship(game_id, from_npc, to_npc)
        new = Relationship(
            from_npc=from_npc, to_npc=to_npc,
            trust=_clamp(rel.trust + trust, RELATION_MIN, RELATION_MAX),
            fear=_clamp(rel.fear + fear, RELATION_MIN, RELATION_MAX),
            resentment=_clamp(rel.resentment + resentment, RELATION_MIN, RELATION_MAX),
            affection=_clamp(rel.affection + affection, RELATION_MIN, RELATION_MAX),
            suspicion=_clamp(rel.suspicion + suspicion, RELATION_MIN, RELATION_MAX),
        )
        self.conn.execute(
            """
            INSERT INTO relationships (game_id, from_npc, to_npc, trust, fear,
                                       resentment, affection, suspicion)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id, from_npc, to_npc) DO UPDATE SET
                trust=excluded.trust, fear=excluded.fear,
                resentment=excluded.resentment, affection=excluded.affection,
                suspicion=excluded.suspicion
            """,
            (game_id, from_npc, to_npc, new.trust, new.fear, new.resentment,
             new.affection, new.suspicion),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # 记忆
    # ------------------------------------------------------------------
    def add_memory(self, game_id: str, memory: Memory) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO memories (game_id, npc_id, day, type, content, importance,
                                  emotional_tag, related_npc, is_long_term)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                game_id, memory.npc_id, memory.day, memory.type.value, memory.content,
                memory.importance, memory.emotional_tag, memory.related_npc,
                1 if memory.is_long_term else 0,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_recent_memories(
        self, game_id: str, npc_id: str, limit: int = RECENT_MEMORY_LIMIT
    ) -> List[Memory]:
        rows = self.conn.execute(
            """
            SELECT * FROM memories
            WHERE game_id = ? AND npc_id = ? AND is_long_term = 0
            ORDER BY day DESC, id DESC LIMIT ?
            """,
            (game_id, npc_id, limit),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def get_longterm_memories(
        self,
        game_id: str,
        npc_id: str,
        fact_limit: int = LONGTERM_FACT_SLOT,
        reflection_limit: int = LONGTERM_REFLECTION_SLOT,
    ) -> List[Memory]:
        """按【槽位】组装长期记忆,避免反思刷屏挤掉关键事实(C2)。

        - 关键事实槽:非反思类长期记忆,按重要度取前 fact_limit 条。
        - 自我反思槽:reflection 类长期记忆单独保留 reflection_limit 条(取最新)。
        返回 事实 + 反思 的合并列表(事实在前)。反思因此不再与事实抢同一批 Top-N。
        """
        fact_rows = self.conn.execute(
            """
            SELECT * FROM memories
            WHERE game_id = ? AND npc_id = ? AND is_long_term = 1 AND type != ?
            ORDER BY importance DESC, id DESC LIMIT ?
            """,
            (game_id, npc_id, MemoryType.REFLECTION.value, fact_limit),
        ).fetchall()
        reflection_rows = self.conn.execute(
            """
            SELECT * FROM memories
            WHERE game_id = ? AND npc_id = ? AND is_long_term = 1 AND type = ?
            ORDER BY day DESC, id DESC LIMIT ?
            """,
            (game_id, npc_id, MemoryType.REFLECTION.value, reflection_limit),
        ).fetchall()
        return [self._row_to_memory(r) for r in fact_rows] + [
            self._row_to_memory(r) for r in reflection_rows
        ]

    def get_shortterm_memories(self, game_id: str, npc_id: str) -> List[Memory]:
        """获取该 NPC 全部短期记忆(用于压缩判断)。"""
        rows = self.conn.execute(
            """
            SELECT * FROM memories
            WHERE game_id = ? AND npc_id = ? AND is_long_term = 0
            ORDER BY day ASC, id ASC
            """,
            (game_id, npc_id),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def promote_memory_to_longterm(self, memory_id: int) -> None:
        self.conn.execute(
            "UPDATE memories SET is_long_term = 1 WHERE id = ?", (memory_id,)
        )
        self.conn.commit()

    def delete_memories(self, ids: List[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})", ids
        )
        self.conn.commit()

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> Memory:
        return Memory(
            id=row["id"], npc_id=row["npc_id"], day=row["day"],
            type=MemoryType(row["type"]), content=row["content"],
            importance=row["importance"], emotional_tag=row["emotional_tag"],
            related_npc=row["related_npc"], is_long_term=bool(row["is_long_term"]),
        )

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def add_event(self, game_id: str, event: Event) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO events (game_id, day, type, title, summary, actors,
                                consequences, visibility)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                game_id, event.day, event.type.value, event.title, event.summary,
                json.dumps(event.actors, ensure_ascii=False),
                event.consequences.model_dump_json(),
                event.visibility,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_event_by_day(self, game_id: str, day: int) -> Optional[Event]:
        row = self.conn.execute(
            "SELECT * FROM events WHERE game_id = ? AND day = ? ORDER BY id DESC LIMIT 1",
            (game_id, day),
        ).fetchone()
        return self._row_to_event(row) if row else None

    def get_events_in_range(
        self, game_id: str, from_day: int, to_day: int, only_public: bool = True
    ) -> List[Event]:
        """按天数区间 [from_day, to_day] 取事件,用于回归简报。

        only_public=True 时只返回玩家能知道的公开事件,NPC 的私密谋划被过滤掉,
        留作玩家通过对话去挖掘。
        """
        sql = (
            "SELECT * FROM events WHERE game_id = ? AND day >= ? AND day <= ?"
        )
        params = [game_id, from_day, to_day]
        if only_public:
            sql += " AND visibility = 'public'"
        sql += " ORDER BY day ASC, id ASC"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"], day=row["day"], type=EventType(row["type"]),
            title=row["title"], summary=row["summary"],
            actors=json.loads(row["actors"]) if row["actors"] else [],
            consequences=EventConsequences.model_validate_json(row["consequences"])
            if row["consequences"] else EventConsequences(),
            visibility=row["visibility"],
        )

    # ------------------------------------------------------------------
    # 世界状态
    # ------------------------------------------------------------------
    def get_world_value(self, game_id: str, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM world_state WHERE game_id = ? AND key = ?",
            (game_id, key),
        ).fetchone()
        return row["value"] if row else None

    def set_world_value(self, game_id: str, key: str, value) -> None:
        self.conn.execute(
            """
            INSERT INTO world_state (game_id, key, value) VALUES (?, ?, ?)
            ON CONFLICT(game_id, key) DO UPDATE SET value = excluded.value
            """,
            (game_id, key, str(value)),
        )
        self.conn.commit()

    def get_world_state(self, game_id: str) -> WorldState:
        def _int(key: str, default: int) -> int:
            v = self.get_world_value(game_id, key)
            return int(v) if v is not None else default

        def _str(key: str, default: str) -> str:
            v = self.get_world_value(game_id, key)
            return v if v is not None else default

        return WorldState(
            current_day=_int("current_day", 1),
            global_tension=_int("global_tension", 20),
            athou_truth_progress=_int("athou_truth_progress", 0),
            police_exposure_risk=_int("police_exposure_risk", 30),
            boss_debt=_int("boss_debt", 300000),
            truth_pressure=_int("truth_pressure", 0),
            exposure_stage=_str("exposure_stage", "normal"),
            debt_stage=_str("debt_stage", "stable"),
            truth_stage=_str("truth_stage", "latent"),
        )

    def add_boss_debt(self, game_id: str, delta: int) -> int:
        """调整阿财债务(不低于 0),返回新值。"""
        cur = self.get_world_state(game_id).boss_debt
        new = max(0, cur + delta)
        self.set_world_value(game_id, "boss_debt", new)
        return new

    def add_exposure(self, game_id: str, delta: int) -> int:
        """调整老陈曝光风险(裁剪 0-100),返回新值。"""
        cur = self.get_world_state(game_id).police_exposure_risk
        new = _clamp(cur + delta, 0, 100)
        self.set_world_value(game_id, "police_exposure_risk", new)
        return new

    def add_truth_pressure(self, game_id: str, delta: int) -> int:
        """调整真相压力(裁剪 0-100),返回新值。仅供 NPC 自运行链路累积局势压力。"""
        cur = self.get_world_state(game_id).truth_pressure
        new = _clamp(cur + delta, TRUTH_PRESSURE_MIN, TRUTH_PRESSURE_MAX)
        self.set_world_value(game_id, "truth_pressure", new)
        return new

    def get_current_day(self, game_id: str) -> int:
        return self.get_world_state(game_id).current_day

    def increment_day(self, game_id: str) -> int:
        new_day = self.get_current_day(game_id) + 1
        self.set_world_value(game_id, "current_day", new_day)
        return new_day

    # ------------------------------------------------------------------
    # 活世界:现实时间锚点 / 玩家已读天数
    # ------------------------------------------------------------------
    def get_last_advanced_at(self, game_id: str) -> Optional[datetime]:
        """读取上次推进的现实时间锚点。"""
        v = self.get_world_value(game_id, "last_advanced_at")
        if not v:
            return None
        try:
            dt = datetime.fromisoformat(v)
        except ValueError:
            return None
        # 兼容旧存档:无时区的历史锚点按 UTC 处理,避免与 aware 时间相减报错
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def set_last_advanced_at(self, game_id: str, when: datetime) -> None:
        self.set_world_value(game_id, "last_advanced_at", when.isoformat())

    def get_last_seen_day(self, game_id: str) -> int:
        v = self.get_world_value(game_id, "last_seen_day")
        return int(v) if v is not None else 1

    def set_last_seen_day(self, game_id: str, day: int) -> None:
        self.set_world_value(game_id, "last_seen_day", day)

    # ------------------------------------------------------------------
    # 玩家状态(行动点 / 金钱 / 声望 / 嫌疑)
    # ------------------------------------------------------------------
    def get_player_state(self, game_id: str) -> PlayerState:
        """读取玩家状态;缺失字段(老存档)按默认值兜底。"""
        def _int(key: str, default: int) -> int:
            v = self.get_world_value(game_id, key)
            return int(v) if v is not None else default

        return PlayerState(
            money=_int("player_money", PLAYER_START_MONEY),
            reputation=_int("player_reputation", 0),
            suspicion=_int("player_suspicion", 0),
            energy=_int("player_energy", PLAYER_DAILY_ENERGY),
            max_energy=PLAYER_DAILY_ENERGY,
        )

    def spend_player_energy(self, game_id: str, amount: int) -> bool:
        """尝试消耗 amount 点行动点。不足则返回 False 且不扣减。"""
        state = self.get_player_state(game_id)
        if amount <= 0:
            return True
        if state.energy < amount:
            return False
        self.set_world_value(game_id, "player_energy", state.energy - amount)
        return True

    def add_player_money(self, game_id: str, delta: int) -> None:
        state = self.get_player_state(game_id)
        self.set_world_value(game_id, "player_money", max(0, state.money + delta))

    def add_player_reputation(self, game_id: str, delta: int) -> None:
        state = self.get_player_state(game_id)
        self.set_world_value(
            game_id, "player_reputation",
            _clamp(state.reputation + delta, PLAYER_STAT_MIN, PLAYER_STAT_MAX),
        )

    def add_player_suspicion(self, game_id: str, delta: int) -> None:
        state = self.get_player_state(game_id)
        self.set_world_value(
            game_id, "player_suspicion",
            _clamp(state.suspicion + delta, PLAYER_STAT_MIN, PLAYER_STAT_MAX),
        )

    def reset_player_day(self, game_id: str) -> None:
        """开启新一天:行动点回满,清空当日免费聊天名单。"""
        self.set_world_value(game_id, "player_energy", PLAYER_DAILY_ENERGY)
        self.set_world_value(game_id, "player_free_talked", "")

    def has_free_talk(self, game_id: str, npc_id: str) -> bool:
        """该 NPC 今天是否还有"免费闲聊"额度(每人每天首次免费)。"""
        raw = self.get_world_value(game_id, "player_free_talked") or ""
        talked = {x for x in raw.split(",") if x}
        return npc_id not in talked

    def mark_free_talk(self, game_id: str, npc_id: str) -> None:
        """标记该 NPC 今天的免费闲聊额度已用。"""
        raw = self.get_world_value(game_id, "player_free_talked") or ""
        talked = {x for x in raw.split(",") if x}
        talked.add(npc_id)
        self.set_world_value(game_id, "player_free_talked", ",".join(sorted(talked)))
