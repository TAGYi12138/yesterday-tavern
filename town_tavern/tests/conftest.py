"""pytest 公共夹具:每个测试用独立的临时 SQLite 库,互不污染。

必须在 import 任何会读取 DB_PATH 的模块【之前】设置 TOWN_TAVERN_DB,因此这里
用 fixture 直接构造 Repository 时传入一条独立连接,避免依赖全局 DB_PATH。
"""
import sqlite3

import pytest

from town_tavern.storage.db import get_connection
from town_tavern.storage.repository import Repository


@pytest.fixture()
def repo(tmp_path) -> Repository:
    db_file = tmp_path / "test_tavern.db"
    conn: sqlite3.Connection = get_connection(db_file)
    return Repository(conn)


@pytest.fixture()
def game(repo: Repository):
    """返回 (repo, game_id),已载入种子 NPC 与世界。"""
    game_id = repo.create_game()
    return repo, game_id
