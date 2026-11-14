#!/usr/bin/env python3
"""Do the plate gates pick anything the market missed? Tried on finished tour matches at closing prices.

Each match sees only results dated before it. Profit is per one unit at the closing price, so a small
minus is the bookmaker's margin. Read-only; a measurement, never a promise.

    python tools/tennis/plates_backtest.py --from-year 2022
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tennis_plates as tp  # noqa: E402

DEFAULT_DB = HERE.parents[1] / "local-data" / "tennis" / "tennis.sqlite"


class Tally:
    def __init__(self):
        self.n = self.won = 0
        self.expected = self.profit = 0.0

    def add(self, won: bool, price: float, other: float) -> None:
        self.n += 1
        self.won += won
        self.expected += tp.fair_price(price, other)
        self.profit += price - 1 if won else -1

    def line(self) -> str:
        if not self.n:
            return "n=0"
        return (f"n={self.n}, выиграли {100 * self.won / self.n:.1f}%, рынок ждал {100 * self.expected / self.n:.1f}%, "
                f"на единицу {self.profit / self.n:+.3f}")


def run(db: sqlite3.Connection, tour: str, from_year: int) -> dict:
    players = tp.load_atp_history(db, tour)
    results = {name: Tally() for name in ("все фавориты", "все андердоги", "старый отбор: 60% на покрытии", "новый отбор: фаворит в той же роли",
                                          "новый отбор: андердог в той же роли")}
    since = f"{from_year}-01-01"
    for rows in players.values():
        for row in rows:
            if not row["date"] or row["date"] < since or not row.get("price") or not row.get("opp_price") or row["price"] == row["opp_price"]:
                continue
            price, other, won = row["price"], row["opp_price"], row["won"]
            favourite = price < other
            results["все фавориты" if favourite else "все андердоги"].add(won, price, other)
            pool = tp.recent(rows, row["date"], row["surface"], limit=tp.ROLE_LIMIT)
            last = pool[:tp.SURFACE_LIMIT]
            if tp.priced(price, other, sum(r["won"] for r in last), len(last)):
                results["старый отбор: 60% на покрытии"].add(won, price, other)
            role = tp.same_role(pool, favourite)
            if tp.priced(price, other, sum(r["won"] for r in role), len(role), tp.MIN_ROLE, 0.0):
                results["новый отбор: фаворит в той же роли" if favourite else "новый отбор: андердог в той же роли"].add(won, price, other)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--from-year", type=int, default=2022)
    args = parser.parse_args()
    db = sqlite3.connect(f"{args.db.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    for tour in tp.TOURS:
        print(tour)
        for name, tally in run(db, tour, args.from_year).items():
            print(f"  {name}: {tally.line()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
