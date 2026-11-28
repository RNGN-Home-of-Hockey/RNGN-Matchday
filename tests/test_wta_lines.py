"""Each past match gets the line it had itself, from the winner's side. Names and prices are invented."""
import json
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "tennis"))

import winline_snapshot as ws  # noqa: E402
import wta_lines as wl  # noqa: E402
import tennis_plates as wp  # noqa: E402

ANNA, BEA = 1, 2
AFTER = datetime(2026, 12, 1, tzinfo=timezone.utc)


def make_db():
    db = sqlite3.connect(":memory:")
    db.executescript((ROOT / "tools" / "tennis" / "schema.sql").read_text(encoding="utf-8"))
    db.execute("INSERT INTO wta_tournament (group_id, year, name, surface, start_date, end_date, level, source) VALUES (7, 2026, 'ГОРОД', 'Hard', '2026-10-20', '2026-10-27', 'WTA 250', 't')")
    db.execute("INSERT INTO wta_player (player_id, first_name, last_name, full_name, source) VALUES (1, 'Anna', 'Alpha', 'Anna Alpha', 't')")
    db.execute("INSERT INTO wta_player (player_id, first_name, last_name, full_name, source) VALUES (2, 'Bea', 'Beta', 'Bea Beta', 't')")
    db.execute("INSERT INTO player (player_id, name_ru, fs_id, wta_id, tour, source) VALUES ('a', 'Анна Альфа', 'F1', '1', 'WTA', 't')")
    db.execute("INSERT INTO player (player_id, name_ru, fs_id, wta_id, tour, source) VALUES ('b', 'Бея Бета', 'F2', '2', 'WTA', 't')")
    db.execute("""INSERT INTO wta_match (match_key, group_id, year, discipline, draw, status, winner_id, loser_id, score, match_date, seen_in, reports, source)
                  VALUES ('K', 7, 2026, 'S', 'M', 'completed', 1, 2, '6-3  6-4', '2026-10-22', 'tournament', 1, 't')""")
    return db


def td(db, winner="Alpha A.", loser="Beta B.", odds=None, key="x"):
    db.execute("""INSERT INTO td_match (td_key, tour, season, file, tournament, match_date, winner, loser, odds_json, comment, source)
                  VALUES (?, 'WTA', 2026, 'f', 'Город', '2026-10-22', ?, ?, ?, 'Completed', 'tennis-data')""",
               (key, winner, loser, json.dumps(odds if odds is not None else {"PSW": 1.5, "PSL": 2.6, "B365W": 1.44, "B365L": 2.75})))


def bolshe(db, p1="F1", p2="F2", pre1=1.6, pre2=2.3, hcap=-2.5, hcap1=1.9, hcap2=1.9, day="2026-10-22", wl_id="500"):
    db.execute("INSERT INTO match (match_id, p1_fs_id, p2_fs_id, date_msk, is_doubles, source) VALUES ('M', ?, ?, ?, 0, 't')", (p1, p2, day))
    db.execute("INSERT INTO match_odds (match_id, wl_id, pre1, pre2, hcap, hcap1, hcap2, source) VALUES ('M', ?, ?, ?, ?, ?, ?, 't')", (wl_id, pre1, pre2, hcap, hcap1, hcap2))


def feed(db, team1="Альфа А.", team2="Бета Б.", lines=(), wl_id="900", start="2026-10-22T13:00:00.000+03:00", taken="2026-10-22T09:00:00+00:00"):
    event = {"wl_id": wl_id, "circuit": "WTA", "tournament": "Город, Страна", "team1": team1, "team2": team2, "team1_id": "11", "team2_id": "22",
             "start_at": start, "url": "", "is_doubles": 0}
    ws.store(db, [event], [{"wl_id": wl_id, "market": m, "value": v, "odd1": a, "odd2": b} for m, v, a, b in lines], taken)


def line(db):
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM wta_match_line WHERE match_key = 'K'").fetchone()
    db.row_factory = None
    return dict(row) if row else None


class Prices(unittest.TestCase):
    def test_tennis_data_prefers_pinnacle_and_says_whose(self):
        db = make_db()
        td(db)
        wl.build(db, AFTER)
        row = line(db)
        self.assertEqual((row["winner_price"], row["loser_price"], row["price_source"], row["price_bookmaker"]), (1.5, 2.6, "tennis-data", "Pinnacle"))
        self.assertIsNone(row["handicap"])

    def test_it_falls_back_when_pinnacle_has_no_price(self):
        self.assertEqual(wl.td_price(json.dumps({"B365W": 1.44, "B365L": 2.75}))["price_bookmaker"], "Bet365")
        self.assertIsNone(wl.td_price(json.dumps({"PSW": 1.5})))
        self.assertIsNone(wl.td_price(None))

    def test_a_file_naming_the_other_winner_gives_no_price(self):
        db = make_db()
        td(db, winner="Beta B.", loser="Alpha A.")
        wl.build(db, AFTER)
        self.assertIsNone(line(db))

    def test_bolshe_handicap_is_turned_to_the_winner(self):
        db = make_db()
        bolshe(db, p1="F2", p2="F1", pre1=2.3, pre2=1.6, hcap=2.5, hcap1=1.8, hcap2=2.0)
        wl.build(db, AFTER)
        row = line(db)
        self.assertEqual((row["winner_price"], row["loser_price"]), (1.6, 2.3))
        self.assertEqual((row["handicap"], row["handicap_winner_price"], row["handicap_loser_price"]), (-2.5, 2.0, 1.8))
        self.assertEqual(row["line_source"], "bolshe-winline")

    def test_winline_beats_tennis_data_for_the_price(self):
        db = make_db()
        td(db)
        bolshe(db)
        wl.build(db, AFTER)
        self.assertEqual((line(db)["winner_price"], line(db)["price_bookmaker"]), (1.6, "Winline"))

    def test_a_meeting_far_from_that_day_is_another_match(self):
        db = make_db()
        bolshe(db, day="2026-03-01")
        wl.build(db, AFTER)
        self.assertIsNone(line(db))


class Feed(unittest.TestCase):
    def test_closing_prices_and_main_rungs(self):
        db = make_db()
        feed(db, lines=(("Победитель", None, 1.7, 2.1), ("Фора", -1.5, 1.6, 2.3), ("Фора", -3.5, 1.95, 1.85), ("Тотал", 20.5, 1.5, 2.5), ("Тотал", 21.5, 1.9, 1.9)))
        wl.build(db, AFTER)
        row = line(db)
        self.assertEqual((row["winner_price"], row["handicap"], row["total"], row["line_source"]), (1.7, -3.5, 21.5, "winline-feed"))
        self.assertEqual((row["over_price"], row["under_price"]), (1.9, 1.9))

    def test_a_price_after_the_start_is_not_the_closing_one(self):
        db = make_db()
        feed(db, lines=(("Победитель", None, 1.7, 2.1),))
        feed(db, lines=(("Победитель", None, 1.1, 7.0),), taken="2026-10-22T11:00:00+00:00")  # 14:00 МСК, after the 13:00 start
        wl.build(db, AFTER)
        self.assertEqual(line(db)["winner_price"], 1.7)

    def test_the_second_team_won(self):
        db = make_db()
        feed(db, team1="Бета Б.", team2="Альфа А.", lines=(("Победитель", None, 2.1, 1.7), ("Фора", 1.5, 1.8, 2.0)))
        wl.build(db, AFTER)
        row = line(db)
        self.assertEqual((row["winner_price"], row["loser_price"]), (1.7, 2.1))
        self.assertEqual((row["handicap"], row["handicap_winner_price"]), (-1.5, 2.0))

    def test_a_match_not_started_gets_nothing(self):
        db = make_db()
        feed(db, lines=(("Победитель", None, 1.7, 2.1),))
        wl.build(db, datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertIsNone(line(db))

    def test_unknown_players_get_nothing(self):
        db = make_db()
        feed(db, team1="Неизвестная Н.", lines=(("Победитель", None, 1.7, 2.1),))
        wl.build(db, AFTER)
        self.assertIsNone(line(db))


class Rebuild(unittest.TestCase):
    def test_a_repeat_gives_the_same_rows(self):
        db = make_db()
        td(db)
        wl.build(db, AFTER)
        first = db.execute("SELECT * FROM wta_match_line").fetchall()
        wl.build(db, AFTER)
        self.assertEqual(db.execute("SELECT * FROM wta_match_line").fetchall(), first)


class Plates(unittest.TestCase):
    def test_history_carries_each_matchs_price_and_counts_favourite_and_underdog(self):
        db = make_db()
        td(db)
        wl.build(db, AFTER)
        rows = wp.load_history(db)
        self.assertEqual((rows[ANNA][0]["price"], rows[ANNA][0]["opp_price"]), (1.5, 2.6))
        self.assertEqual((rows[BEA][0]["price"], rows[BEA][0]["opp_price"]), (2.6, 1.5))
        self.assertEqual(wp.priced_record(rows[ANNA])["favourite"], (1, 0))
        self.assertEqual(wp.priced_record(rows[BEA])["underdog"], (0, 1))

    def test_no_prices_means_no_record(self):
        self.assertIsNone(wp.priced_record([{"won": True, "price": None, "opp_price": None}]))


if __name__ == "__main__":
    unittest.main()
