"""Winline's line kept with its history, and plates drafted from it. Names and prices are invented."""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "tennis"))

import winline_snapshot as ws  # noqa: E402
import tennis_plates as wp  # noqa: E402

FEED = """<Feed><Sport Name="Футбол" Id="1"><Country Name="Лига"><Tournament Name="Кубок"><Match Team1="А" Team2="Б" Id="9">
<line freetext="Победитель" name1="1" odd1="2.0" name2="2" odd2="2.0"></line></Match></Tournament></Country></Sport>
<Sport Name="Теннис" Id="5"><Country Name="WTA" Id="6"><Tournament Name="Город, Страна" Id="7">
<Match Team1="Альфа А." Team2="Бета Б." Id1="11" Id2="22" MatchDate="2026-11-05T13:00:00.000+03:00" MatchUrl="https://x/1" islive="0" Id="100">
<line freetext="Победитель" name1="1" odd1="1.60" name2="2" odd2="2.30"></line>
<line freetext="Фора" value="-1.5" name1="1" odd1="1.70" name2="2" odd2="2.10"></line>
<line freetext="Тотал" value="20.5" name1="Больше" odd1="1.80" name2="Меньше" odd2="1.95"></line>
</Match></Tournament>
<Tournament Name="Город, Страна, Пары" Id="8"><Match Team1="Альфа А./Гамма Г." Team2="Бета Б./Дельта Д." Id="101">
<line freetext="Победитель" name1="1" odd1="1.90" name2="2" odd2="1.90"></line></Match></Tournament>
</Country></Sport></Feed>"""


def fresh_db():
    db = sqlite3.connect(":memory:")
    db.executescript((ROOT / "tools" / "tennis" / "schema.sql").read_text(encoding="utf-8"))
    return db


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        self.events, self.lines = ws.parse_feed(FEED)

    def test_only_tennis_is_read_and_doubles_are_marked(self):
        self.assertEqual([(e["wl_id"], e["circuit"], e["is_doubles"]) for e in self.events], [("100", "WTA", 0), ("101", "WTA", 1)])
        self.assertEqual({l["market"] for l in self.lines}, {"Победитель", "Фора", "Тотал"})

    def test_a_feed_without_tennis_gives_nothing(self):
        self.assertEqual(ws.parse_feed("<Feed></Feed>"), ([], []))

    def test_a_price_is_stored_only_when_it_changes(self):
        self.assertEqual(ws.store(self.db, self.events, self.lines, "t1"), 4)
        self.assertEqual(ws.store(self.db, self.events, self.lines, "t2"), 0)
        moved = [{**l, "odd1": 1.55} if l["market"] == "Победитель" and l["wl_id"] == "100" else l for l in self.lines]
        self.assertEqual(ws.store(self.db, self.events, moved, "t3"), 1)
        history = self.db.execute("SELECT taken_at, odd1 FROM winline_line WHERE wl_id='100' AND market='Победитель' ORDER BY taken_at").fetchall()
        self.assertEqual(history, [("t1", 1.6), ("t3", 1.55)])

    def test_current_view_shows_the_last_price_of_every_rung(self):
        ws.store(self.db, self.events, self.lines, "t1")
        moved = [{**l, "odd1": 1.55} if l["market"] == "Победитель" and l["wl_id"] == "100" else l for l in self.lines]
        ws.store(self.db, self.events, moved, "t2")
        rows = dict(self.db.execute("SELECT market, odd1 FROM winline_current WHERE wl_id='100'"))
        self.assertEqual(rows, {"Победитель": 1.55, "Фора": 1.7, "Тотал": 1.8})

    def test_event_keeps_its_first_sighting(self):
        ws.store(self.db, self.events, self.lines, "t1")
        ws.store(self.db, self.events, self.lines, "t2")
        self.assertEqual(self.db.execute("SELECT first_seen, last_seen FROM winline_event WHERE wl_id='100'").fetchone(), ("t1", "t2"))


def result(won, margin=4, total=20, full=True, day="2026-10-01", surface="hard", opp=999):
    return {"date": day, "surface": surface, "full": full, "total": total, "won": won, "margin": margin, "opp": opp}


EVENT = {"wl_id": "100", "team1": "Альфа А.", "team2": "Бета Б.", "tournament": "Город, Страна",
         "start_at": "2026-11-05T13:00:00.000+03:00", "url": "u"}


def lines_of(*rows):
    return [{"market": m, "value": v, "odd1": a, "odd2": b} for m, v, a, b in rows]


class Gate(unittest.TestCase):
    def test_price_range_and_sample(self):
        self.assertIsNone(wp.priced(1.40, 3.0, 18, 20))
        self.assertIsNone(wp.priced(5.0, 1.15, 18, 20))
        self.assertIsNone(wp.priced(1.8, 2.0, 9, 9))
        self.assertIsNone(wp.priced(1.8, 2.0, 11, 20))
        self.assertIsNotNone(wp.priced(1.8, 2.0, 18, 20))

    def test_a_record_the_market_already_expects_does_not_pass(self):
        self.assertIsNone(wp.priced(1.52, 2.6, 13, 20))

    def test_the_margin_is_taken_out_of_what_the_market_expects(self):
        self.assertAlmostEqual(wp.fair_price(1.9, 1.9), 0.5)


def priced_result(won, price=2.2, opp=1.6):
    return {**result(won), "price": price, "opp_price": opp}


def plates_for(prices=(2.2, 1.6), form=None, hist=None, pool=None, h2h=None, info=None, surface="hard"):
    empty = {1: [], 2: []}
    lines = lines_of(("Победитель", None, *prices)) if prices else []
    price = wp.winner_prices(lines)
    found = wp.candidates(EVENT, price, form or empty, hist or empty, pool or empty, h2h or [], info or {1: None, 2: None}, surface)
    return wp.select_plates(EVENT, price, found)


class Plates(unittest.TestCase):
    def test_a_match_with_only_a_price_still_gets_a_plate(self):
        cards = plates_for()
        self.assertEqual([c["text"] for c in cards], ["Фаворит у букмекеров: Бета Б., цена 1.6"])
        self.assertEqual(cards[0]["price_text"], "Победа Бета Б. — 1.6")
        self.assertIn("рынок ждёт", cards[0]["why"])

    def test_no_price_and_no_history_means_no_plate(self):
        self.assertEqual(plates_for(prices=None), [])

    def test_a_run_ranks_above_ordinary_form(self):
        form = {1: [result(True)] * 6 + [result(False)] * 4, 2: []}
        cards = plates_for(form=form)
        self.assertEqual(cards[0]["text"], "Альфа А.: побед подряд — 6")
        self.assertEqual((cards[0]["hits"], cards[0]["n"]), (6, 6))
        self.assertIn("Альфа А.", cards[0]["price_text"])

    def test_a_losing_run_is_a_story_too(self):
        cards = plates_for(form={1: [result(False)] * 4 + [result(True)], 2: []})
        self.assertEqual(cards[0]["text"], "Альфа А.: поражений подряд — 4")

    def test_a_record_counts_in_todays_role(self):
        pool = {1: [priced_result(False)] * 6 + [priced_result(True, 1.5, 2.6)] * 9, 2: []}
        role = [c for c in plates_for(pool=pool) if c["family"] == "role"][0]
        self.assertEqual(role["text"], "Альфа А.: андердогом 0-6")
        self.assertIn("андердог", role["what"])
        self.assertEqual(role["score"], 75)

    def test_head_to_head_shutout_ranks_high(self):
        cards = plates_for(h2h=[result(False)] * 3)
        self.assertEqual(cards[0]["text"], "Личные встречи: Бета Б. ведёт 3-0")

    def test_better_ranked_underdog_is_a_story(self):
        info = {1: {"rank": 12}, 2: {"rank": 90}}
        self.assertEqual(plates_for(info=info)[0]["family"], "rank")
        self.assertEqual(plates_for(prices=(1.6, 2.2), info=info)[-1]["family"], "market")

    def test_elo_against_the_market(self):
        info = {1: {"elo": 2000}, 2: {"elo": 1600}}
        card = [c for c in plates_for(info=info) if c["family"] == "elo"][0]
        self.assertEqual(card["score"], 70)
        self.assertEqual(card["text"], "По Эло Альфа А. 91 %, рынок 42 %")

    def test_surface_elo_is_preferred(self):
        self.assertEqual(wp.elo_of({"elo": 1500, "elo_hard": 1700}, "hard"), 1700)
        self.assertEqual(wp.elo_of({"elo": 1500}, "hard"), 1500)
        self.assertIsNone(wp.elo_of(None, "hard"))

    def test_at_most_a_few_plates(self):
        form = {1: [result(True)] * 8, 2: [result(False)] * 8}
        info = {1: {"rank": 12, "elo": 2000}, 2: {"rank": 90, "elo": 1600}}
        self.assertLessEqual(len(plates_for(form=form, info=info, h2h=[result(True)] * 3)), wp.MAX_PLATES)

    def test_wording_never_calls_a_bet_good(self):
        form = {1: [result(True)] * 8, 2: [result(False)] * 8}
        for card in plates_for(form=form, info={1: {"rank": 12, "elo": 2000}, 2: {"rank": 90, "elo": 1600}}):
            words = (card["text"] + card["what"] + card.get("why", "")).lower()
            for word in ("выгод", "ставь", "ставк", "верняк", "ценн"):
                self.assertNotIn(word, words)


class EnglishNames(unittest.TestCase):
    def test_either_order_is_one_name(self):
        self.assertEqual(wp.name_words("Shelton, Ben"), wp.name_words("Ben Shelton"))
        self.assertEqual(wp.name_words("Auger-Aliassime, Felix"), wp.name_words("Felix Auger Aliassime"))

    def test_women_link_through_the_index(self):
        index = wp.english_index([("Anna Alpha", 1), ("Bea Beta", 2)])
        self.assertEqual(wp.by_english("Alpha, Anna", index, {}), 1)
        self.assertIsNone(wp.by_english("Anna Other", index, {}))

    def test_a_shared_name_links_nobody(self):
        index = wp.english_index([("Anna Alpha", 1), ("Alpha Anna", 2), ("Bea Beta", 3)])
        self.assertIsNone(index.get(wp.name_words("Anna Alpha")))
        self.assertEqual(index[wp.name_words("Bea Beta")], 3)

    def test_men_need_a_history_under_that_key(self):
        players = {"shelton|b": [result(True)]}
        self.assertEqual(wp.by_english("Ben Shelton", None, players), "shelton|b")
        self.assertEqual(wp.by_english("Shelton, Ben", None, players), "shelton|b")
        self.assertIsNone(wp.by_english("Bob Shelton Junior", None, players))

    def test_a_one_word_or_empty_name_links_nobody(self):
        self.assertIsNone(wp.by_english("Shelton", None, {"shelton|b": []}))
        self.assertIsNone(wp.by_english(None, None, {}))

    def test_build_links_by_the_latin_name_when_the_russian_one_fails(self):
        event = {**EVENT, "team1": "Иначе И.", "team2": "Совсем С.", "team1_en": "Anna Alpha", "team2_en": "Bea Beta"}
        built = wp.build(event, [], {}, {}, {"город": "hard"}, None, None, wp.english_index([("Anna Alpha", 1), ("Bea Beta", 2)]))
        self.assertTrue(built["linked"])

    def test_feed_names_reach_the_event(self):
        db = fresh_db()
        events, lines = ws.parse_feed(FEED)
        ws.store(db, events, lines, "t1")
        text = """<Sport Name="Tennis"><Match Team1="Anna Alpha" Team2="Beta, Bea" Id="100"></Match></Sport>"""
        ws.store_names(db, ws.parse_names(text))
        self.assertEqual(db.execute("SELECT team1_en, team2_en FROM winline_event WHERE wl_id='100'").fetchone(), ("Anna Alpha", "Beta, Bea"))
        self.assertEqual(ws.parse_names("<Feed></Feed>"), [])

    def test_an_old_database_gets_the_columns(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE winline_event (wl_id TEXT)")
        ws.add_name_columns(db)
        ws.add_name_columns(db)
        self.assertEqual([r[1] for r in db.execute("PRAGMA table_info(winline_event)")], ["wl_id", "team1_en", "team2_en"])


class Circuits(unittest.TestCase):
    def test_winline_circuits_to_tours(self):
        for circuit, tour in (("ATP", "ATP"), ("World Tennis, Мужчины", "ATP"), ("Челленджер (М)", "ATP"), ("UTR (М)", "ATP"),
                              ("WTA", "WTA"), ("World Tennis, Женщины", "WTA"), ("UTR (Ж)", "WTA")):
            self.assertEqual(wp.circuit_tour(circuit), tour)


class BolsheFallback(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        self.db.execute("INSERT INTO player (player_id, fs_id, name_ru, tour, singles_rank, elo, source) VALUES ('a', 'F1', 'Анна Альфа', 'WTA', 300, 1500, 't')")
        self.db.execute("INSERT INTO player (player_id, fs_id, name_ru, tour, singles_rank, elo, source) VALUES ('b', 'F2', 'Бея Бета', 'WTA', 450, 1400, 't')")
        self.db.execute("INSERT INTO tournament (tournament_key, tour, tournament, surface, first_seen, source) VALUES ('k', 'WTA', 'W35 Город', 'clay', '2026', 't')")
        for n in range(6):
            self.db.execute("""INSERT INTO match (match_id, tournament_key, p1_fs_id, p2_fs_id, p1_name, p2_name, date_msk, status, winner, sets_json, is_doubles, source)
                               VALUES (?, 'k', 'F1', 'F2', 'Альфа А.', 'Бета Б.', ?, 'finished', 1, '[{"p1":6,"p2":3},{"p1":6,"p2":4}]', 0, 't')""", (f"M{n}", f"2026-10-{n + 10}"))
        self.db.execute("INSERT INTO match (match_id, tournament_key, p1_fs_id, p2_fs_id, status, is_doubles, source) VALUES ('NEXT', 'k', 'F2', 'F1', 'scheduled', 0, 't')")
        self.db.execute("INSERT INTO match_odds (match_id, wl_id, pre1, pre2, source) VALUES ('NEXT', '100', 2.3, 1.6, 't')")
        self.bolshe = wp.load_bolshe(self.db)

    def test_rows_from_each_side(self):
        rows = self.bolshe.players["F2"]
        self.assertEqual((len(rows), rows[0]["won"], rows[0]["margin"], rows[0]["total"]), (6, False, -5, 19))
        self.assertEqual(rows[0]["score"], "6-3  6-4")

    def test_link_by_name_and_by_the_matched_event(self):
        event = {**EVENT, "circuit": "World Tennis, Женщины", "team1": "Альфа А.", "team2": "Незнакомая Н."}
        self.assertEqual(self.bolshe.link(event, {}), ("F1", "F2"))

    def test_link_by_prices_when_no_name_fits(self):
        event = {**EVENT, "circuit": "World Tennis, Женщины", "team1": "Иная И.", "team2": "Другая Д."}
        self.assertEqual(self.bolshe.link(event, {1: 2.3, 2: 1.6}), ("F2", "F1"))
        self.assertEqual(self.bolshe.link(event, {1: 1.6, 2: 2.3}), ("F1", "F2"))

    def test_an_event_without_a_base_of_its_own_gets_stories(self):
        event = {**EVENT, "circuit": "World Tennis, Женщины", "tournament": "Город, Страна"}
        built = wp.build(event, lines_of(("Победитель", None, 1.6, 2.3)), {}, {}, {}, None, self.bolshe)
        self.assertEqual(built["history"], "bolshe")
        self.assertTrue(built["linked"])
        self.assertEqual(built["surface"], "clay")
        self.assertEqual(built["lines"][0]["text"], "Альфа А.: побед подряд — 6")
        self.assertTrue(any("месяц" in n for n in built["notes"]))


class AtpHistory(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        for n, (winner, loser, day, comment) in enumerate((("Shelton B.", "De Minaur A.", "2026-10-01", "Completed"),
                                                           ("De Minaur A.", "Shelton B.", "2026-09-01", "Retired"),
                                                           ("Shelton B.", "De Minaur A.", "2026-08-01", "Walkover"))):
            self.db.execute("""INSERT INTO td_match (td_key, tour, season, file, surface, tournament, match_date, winner, loser, sets_json, comment, odds_json, source)
                               VALUES (?, 'ATP', 2026, 'f', 'Hard', 'Open', ?, ?, ?, '[[6, 3], [6, 4]]', ?, '{"PSW": 1.5, "PSL": 2.6}', 't')""", (str(n), day, winner, loser, comment))
        self.db.execute("INSERT INTO player (player_id, name_ru, name_latin, tour, source) VALUES ('s', 'Бен Шелтон', 'Ben Shelton', 'ATP', 't')")
        self.db.execute("INSERT INTO player (player_id, name_ru, name_latin, tour, source) VALUES ('m', 'Алекс Де Минор', 'Alex de Minaur', 'ATP', 't')")
        self.db.execute("INSERT INTO tournament (tournament_key, tour, tournament, surface, first_seen, source) VALUES ('k', 'ATP', 'Париж', 'hard', '2026', 't')")

    def test_rows_have_the_same_shape_and_prices_from_each_side(self):
        rows = wp.load_atp_history(self.db)
        shelton = rows["shelton|b"]
        self.assertEqual(len(shelton), 2, "a walkover is not a match")
        self.assertEqual((shelton[0]["won"], shelton[0]["price"], shelton[0]["opp_price"], shelton[0]["margin"]), (True, 1.5, 2.6, 5))
        self.assertEqual((shelton[1]["won"], shelton[1]["price"], shelton[1]["full"]), (False, 2.6, False))

    def test_the_same_engine_builds_a_mens_match(self):
        tour = wp.load_tour(self.db, "ATP")
        event = {**EVENT, "team1": "Шелтон Б.", "team2": "Де Минор А.", "tournament": "Париж, Франция"}
        built = wp.build(event, [], tour.players, tour.ids, tour.surfaces, tour.learned)
        self.assertTrue(built["linked"])
        self.assertEqual(len(built["h2h_rows"]), 2)
        self.assertEqual(built["surface"], "hard")

    def test_names_become_the_same_key_from_both_sides(self):
        self.assertEqual(wp.td_key("De Minaur A."), wp.atp_key("Alex de Minaur"))
        self.assertEqual(wp.td_key("Auger-Aliassime F."), wp.atp_key("Felix Auger-Aliassime"))


class Facts(unittest.TestCase):
    def test_head_to_head_names_the_leader(self):
        cards = wp.fact_cards(EVENT, {1: [], 2: []}, {1: [], 2: []}, [result(False), result(False), result(True)], None)
        self.assertEqual([c["text"] for c in cards], ["Личные встречи: Бета Б. ведёт 2-1"])

    def test_level_head_to_head(self):
        cards = wp.fact_cards(EVENT, {1: [], 2: []}, {1: [], 2: []}, [result(False), result(True)], None)
        self.assertEqual(cards[0]["text"], "Личные встречи: 1-1")

    def test_form_needs_five_and_surface_needs_the_sample(self):
        form = {1: [result(True)] * 4, 2: [result(True)] * 5}
        hist = {1: [result(True)] * 9, 2: [result(True)] * 10}
        texts = [c["text"] for c in wp.fact_cards(EVENT, hist, form, [], "hard")]
        self.assertEqual(texts, ["Бета Б.: 5-0 в последних 5 матчах", "Бета Б.: 10-0 в последних 10 матчах на харде"])


class Linking(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        for pid, name, wta in (("a", "Анна Альфа", "1"), ("b", "Бея Бета", "2"), ("c", "Вера Вторая", "3"), ("d", "Варя Вторая", "4"),
                               ("e", "Ёлка Семёнова", "5")):
            self.db.execute("INSERT INTO player (player_id, name_ru, wta_id, tour, source) VALUES (?, ?, ?, 'WTA', 't')", (pid, name, wta))
        self.index = wp.player_index(self.db)

    def test_surname_and_initial(self):
        self.assertEqual(wp.find_wta_id("Альфа А.", self.index), 1)
        self.assertEqual(wp.find_wta_id("Семенова Е.", self.index), 5)

    def test_two_candidates_or_none_link_nobody(self):
        self.assertIsNone(wp.find_wta_id("Вторая В.", self.index))
        self.assertIsNone(wp.find_wta_id("Неизвестная Н.", self.index))
        self.assertIsNone(wp.find_wta_id("Альфа Б.", self.index))
        self.assertIsNone(wp.find_wta_id(None, self.index))

    def test_surface_from_the_city(self):
        self.db.execute("INSERT INTO tournament (tournament_key, tour, tournament, surface, first_seen, source) VALUES ('k', 'WTA', 'Город', 'hard', '2026', 't')")
        index = wp.surface_index(self.db)
        self.assertEqual(wp.surface_of("Город, Страна", index), "hard")
        self.assertIsNone(wp.surface_of("Другой, Страна", index))


class Learned(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        self.db.execute("INSERT INTO player (player_id, fs_id, wta_id, tour, source) VALUES ('a', 'F1', '1', 'WTA', 't')")
        self.db.execute("INSERT INTO player (player_id, fs_id, wta_id, tour, source) VALUES ('b', 'F2', '2', 'WTA', 't')")

    def link(self, wl_id, team1, team2, odd1, odd2, pre1, pre2, name1="Иная И.", name2="Другая Д."):
        self.db.execute("INSERT INTO match (match_id, p1_fs_id, p2_fs_id, source) VALUES (?, 'F1', 'F2', 't')", (f"M{wl_id}",))
        self.db.execute("INSERT INTO match_odds (match_id, wl_id, pre1, pre2, source) VALUES (?, ?, ?, ?, 't')", (f"M{wl_id}", wl_id, pre1, pre2))
        ws.store(self.db, [{"wl_id": wl_id, "circuit": "WTA", "tournament": "Т", "team1": name1, "team2": name2, "team1_id": team1,
                            "team2_id": team2, "start_at": "2026-11-05", "url": "", "is_doubles": 0}],
                 [{"wl_id": wl_id, "market": "Победитель", "value": None, "odd1": odd1, "odd2": odd2}], f"t{wl_id}")

    def test_same_order_and_crossed_order_by_the_prices(self):
        self.link("1", "w1", "w2", 1.5, 2.5, 1.5, 2.5)
        self.assertEqual(wp.learned_players(self.db, {}), {"w1": 1, "w2": 2})

    def test_crossed(self):
        self.link("1", "w2", "w1", 2.5, 1.5, 1.5, 2.5)
        self.assertEqual(wp.learned_players(self.db, {}), {"w2": 2, "w1": 1})

    def test_level_prices_need_a_name(self):
        self.link("1", "w1", "w2", 1.9, 1.9, 1.9, 1.9)
        self.assertEqual(wp.learned_players(self.db, {}), {})
        self.assertEqual(wp.learned_players(self.db, {("другая", "д"): {1}}), {"w2": 1, "w1": 2})

    def test_an_id_pointing_at_two_players_is_dropped(self):
        self.link("1", "w1", "w2", 1.5, 2.5, 1.5, 2.5)
        self.link("2", "w1", "w2", 2.5, 1.5, 1.5, 2.5)
        self.assertEqual(wp.learned_players(self.db, {}), {})

    def test_build_prefers_the_learned_id(self):
        event = {**EVENT, "team1": "Иначе И.", "team2": "Совсем С.", "team1_id": "w1", "team2_id": "w2"}
        built = wp.build(event, [], {}, {}, {"город": "hard"}, {"w1": 1, "w2": 2})
        self.assertTrue(built["linked"])


class Build(unittest.TestCase):
    def setUp(self):
        self.ids = {("альфа", "а"): {1}, ("бета", "б"): {2}}
        self.surfaces = {"город": "hard"}

    def test_future_results_are_not_used(self):
        players = {1: [result(True, day="2026-11-06")] * 12 + [result(False, day="2026-10-01")] * 2, 2: []}
        built = wp.build(EVENT, [], players, self.ids, self.surfaces)
        self.assertEqual(built["facts"], [])

    def test_unknown_player_still_gets_the_price_plate(self):
        built = wp.build({**EVENT, "team2": "Неизвестная Н."}, lines_of(("Победитель", None, 1.9, 1.8)), {}, self.ids, self.surfaces)
        self.assertFalse(built["linked"])
        self.assertEqual(built["facts"], [])
        self.assertEqual([c["family"] for c in built["lines"]], ["market"])
        self.assertTrue(built["notes"])

    def test_unknown_surface_still_gives_form_and_head_to_head(self):
        players = {1: [result(True, opp=2)] * 6, 2: [result(False, opp=1)] * 6}
        built = wp.build({**EVENT, "tournament": "Другой, Страна"}, lines_of(("Победитель", None, 1.9, 1.8)), players, self.ids, self.surfaces)
        self.assertEqual({c["kind"] for c in built["facts"]}, {"form", "h2h"})
        self.assertTrue(built["lines"])
        self.assertTrue(any("покрытие" in n for n in built["notes"]))

    def test_end_to_end_from_the_feed(self):
        db = fresh_db()
        events, lines = ws.parse_feed(FEED)
        ws.store(db, events, lines, "t1")
        found = wp.upcoming(db, None, None)
        self.assertEqual([e["wl_id"] for e in found], ["100"])  # doubles left out
        self.assertEqual(len(found[0]["lines"]), 3)
        self.assertEqual(wp.upcoming(db, "2026-11-06", None), [])
        self.assertEqual(len(wp.upcoming(db, None, "альфа")), 1)


if __name__ == "__main__":
    unittest.main()
