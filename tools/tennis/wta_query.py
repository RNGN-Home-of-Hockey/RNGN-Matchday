#!/usr/bin/env python3
"""Ask the parsed WTA tables about a player: career, season, surface, tournament, matches.

    python tools/tennis/wta_query.py career "Rybakina"
    python tools/tennis/wta_query.py season "Rybakina" --year 2024
    python tools/tennis/wta_query.py surface 324166 --discipline D
    python tools/tennis/wta_query.py tournament "Rybakina" --event "WIMBLEDON"
    python tools/tennis/wta_query.py matches "Rybakina" --year 2024 --surface Grass

The same numbers are views for plain SQL: wta_career, wta_season, wta_surface,
wta_tournament_results, and wta_player_result for single matches.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[2] / "local-data" / "tennis" / "tennis.sqlite"
TOTALS = ("matches", "wins", "losses", "walkovers", "titles", "finals", "matches_with_stats", "aces", "df")
VIEWS = {
    "career": ("wta_career", ("circuit", "first_year", "last_year"), "circuit"),
    "season": ("wta_season", ("year", "circuit"), "year DESC, circuit"),
    "surface": ("wta_surface", ("surface", "circuit", "first_year", "last_year"), "matches DESC"),
    "tournament": ("wta_tournament_results", ("year", "tournament_name", "level", "surface"), "year DESC"),
}
MATCH_COLUMNS = ("year", "tournament_name", "level", "surface", "round", "won", "opponent_name", "score",
                 "status", "player_rank", "opponent_rank", "aces", "df")


def find_player(db: sqlite3.Connection, who: str) -> tuple[int, str]:
    if who.isdigit():
        found = db.execute("SELECT player_id, full_name FROM wta_player WHERE player_id = ?", (int(who),)).fetchall()
    else:
        found = db.execute(
            """SELECT player_id, full_name FROM wta_player WHERE full_name LIKE ? ORDER BY singles_rank IS NULL,
               singles_rank, full_name LIMIT 8""", (f"%{who}%",)).fetchall()
    if not found:
        raise SystemExit(f"no player matches {who!r}")
    exact = [p for p in found if p[1].lower() == who.lower()]
    if len(found) > 1 and not exact:
        print(f"{len(found)} players match, taking the first: " + ", ".join(f"{n} ({i})" for i, n in found), file=sys.stderr)
    return (exact or found)[0]


def run(db: sqlite3.Connection, what: str, player_id: int, discipline: str, year=None, event=None, surface=None):
    """Return (column names, rows)."""
    where, values = ["player_id = ?", "discipline = ?"], [player_id, discipline]
    if year and what in ("season", "tournament", "matches"):
        where.append("year = ?")
        values.append(year)
    if event and what in ("tournament", "matches"):
        where.append("(tournament_name LIKE ? OR tournament_title LIKE ?)")
        values += [f"%{event}%", f"%{event}%"]
    if surface and what in ("surface", "matches"):
        where.append("surface = ?")
        values.append(surface)
    if what == "matches":
        columns, order = MATCH_COLUMNS, "match_date DESC, round"
        view = "wta_player_result"
    else:
        view, keys, order = VIEWS[what]
        columns = keys + TOTALS
    sql = f"SELECT {', '.join(columns)} FROM {view} WHERE {' AND '.join(where)} ORDER BY {order}"
    return columns, db.execute(sql, values).fetchall()


def show(columns, rows) -> None:
    cells = [[("" if v is None else str(v)) for v in row] for row in rows]
    widths = [max([len(c)] + [len(row[i]) for row in cells]) for i, c in enumerate(columns)]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    for row in cells:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", choices=[*VIEWS, "matches"])
    parser.add_argument("player", help="WTA id or part of a name")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--discipline", choices=["S", "D"], default="S")
    parser.add_argument("--year", type=int)
    parser.add_argument("--event", help="part of the tournament's name")
    parser.add_argument("--surface", help="Hard, Clay, Grass, Carpet")
    args = parser.parse_args()
    db = sqlite3.connect(f"{args.db.resolve().as_uri()}?mode=ro", uri=True)
    player_id, name = find_player(db, args.player)
    print(f"{name} ({player_id})")
    show(*run(db, args.what, player_id, args.discipline, args.year, args.event, args.surface))
    return 0


if __name__ == "__main__":
    sys.exit(main())
