#!/usr/bin/env python3
"""Load the yearly tennis-data.co.uk files (raw/tennis-data/) into td_match in tennis.sqlite.

These are downloaded files, and .xls needs xlrd, .xlsx needs openpyxl. Keep both in a separate
environment and run with -I, so nothing next to the files or the script gets imported:

    <env>/Scripts/python.exe -I tools/tennis/import_tennis_data.py

A repeat run updates rows in place; the current year's file keeps growing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DIR = HERE.parents[1] / "local-data" / "tennis" / "raw" / "tennis-data"
DEFAULT_DB = HERE.parents[1] / "local-data" / "tennis" / "tennis.sqlite"
SOURCE = "tennis-data"
ODDS_PREFIXES = ("B365", "CB", "EX", "PS", "UB", "SB", "SJ", "LB", "GB", "IW", "BFE", "Max", "Avg")
EXCEL_EPOCH = date(1899, 12, 30)


def read_sheet(path: Path) -> list[dict]:
    if path.suffix.lower() == ".xls":
        import xlrd  # noqa: PLC0415 - only this format needs it
        sheet = xlrd.open_workbook(str(path)).sheet_by_index(0)
        header = [str(x).strip() for x in sheet.row_values(0)]
        rows = (sheet.row_values(i) for i in range(1, sheet.nrows))
    else:
        import openpyxl  # noqa: PLC0415 - only this format needs it
        sheet = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
        values = sheet.iter_rows(values_only=True)
        header = [str(x).strip() if x is not None else "" for x in next(values)]
        rows = values
    return [dict(zip(header, row)) for row in rows]


def as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def text(value):
    if value is None:
        return None
    return " ".join(str(value).split()) or None


def iso_date(value) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, (int, float)) and value:
        return (EXCEL_EPOCH + timedelta(days=int(value))).isoformat()
    found = text(value)
    if found is None:
        return None
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(found[:10], pattern).date().isoformat()
        except ValueError:
            continue
    return None


def parse_row(raw: dict, season: int, file: str) -> dict | None:
    tour = "WTA" if "WTA" in raw else "ATP"
    winner, loser = text(raw.get("Winner")), text(raw.get("Loser"))
    if not winner or not loser:
        return None
    sets = []
    for n in range(1, 6):
        won, lost = as_int(raw.get(f"W{n}")), as_int(raw.get(f"L{n}"))
        if won is not None and lost is not None:
            sets.append([won, lost])
    odds = {k: raw[k] for k in raw if k.startswith(ODDS_PREFIXES) and isinstance(raw[k], (int, float))}
    played = iso_date(raw.get("Date"))
    # A current-year file only grows, so these fields, not a row number, identify a match.
    identity = f"{tour}|{season}|{text(raw.get('Tournament'))}|{text(raw.get('Round'))}|{winner}|{loser}|{played}"
    return {
        "td_key": hashlib.sha1(identity.encode("utf-8")).hexdigest(), "tour": tour, "season": season, "file": file,
        "location": text(raw.get("Location")), "tournament": text(raw.get("Tournament")), "match_date": played,
        "tier": text(raw.get("Tier") or raw.get("Series")), "court": text(raw.get("Court")),
        "surface": text(raw.get("Surface")), "round": text(raw.get("Round")), "best_of": as_int(raw.get("Best of")),
        "winner": winner, "loser": loser, "winner_rank": as_int(raw.get("WRank")), "loser_rank": as_int(raw.get("LRank")),
        "winner_points": as_int(raw.get("WPts")), "loser_points": as_int(raw.get("LPts")),
        "sets_json": json.dumps(sets) if sets else None, "sets_w": as_int(raw.get("Wsets")), "sets_l": as_int(raw.get("Lsets")),
        "comment": text(raw.get("Comment")), "odds_json": json.dumps(odds) if odds else None, "source": SOURCE,
    }


def load_file(db: sqlite3.Connection, path: Path) -> int:
    folder = path.parent.name
    file = f"{folder}/{path.name}"
    rows = [r for r in (parse_row(raw, int(folder[:4]), file) for raw in read_sheet(path)) if r]
    columns = [row[1] for row in db.execute("PRAGMA table_info(td_match)")]
    db.executemany(
        f"INSERT OR REPLACE INTO td_match ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        [[row[c] for c in columns] for row in rows],
    )
    db.commit()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    db = sqlite3.connect(args.db)
    db.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    total = 0
    for path in sorted(args.dir.glob("*/*.xls*")):
        count = load_file(db, path)
        total += count
        print(f"{path.parent.name}/{path.name} {count}", flush=True)
    print(f"rows read: {total}")
    print(db.execute("SELECT tour, COUNT(*), MIN(season), MAX(season) FROM td_match GROUP BY tour").fetchall())
    return 0


if __name__ == "__main__":
    sys.exit(main())
