"""多人讨论 PR-A:timeline_messages 的 group_id/participants 列 + 老库迁移 + 读写。"""
import sqlite3

from town_tavern.storage.db import get_connection, init_db
from town_tavern.storage.repository import Repository


def _cols(conn: sqlite3.Connection, table: str) -> set:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_migrate_old_timeline_db_adds_group_columns(tmp_path):
    """模拟"没有 group_id/participants 列"的老库:迁移后补列且不丢老数据。"""
    db_file = tmp_path / "old.db"
    raw = sqlite3.connect(str(db_file))
    raw.row_factory = sqlite3.Row
    # 旧版建表:故意不含 group_id/participants。
    raw.execute(
        """
        CREATE TABLE timeline_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT NOT NULL,
            day INTEGER NOT NULL,
            tick INTEGER DEFAULT 0,
            type TEXT NOT NULL,
            speaker_id TEXT,
            speaker_name TEXT,
            target_id TEXT,
            target_name TEXT,
            text TEXT NOT NULL,
            visibility TEXT DEFAULT 'public',
            debug_payload TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    raw.execute(
        "INSERT INTO timeline_messages (game_id, day, type, text) VALUES (?,?,?,?)",
        ("g1", 1, "dialogue", "老数据一行"),
    )
    raw.commit()
    assert "group_id" not in _cols(raw, "timeline_messages")
    raw.close()

    # 用正式连接打开同一库 → init_db/_migrate 应补上两列,且老行仍在(新列为 NULL)。
    conn = get_connection(db_file)
    init_db(conn)
    cols = _cols(conn, "timeline_messages")
    assert "group_id" in cols and "participants" in cols
    row = conn.execute("SELECT * FROM timeline_messages WHERE text='老数据一行'").fetchone()
    assert row is not None
    assert row["group_id"] is None and row["participants"] is None


def test_add_and_read_group_fields_roundtrip(game):
    """新写入带 group_id/participants 的消息,两种模式读出都应带回这两个字段。"""
    repo, gid = game
    repo.add_timeline_message(
        gid, 2, type="dialogue", text="你那盘带子呢?",
        speaker_id="gambler", speaker_name="阿龙",
        target_id="reporter", target_name="小林", visibility="public",
        group_id="grp-x", participants=["gambler", "reporter", "police"],
    )
    for mode in ("player", "debug"):
        msgs = repo.get_timeline_messages(gid, mode=mode)
        m = next(x for x in msgs if x["text"] == "你那盘带子呢?")
        assert m["group_id"] == "grp-x"
        assert m["participants"] == ["gambler", "reporter", "police"]


def test_non_group_message_has_null_group_fields(game):
    """普通(非讨论)消息不带分组字段:group_id=None、participants=None。"""
    repo, gid = game
    repo.add_timeline_message(gid, 1, type="narration", text="酒馆里很安静。")
    m = next(x for x in repo.get_timeline_messages(gid, mode="debug")
             if x["text"] == "酒馆里很安静。")
    assert m["group_id"] is None
    assert m["participants"] is None
