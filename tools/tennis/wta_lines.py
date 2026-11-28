#!/usr/bin/env python3
"""The line each past match itself had, gathered in wta_match_line from what we may use:

  tennis-data     closing prices on the winner, tour-level main draws from 2007 (no handicap or total)
  bolshe-winline  Winline's last price and main handicap, from BOLSHE's copy, since 14 Oct 2026
  winline-feed    Winline's last price, main handicap and main total before the start, our own snapshots

Winline wins where it exists, being the line the broadcast shows. OddsPortal, BetExplorer and TennisExplorer
hold more but forbid collecting it without their consent, so a licensed source joins here when one is bought.
A full rebuild, cheap enough for the hourly parse unit.

    python tools/tennis/wta_lines.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tennis_plates  # noqa: E402
import wta_report  # noqa: E402

DEFAULT_DB = HERE.parents[1] / "local-data" / "tennis" / "tennis.sqlite"
# Sharpest first: Pinnacle's closing price, then Bet365, then the market's average.
BOOKMAKERS = (("PS", "Pinnacle"), ("B365", "Bet365"), ("Avg", "средняя по рынку"), ("EX", "Expekt"), ("LB", "Ladbrokes"), ("SJ", "Stan James"), ("UB", "Unibet"))
SAME_EVENT_DAYS = 16  # a match date may be the event's first day, and a Slam lasts two weeks
WOMEN = ("WTA", "World Tennis, Женщины")


def td_price(odds_json: str | None) -> dict | None:
    odds = json.loads(odds_json) if odds_json else {}
    for prefix, name in BOOKMAKERS:
        won, lost = odds.get(f"{prefix}W"), odds.get(f"{prefix}L")
        if won and lost:
            return {"winner_price": won, "loser_price": lost, "price_source": "tennis-data", "price_bookmaker": name}
    return None


def from_tennis_data(db: sqlite3.Connection, lines: dict) -> None:
    seasons = [r[0] for r in db.execute("SELECT DISTINCT season FROM td_match WHERE tour = 'WTA' ORDER BY season")]
    for season in seasons:
        wtas = [w for w in wta_report.wta_rows(db, season) if (w.get("level") or "") != "ITF"]
        if not wtas:
            continue
        for td, how, wta in wta_report.reconcile(wta_report.td_rows(db, season), wtas)["pairs"]:
            # Where the file names the other winner, whose price is whose cannot be trusted.
            price = td_price(td["odds_json"]) if how == "same" else None
            if price:
                lines[wta["match_key"]].update(price)


def pair_index(db: sqlite3.Connection) -> dict:
    index = defaultdict(list)
    for key, winner, loser, played in db.execute(
            """SELECT m.match_key, m.winner_id, m.loser_id, COALESCE(m.match_date, t.start_date)
               FROM wta_match m JOIN wta_tournament t ON t.group_id = m.group_id AND t.year = m.year
               WHERE m.discipline = 'S' AND m.winner_id IS NOT NULL AND m.loser_id IS NOT NULL"""):
        if played:
            index[frozenset((winner, loser))].append((played[:10], key, winner))
    return index


def find_match(index: dict, first: int, second: int, day: str) -> tuple[str, int] | None:
    """(match key, winner) of the pair's meeting nearest to that day."""
    wanted = date.fromisoformat(day[:10])
    near = [(abs((date.fromisoformat(played) - wanted).days), key, winner) for played, key, winner in index.get(frozenset((first, second)), [])]
    near = [row for row in near if row[0] <= SAME_EVENT_DAYS]
    if not near:
        return None
    _, key, winner = min(near, key=lambda row: row[0])
    return key, winner


def oriented(line: dict, first_won: bool, handicap, first_price, second_price) -> None:
    """Writes a handicap quoted for the first side from the winner's side."""
    line["handicap"] = handicap if first_won else -handicap
    line["handicap_winner_price"] = first_price if first_won else second_price
    line["handicap_loser_price"] = second_price if first_won else first_price


def from_bolshe(db: sqlite3.Connection, lines: dict, index: dict) -> None:
    rows = db.execute(
        """SELECT a.wta_id, b.wta_id, m.date_msk, o.pre1, o.pre2, o.hcap, o.hcap1, o.hcap2
           FROM match_odds o JOIN match m ON m.match_id = o.match_id
           JOIN player a ON a.fs_id = m.p1_fs_id JOIN player b ON b.fs_id = m.p2_fs_id
           WHERE a.wta_id IS NOT NULL AND b.wta_id IS NOT NULL AND o.pre1 IS NOT NULL AND m.is_doubles = 0 AND m.date_msk IS NOT NULL""")
    for id1, id2, day, pre1, pre2, hcap, hcap1, hcap2 in rows:
        first, second = int(id1), int(id2)
        found = find_match(index, first, second, day)
        if not found:
            continue
        key, winner = found
        first_won = winner == first
        line = lines[key]
        line.update(winner_price=pre1 if first_won else pre2, loser_price=pre2 if first_won else pre1,
                    price_source="bolshe-winline", price_bookmaker="Winline")
        if hcap is not None and hcap1 and hcap2:
            oriented(line, first_won, hcap, hcap1, hcap2)
            line["line_source"] = "bolshe-winline"


def main_rung(rungs: list[dict]) -> dict | None:
    """The rung with the most even prices is a ladder's main line, the same choice BOLSHE makes."""
    return min(rungs, key=lambda r: abs(r["odd1"] - r["odd2"])) if rungs else None


def closing_lines(rows: list[dict], start: datetime) -> dict:
    """The last price of each rung before the start: market -> value -> row."""
    closing: dict = defaultdict(dict)
    for row in rows:
        if datetime.fromisoformat(row["taken_at"]) < start:
            closing[row["market"]][row["value_key"]] = row
    return closing


def from_winline(db: sqlite3.Connection, lines: dict, index: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    names = tennis_plates.player_index(db)
    learned = tennis_plates.learned_players(db, names)
    marks = ", ".join("?" for _ in WOMEN)
    events = db.execute(f"SELECT wl_id, team1, team2, team1_id, team2_id, start_at FROM winline_event WHERE is_doubles = 0 AND circuit IN ({marks})", WOMEN).fetchall()
    for wl_id, team1, team2, id1, id2, start_at in events:
        if not start_at:
            continue
        start = datetime.fromisoformat(start_at)
        first = learned.get(id1) or tennis_plates.find_wta_id(team1, names)
        second = learned.get(id2) or tennis_plates.find_wta_id(team2, names)
        if start >= now or not first or not second:
            continue
        found = find_match(index, first, second, start_at)
        if not found:
            continue
        key, winner = found
        first_won = winner == first
        rows = [{"market": m, "value_key": k, "value": v, "odd1": a, "odd2": b, "taken_at": t} for m, k, v, a, b, t in db.execute(
            "SELECT market, value_key, value, odd1, odd2, taken_at FROM winline_line WHERE wl_id = ? ORDER BY taken_at", (wl_id,))]
        closing = closing_lines(rows, start)
        line = lines[key]
        win = closing.get("Победитель", {}).get("")
        if win:
            line.update(winner_price=win["odd1"] if first_won else win["odd2"], loser_price=win["odd2"] if first_won else win["odd1"],
                        price_source="winline-feed", price_bookmaker="Winline")
        handicap = main_rung(list(closing.get("Фора", {}).values()))
        if handicap:
            oriented(line, first_won, handicap["value"], handicap["odd1"], handicap["odd2"])
            line["line_source"] = "winline-feed"
        total = main_rung(list(closing.get("Тотал", {}).values()))
        if total:
            line.update(total=total["value"], over_price=total["odd1"], under_price=total["odd2"], line_source="winline-feed")


def build(db: sqlite3.Connection, now: datetime | None = None) -> Counter:
    lines: dict = defaultdict(dict)
    index = pair_index(db)
    # Later sources overwrite earlier ones, so the order is the priority.
    from_tennis_data(db, lines)
    from_bolshe(db, lines, index)
    from_winline(db, lines, index, now)
    columns = ("winner_price", "loser_price", "price_source", "price_bookmaker", "handicap", "handicap_winner_price",
               "handicap_loser_price", "total", "over_price", "under_price", "line_source")
    rows = [(key, *[line.get(c) for c in columns]) for key, line in lines.items() if line]
    db.execute("DELETE FROM wta_match_line")
    db.executemany(f"INSERT INTO wta_match_line (match_key, {', '.join(columns)}) VALUES ({', '.join('?' for _ in range(len(columns) + 1))})", rows)
    db.commit()
    counts = Counter(line.get("price_source") for line in lines.values() if line.get("price_source"))
    counts["with_handicap"] = sum(1 for line in lines.values() if line.get("handicap") is not None)
    counts["with_total"] = sum(1 for line in lines.values() if line.get("total") is not None)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    db = sqlite3.connect(args.db, timeout=60)
    db.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    print(json.dumps(build(db), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
