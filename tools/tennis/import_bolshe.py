#!/usr/bin/env python3
"""Copy tennis data from the BOLSHE-Live-Center D1 database into the local SQLite database.

Only SELECT queries reach D1, in pages, through the wrangler login of the BOLSHE checkout.
`wrangler d1 export` is avoided on purpose: it blocks a live database while it runs.
Repeat runs update rows in place, and rows D1 no longer has are kept: matches leave D1 over
time, and the point of this database is that history stays.

    python tools/tennis/import_bolshe.py --bolshe ../BOLSHE-Live-Center
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE.parents[1] / "local-data" / "tennis" / "tennis.sqlite"
SOURCE = "bolshe-d1"
D1_NAME = "bolshe-live-center"
PAGE = 2000

# local table -> (D1 table, stable ORDER BY for paging)
TABLES = {
    "player": ("players", "player_id"),
    "tournament": ("tournaments", "tournament_key"),
    "match": ("matches", "match_id"),
    "match_player_stats": ("player_match_stats", "match_id, fs_id"),
    "match_odds": ("match_odds", "match_id"),
    "ranking_snapshot": ("ranking_snapshots", "tour, discipline, ranking_date, player_id"),
    "elo_snapshot": ("elo_snapshots", "player_id, rated_on"),
}


def d1_select(bolshe: Path, sql: str) -> list[dict]:
    cmd = f'npx --no-install wrangler d1 execute {D1_NAME} --remote --json --command "{sql}"'
    res = subprocess.run(cmd, cwd=bolshe, shell=True, capture_output=True, encoding="utf-8", timeout=180)
    if res.returncode != 0:
        raise RuntimeError(f"wrangler failed: {res.stdout[-400:]} {res.stderr[-400:]}")
    return json.loads(res.stdout)[0]["results"]


def local_columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]


def upsert(db: sqlite3.Connection, table: str, rows: list[dict], source: str = SOURCE) -> int:
    """Write the columns both sides have; columns of the source we do not model are dropped."""
    columns = local_columns(db, table)
    keep = [c for c in columns if c != "source"]
    names = ", ".join(f'"{c}"' for c in keep + ["source"])
    marks = ", ".join("?" for _ in keep + ["source"])
    sql = f'INSERT OR REPLACE INTO "{table}" ({names}) VALUES ({marks})'
    db.executemany(sql, [[row.get(c) for c in keep] + [source] for row in rows])
    return len(rows)


def copy_table(bolshe: Path, db: sqlite3.Connection, table: str) -> int:
    d1_table, order = TABLES[table]
    copied = offset = 0
    while True:
        rows = d1_select(bolshe, f"SELECT * FROM {d1_table} ORDER BY {order} LIMIT {PAGE} OFFSET {offset}")
        if rows:
            copied += upsert(db, table, rows)
            db.commit()
        print(f"  {table}: {copied}", flush=True)
        if len(rows) < PAGE:
            return copied
        offset += PAGE


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    return db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bolshe", type=Path, required=True, help="BOLSHE-Live-Center checkout")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()

    db = open_db(args.db)
    started = datetime.now(timezone.utc).isoformat()
    run = db.execute("INSERT INTO import_run (source, started_at) VALUES (?, ?)", (SOURCE, started)).lastrowid
    counts = {table: copy_table(args.bolshe, db, table) for table in TABLES}
    db.execute(
        "UPDATE import_run SET finished_at = ?, rows_json = ? WHERE run_id = ?",
        (datetime.now(timezone.utc).isoformat(), json.dumps(counts), run),
    )
    db.commit()
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
