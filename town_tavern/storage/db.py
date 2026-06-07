"""SQLite 连接管理与建表。

所有运行期状态(npc 状态、关系、记忆、事件、世界状态)都按 game_id 隔离,
以支持多局存档。
"""
import sqlite3
from pathlib import Path

from ..config import DB_PATH


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """获取一个带 Row 工厂的 SQLite 连接。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row  # 让查询结果支持按列名访问
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL + 忙等超时:允许守护进程与 CLI 同时读写同一库,显著降低 "database is locked"。
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """创建所有核心表(若不存在)。"""
    cur = conn.cursor()

    # 游戏存档维度
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS games (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
        """
    )

    # NPC 运行期状态(固定设定也冗余存一份,便于单库自洽)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS npcs (
            game_id TEXT NOT NULL,
            id TEXT NOT NULL,
            name TEXT NOT NULL,
            age INTEGER,
            job TEXT,
            personality TEXT,
            desire TEXT,
            fear TEXT,
            regret TEXT,
            secret TEXT,
            first_impression TEXT DEFAULT '',
            speech_style TEXT DEFAULT '',
            current_goal TEXT,
            stress INTEGER DEFAULT 50,
            money INTEGER DEFAULT 0,
            PRIMARY KEY (game_id, id)
        )
        """
    )

    # 关系(单向,五维)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS relationships (
            game_id TEXT NOT NULL,
            from_npc TEXT NOT NULL,
            to_npc TEXT NOT NULL,
            trust INTEGER DEFAULT 0,
            fear INTEGER DEFAULT 0,
            resentment INTEGER DEFAULT 0,
            affection INTEGER DEFAULT 0,
            suspicion INTEGER DEFAULT 0,
            PRIMARY KEY (game_id, from_npc, to_npc)
        )
        """
    )

    # 记忆(按 npc 隔离)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT NOT NULL,
            npc_id TEXT NOT NULL,
            day INTEGER NOT NULL,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            importance INTEGER DEFAULT 50,
            emotional_tag TEXT,
            related_npc TEXT,
            is_long_term INTEGER DEFAULT 0
        )
        """
    )

    # 事件
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT NOT NULL,
            day INTEGER NOT NULL,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            actors TEXT,
            consequences TEXT,
            visibility TEXT DEFAULT 'public'
        )
        """
    )

    # 全局世界状态(key-value)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS world_state (
            game_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (game_id, key)
        )
        """
    )

    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """对老存档做向后兼容的列迁移(新增列时不破坏已有数据库)。"""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(npcs)")}
    for col in ("first_impression", "speech_style"):
        if col not in existing:
            conn.execute(f"ALTER TABLE npcs ADD COLUMN {col} TEXT DEFAULT ''")
