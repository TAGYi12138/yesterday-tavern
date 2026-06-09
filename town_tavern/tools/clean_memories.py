"""清理库中已存在的空/无效记忆(历史脏数据修复脚本)。

只删除"空内容 / 纯模板头(如 [记忆梳理])/ 去模板后过短"的记忆,绝不动正常记忆。
判定逻辑与运行期写入校验(memory_engine.is_valid_memory_content)完全一致,
保证"清理标准"和"新写入拦截标准"统一。

用法(宿主机上,容器在跑时):
    docker compose exec daemon python -m town_tavern.tools.clean_memories
    # 只看会删哪些、不实际删除:
    docker compose exec daemon python -m town_tavern.tools.clean_memories --dry-run
    # 指定库 / 只清某局:
    python -m town_tavern.tools.clean_memories --db /data/town_tavern.db --game <game_id>
"""
from __future__ import annotations

import argparse
from pathlib import Path

from ..config import DB_PATH
from ..engine.memory_engine import is_valid_memory_content
from ..storage.db import get_connection
from ..storage.repository import Repository


def main() -> None:
    parser = argparse.ArgumentParser(description="清理空/无效记忆")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite 库路径")
    parser.add_argument("--game", default=None, help="只清理某个 game_id(默认全部)")
    parser.add_argument(
        "--dry-run", action="store_true", help="只列出将删除的记忆,不实际删除"
    )
    args = parser.parse_args()

    conn = get_connection(Path(args.db))
    repo = Repository(conn)
    rows = repo.list_all_memories(args.game)
    bad = [m for m in rows if m.id is not None and not is_valid_memory_content(m.content)]

    print(f"扫描 {len(rows)} 条记忆,发现 {len(bad)} 条空/无效记忆。")
    for m in bad:
        print(f"  - id={m.id} npc={m.npc_id} day={m.day} type={m.type.value} content={m.content!r}")

    if args.dry_run:
        print("(dry-run,未删除)")
        return
    if bad:
        repo.delete_memories([m.id for m in bad if m.id is not None])
        print(f"已删除 {len(bad)} 条。")
    else:
        print("无需清理。")


if __name__ == "__main__":
    main()
