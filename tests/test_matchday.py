"""The Matchday page's data and its small server. Names and prices are invented."""
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "tennis"))

import matchday_server as md  # noqa: E402
import winline_snapshot as ws  # noqa: E402

EVENT = {"wl_id": "100", "circuit": "WTA", "tournament": "Город, Страна", "team1": "Альфа А.", "team2": "Бета Б.", "team1_id": "11",
         "team2_id": "22", "start_at": "2026-11-05T13:00:00.000+03:00", "url": "https://x/100", "is_doubles": 0}
ATP = {**EVENT, "wl_id": "200", "circuit": "ATP", "team1": "Гамма Г.", "team2": "Дельта Д."}
WINNER = {"wl_id": "100", "market": "Победитель", "value": None, "odd1": 1.6, "odd2": 2.4}
TOTAL = {"wl_id": "100", "market": "Тотал", "value": 20.5, "odd1": 1.8, "odd2": 1.95}
HANDICAP = {"wl_id": "100", "market": "Фора", "value": -1.5, "odd1": 1.7, "odd2": 2.1}
SET1 = {"wl_id": "100", "market": "1 сет Исход 12", "value": None, "odd1": 1.5, "odd2": 2.5}


def make_db(path=":memory:"):
    db = sqlite3.connect(path)
    db.executescript((ROOT / "tools" / "tennis" / "schema.sql").read_text(encoding="utf-8"))
    db.execute("INSERT INTO player (player_id, name_ru, wta_id, tour, source) VALUES ('a', 'Анна Альфа', '1', 'WTA', 't')")
    db.execute("INSERT INTO player (player_id, name_ru, wta_id, tour, source) VALUES ('b', 'Бея Бета', '2', 'WTA', 't')")
    db.execute("INSERT INTO tournament (tournament_key, tour, tournament, surface, first_seen, source) VALUES ('k', 'WTA', 'Город', 'hard', '2026', 't')")
    db.execute("INSERT INTO wta_tournament (group_id, year, name, surface, start_date, level, source) VALUES (7, 2026, 'ГОРОД', 'Hard', '2026-06-01', 'WTA 250', 't')")
    db.execute("INSERT INTO wta_player (player_id, full_name, source) VALUES (1, 'Anna Alpha', 't')")
    db.execute("INSERT INTO wta_player (player_id, full_name, source) VALUES (2, 'Bea Beta', 't')")
    for n in range(12):
        db.execute("""INSERT INTO wta_match (match_key, group_id, year, discipline, draw, status, winner_id, loser_id, score, match_date, seen_in, reports, source)
                      VALUES (?, 7, 2026, 'S', 'M', 'completed', 1, ?, '6-3  6-4', ?, 'tournament', 1, 't')""",
                   (f"k{n}", 2 if n == 0 else 900 + n, f"2026-06-{n + 1:02d}"))
    db.commit()
    return db


class Markets(unittest.TestCase):
    def test_families_in_order_with_their_sides(self):
        rows = [{"market": m, "value_key": k, "value": v, "odd1": 1.9, "odd2": 1.9, "taken_at": "t1"} for m, k, v in (
            ("1 сет Исход 12", "", None), ("Тотал", "20.5", 20.5), ("Фора", "-1.5", -1.5), ("Победитель", "", None))]
        families = md.group_markets(rows, "А", "Б")
        self.assertEqual([f["name"] for f in families], ["Исход матча", "Фора по геймам", "Тотал геймов", "1 сет: исход"])
        self.assertEqual((families[0]["side1"], families[2]["side1"], families[3]["side2"]), ("А", "Больше", "Б"))

    def test_a_moved_price_shows_what_it_was(self):
        rows = [{"market": "Победитель", "value_key": "", "value": None, "odd1": 2.0, "odd2": 1.8, "taken_at": "t1"},
                {"market": "Победитель", "value_key": "", "value": None, "odd1": 1.8, "odd2": 1.81, "taken_at": "t2"}]
        rung = md.group_markets(rows, "А", "Б")[0]["rungs"][0]
        self.assertEqual((rung["odd1"], rung["was1"], rung["odd2"], rung["was2"]), (1.8, 2.0, 1.81, None))
        self.assertEqual(len(rung["history"]), 2)

    def test_rungs_are_sorted_by_line(self):
        rows = [{"market": "Тотал", "value_key": str(v), "value": v, "odd1": 1.9, "odd2": 1.9, "taken_at": "t1"} for v in (22.5, 19.5, 21.5)]
        self.assertEqual([r["value"] for r in md.group_markets(rows, "А", "Б")[0]["rungs"]], [19.5, 21.5, 22.5])


class Data(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "t.sqlite"
        self.raw = make_db(self.path)
        ws.store(self.raw, [EVENT, ATP], [WINNER, TOTAL, HANDICAP, SET1], "2026-11-04T10:00:00+00:00")
        self.db = md.open_db(self.path)
        self.history = md.History(self.path)
        self.now = datetime(2026, 11, 4, 10, 5, tzinfo=timezone.utc)

    def tearDown(self):
        self.db.close()
        self.raw.close()
        self.dir.cleanup()

    def test_list_gives_prices_and_counts_for_every_circuit(self):
        found = {m["id"]: m for m in md.list_matches(self.db, self.history, self.now)["matches"]}
        self.assertEqual((found["100"]["odd1"], found["100"]["markets"], found["100"]["in_line"]), (1.6, 4, True))
        self.assertEqual(found["200"]["circuit"], "ATP")
        self.assertIsNone(found["200"]["odd1"])

    def test_a_match_that_left_the_feed_stays_for_two_days(self):
        ws.store(self.raw, [ATP], [], "2026-11-04T11:00:00+00:00")
        found = {m["id"]: m for m in md.list_matches(self.db, self.history, self.now)["matches"]}
        self.assertFalse(found["100"]["in_line"])
        later = datetime(2026, 11, 8, tzinfo=timezone.utc)
        self.assertNotIn("100", {m["id"] for m in md.list_matches(self.db, self.history, later)["matches"]})

    def test_detail_has_markets_plates_and_history(self):
        detail = md.match_detail(self.db, self.history, "100")
        self.assertEqual(detail["url"], "https://x/100")
        self.assertEqual(detail["markets"][0]["name"], "Исход матча")
        self.assertTrue(detail["facts"])
        self.assertEqual([h["winner"] for h in detail["h2h"]], ["Anna Alpha"])
        self.assertEqual(len(detail["form"][0]["results"]), 12)
        self.assertEqual(detail["form"][0]["side"], 1)

    def test_form_gives_each_match_from_the_players_side(self):
        detail = md.match_detail(self.db, self.history, "100")
        winner_side = detail["form"][0]["results"][-1]
        self.assertEqual((winner_side["margin"], winner_side["total"], winner_side["won"]), (5, 19, True))
        loser_side = detail["form"][1]["results"][0]
        self.assertEqual((loser_side["margin"], loser_side["won"], loser_side["winner"]), (-5, False, "Anna Alpha"))

    def test_a_retirement_settles_nothing(self):
        row = {"date": "d", "won": True, "full": False, "margin": 3, "total": 11}
        self.assertEqual((md.result_of(row)["margin"], md.result_of(row)["total"]), (None, None))

    def test_doubles_get_the_markets_and_a_note(self):
        other = {**ATP, "wl_id": "300", "is_doubles": 1, "team1": "Гамма Г./Дельта Д."}
        ws.store(self.raw, [other], [{**WINNER, "wl_id": "300"}], "2026-11-04T10:10:00+00:00")
        detail = md.match_detail(self.db, self.history, "300")
        self.assertEqual(detail["markets"][0]["rungs"][0]["odd2"], 2.4)
        self.assertEqual((detail["lines"], detail["notes"]), ([], [md.PLATE_NOTE]))

    def test_the_mens_tour_goes_through_the_same_engine(self):
        ws.store(self.raw, [ATP], [{**WINNER, "wl_id": "200"}], "2026-11-04T10:10:00+00:00")
        detail = md.match_detail(self.db, self.history, "200")
        self.assertNotIn(md.PLATE_NOTE, detail["notes"])

    def test_unknown_match(self):
        self.assertIsNone(md.match_detail(self.db, self.history, "999"))

    def test_history_is_cached_until_it_is_due(self):
        self.history.get("WTA")
        self.raw.execute("DELETE FROM wta_match")
        self.raw.commit()
        self.assertTrue(self.history.get("WTA").players)
        self.history.ttl = 0
        self.assertFalse(self.history.get("WTA").players)


if __name__ == "__main__":
    unittest.main()
