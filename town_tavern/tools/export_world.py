"""把运行中游戏世界的【所有】信息从 SQLite 完整导出,便于离线分析。

设计目标:**一条命令导出所有信息**,不依赖业务模型代码,直接读底层 SQLite,
因此即便将来加表/加列也会被自动带上(通用全表转储)。

输出三份(默认写到 /data/exports/,正好落在 docker 的 tavern-data 卷里,容器删了也不丢):
  1. world_full_<时间戳>.json   —— 所有库、所有表、所有行的原始转储(机器可读,真·全量)
  2. world_report_<时间戳>.md   —— 人话版:按 game / 按天 串起世界数值、冲突、关系、记忆、事件、线索
  3. 同样的内容打印到 stdout(方便 `docker compose logs` / 重定向)

用法(在宿主机上):
    # 容器在跑:
    docker compose exec daemon python -m town_tavern.tools.export_world
    # 然后把导出文件从卷里拷出来:
    docker compose cp daemon:/data/exports ./exports

    # 或者一次性临时容器(守护没开也行,共享同一数据卷):
    docker compose run --rm daemon python -m town_tavern.tools.export_world

    # 指定库 / 输出目录:
    python -m town_tavern.tools.export_world --db /data/town_tavern.db --out /data/exports
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

# 这些列里存的是 JSON 字符串,导出时顺手解析成对象,分析时更直观。
_JSON_COLUMNS = {"actors", "consequences", "participants"}


def _default_db_path() -> str:
    """优先用环境变量(容器里就是 /data/town_tavern.db),否则退回包内默认。"""
    env = os.environ.get("TOWN_TAVERN_DB")
    if env:
        return env
    return str(Path(__file__).resolve().parent.parent / "town_tavern.db")


def _connect(db_path: str) -> sqlite3.Connection:
    # 只读方式打开,绝不动到正在运行的世界。
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _list_tables(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def _maybe_json(col: str, val: Any) -> Any:
    if col in _JSON_COLUMNS and isinstance(val, str) and val.strip():
        try:
            return json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return val
    return val


def _dump_table(conn: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 (表名来自 schema,可信)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({k: _maybe_json(k, r[k]) for k in r.keys()})
    return out


def dump_all(conn: sqlite3.Connection) -> Dict[str, Any]:
    """通用全量转储:每张表 → 全部行。真·所有信息。"""
    tables = _list_tables(conn)
    data: Dict[str, Any] = {
        "_meta": {
            "exported_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "tables": tables,
        }
    }
    for t in tables:
        data[t] = _dump_table(conn, t)
    return data


# --------------------------------------------------------------------------
# 人话版报告:把全量数据按 game / 天 重新组织,方便肉眼分析"是否按预期推进"
# --------------------------------------------------------------------------
def _by_game(rows: List[Dict[str, Any]], gid: str) -> List[Dict[str, Any]]:
    return [r for r in rows if r.get("game_id") == gid]


def _flag_human(key: str, value: Any) -> str:
    return f"{key} = {value}"


def build_report(data: Dict[str, Any]) -> str:
    games = [g["id"] for g in data.get("games", [])]
    if not games:
        return "(数据库里没有任何存档 game)\n"

    lines: List[str] = []
    lines.append(f"# 游戏世界全量快照  导出于 {data['_meta']['exported_at']}")
    lines.append(f"共 {len(games)} 局存档;包含表:{', '.join(data['_meta']['tables'])}\n")

    for gid in games:
        lines.append("=" * 72)
        lines.append(f"## 存档 game_id = {gid}")

        # ---- 世界数值 / flag(world_state 是 key-value)----
        ws = {r["key"]: r["value"] for r in _by_game(data.get("world_state", []), gid)}
        cur_day = ws.get("current_day", "?")
        lines.append(f"\n### 世界状态(当前第 {cur_day} 天)")
        flags = {k: v for k, v in ws.items() if k.startswith("flag_")}
        non_flags = {k: v for k, v in ws.items() if not k.startswith("flag_")}
        for k in sorted(non_flags):
            lines.append(f"  - {k}: {non_flags[k]}")
        if flags:
            lines.append("  剧情 flag(系统真相,玩家未必知道):")
            for k in sorted(flags):
                lines.append(f"    · {_flag_human(k, flags[k])}")

        # ---- NPC ----
        npcs = _by_game(data.get("npcs", []), gid)
        lines.append(f"\n### NPC({len(npcs)} 人)")
        for n in sorted(npcs, key=lambda x: x.get("id", "")):
            lines.append(
                f"  - {n.get('name')}({n.get('id')}) 状态={n.get('status')}"
                f" 到期日={n.get('status_until_day')} 压力={n.get('stress')}"
                f" 钱={n.get('money')} 目标={n.get('current_goal')}"
            )

        # ---- 关系(单向五维)----
        rels = _by_game(data.get("relationships", []), gid)
        if rels:
            lines.append(f"\n### 关系({len(rels)} 条单向)")
            for r in sorted(rels, key=lambda x: (x.get("from_npc", ""), x.get("to_npc", ""))):
                lines.append(
                    f"  - {r['from_npc']}→{r['to_npc']}: 信任{r['trust']}"
                    f" 恐惧{r['fear']} 怨恨{r['resentment']}"
                    f" 好感{r['affection']} 怀疑{r['suspicion']}"
                )

        # ---- 冲突 + 转移日志 ----
        confs = _by_game(data.get("conflicts", []), gid)
        clogs = _by_game(data.get("conflict_logs", []), gid)
        if confs:
            lines.append(f"\n### 冲突状态机({len(confs)} 桩)")
            for c in confs:
                lines.append(
                    f"  - [{c['id']}] kind={c['kind']} 状态={c['state']}"
                    f" 参与者={c.get('participants')} 在态天数={c.get('age_in_state')}"
                    f" 封顶={c.get('max_stall_days')} 创建日={c.get('created_day')}"
                )
        if clogs:
            lines.append("  状态转移日志(为什么走到这个结局):")
            for lg in sorted(clogs, key=lambda x: (x.get("conflict_id", ""), x.get("day", 0), x.get("id", 0))):
                lines.append(
                    f"    · 第{lg['day']}天 [{lg['conflict_id']}] "
                    f"{lg['from_state']} → {lg['to_state']}"
                    f"  原因:{lg.get('reason')}  后果:{lg.get('consequence_summary')}"
                )

        # ---- 玩家已知线索(与系统 flag 隔离)----
        clues = _by_game(data.get("player_known_clues", []), gid)
        if clues:
            lines.append(f"\n### 玩家已知线索({len(clues)} 条,≠ 系统 flag)")
            for cl in sorted(clues, key=lambda x: x.get("day_found", 0)):
                lines.append(
                    f"  - 第{cl['day_found']}天 [{cl['id']}] {cl['title']}"
                    f"(来源={cl.get('source')} 把握={cl.get('certainty')})"
                )

        # ---- 事件(按天)----
        events = _by_game(data.get("events", []), gid)
        if events:
            lines.append(f"\n### 事件流水({len(events)} 条,按天)")
            for ev in sorted(events, key=lambda x: (x.get("day", 0), x.get("id", 0))):
                vis = ev.get("visibility", "public")
                lines.append(
                    f"  - 第{ev['day']}天 〔{vis}〕[{ev['type']}] {ev['title']}:"
                    f" {ev['summary']} (相关:{ev.get('actors')})"
                )

        # ---- 记忆(按 NPC,守原始隔离视角)----
        mems = _by_game(data.get("memories", []), gid)
        if mems:
            lines.append(f"\n### 记忆({len(mems)} 条,按 NPC 分组——这是各人私有视角)")
            by_npc: Dict[str, List[Dict[str, Any]]] = {}
            for m in mems:
                by_npc.setdefault(m.get("npc_id", "?"), []).append(m)
            for npc_id in sorted(by_npc):
                lines.append(f"  [{npc_id}]")
                for m in sorted(by_npc[npc_id], key=lambda x: (x.get("day", 0), x.get("id", 0))):
                    lt = "长期" if m.get("is_long_term") else "短期"
                    lines.append(
                        f"    · 第{m['day']}天 ({lt}/{m['type']}/重要度{m.get('importance')}"
                        f"/{m.get('emotional_tag')}) {m['content']}"
                    )
        lines.append("")

    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出游戏世界全部信息")
    parser.add_argument("--db", default=_default_db_path(), help="SQLite 库路径")
    parser.add_argument("--out", default=None, help="输出目录(默认 库同级的 exports/)")
    parser.add_argument("--no-files", action="store_true", help="只打印到 stdout,不落文件")
    args = parser.parse_args(argv)

    db_path = args.db
    if not Path(db_path).exists():
        print(f"[导出失败] 找不到数据库:{db_path}")
        return 1

    conn = _connect(db_path)
    try:
        data = dump_all(conn)
    finally:
        conn.close()

    report = build_report(data)
    print(report)

    if not args.no_files:
        out_dir = Path(args.out) if args.out else (Path(db_path).resolve().parent / "exports")
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = out_dir / f"world_full_{ts}.json"
        md_path = out_dir / f"world_report_{ts}.md"
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(report, encoding="utf-8")
        print(f"\n[已写出] 全量 JSON: {json_path}")
        print(f"[已写出] 人话报告: {md_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
