"""Parsing the WTA responses: one match however it was reported, repeat runs, the query views."""
import gzip
import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "tennis"))

import import_tennis_data as itd  # noqa: E402
import wta_collect as wc  # noqa: E402
import wta_parse as wp  # noqa: E402
import wta_query as wq  # noqa: E402
import wta_report as wr  # noqa: E402

ANNA, BEA, CARA, DINA = 101, 102, 103, 104
NAMES = {ANNA: ("Anna", "Alpha"), BEA: ("Bea", "Beta"), CARA: ("Cara", "Gamma"), DINA: ("Dina", "Delta")}


def person(player_id):
    first, last = NAMES[player_id]
    return {"id": player_id, "firstName": first, "lastName": last, "fullName": f"{first} {last}",
            "countryCode": "XYZ", "dateOfBirth": "2000-01-02", "metadata": None}


def event(group=500, year=2024, level="WTA 500", surface="Hard", title="Test Open"):
    return {"tournamentGroup": {"id": group, "name": "TEST", "level": level, "metadata": {}}, "year": year, "title": title,
            "startDate": f"{year}-03-01", "endDate": f"{year}-03-08", "surface": surface, "inOutdoor": "O", "city": "TOWN",
            "country": "XYZ", "singlesDrawSize": 32, "doublesDrawSize": 16, "prizeMoney": 1000000,
            "prizeMoneyCurrency": "USD", "winners": [], "level": level}


def player_match(me, opp, won=True, score="6-3  6-4", round_name="F", tourn_round="5", reason="W", group=500, year=2024,
                 draw="M", flag="S", partner="", opp_partner="", level_code="P", tournament=None):
    return {
        "tourn_nbr": str(group), "tourn_year": str(year), "player_1": str(me), "player_2": str(opp),
        "player_3": str(partner), "player_4": str(opp_partner), "qpm_flag": draw, "s_d_flag": flag,
        "tourn_round": tourn_round, "round_name": round_name, "reason_code": reason, "scores": score,
        "winner": 1 if won else 2, "rank_1": 5, "rank_2": 9, "seed_1": 1, "seed_2": None, "StartDate": f"{year}-03-01T00:00:00+00:00",
        "PrizeWon": 100.0, "points_1": 10, "entry_type_1": " ", "entry_type_2": " ", "TournamentLevel": level_code,
        "TournamentType": "VS", "TournamentName": "TEST", "Surface": "HARD",
        "tournament": event(group, year) if tournament is None else tournament,
        "opponent": person(opp) if opp in NAMES else None,
        "partner": person(partner) if partner in NAMES else None,
        "opponent_partner": person(opp_partner) if opp_partner in NAMES else None,
    }


def listed(match_id, a, b, winner="2", sets=((6, 3), (6, 4)), round_id="F", a2="", b2="", flag="S", draw="M", tiebreaks=None):
    row = {"MatchID": match_id, "MatchState": "F", "Winner": winner, "DrawMatchType": flag, "DrawLevelType": draw,
           "RoundID": round_id, "PlayerIDA": str(a), "PlayerIDA2": str(a2), "PlayerIDB": str(b), "PlayerIDB2": str(b2),
           "MatchTimeStamp": "2024-03-07T12:00:00+00:00", "ScoreString": "", "Venue": {"name": "Court 1"}}
    for n, (x, y) in enumerate(sets, start=1):
        row[f"ScoreSet{n}A"], row[f"ScoreSet{n}B"] = str(x), str(y)
    for n, points in (tiebreaks or {}).items():
        row[f"ScoreTbSet{n}"] = str(points)
    return row


class Fixture(unittest.TestCase):
    def setUp(self):
        self.raw = sqlite3.connect(":memory:")
        self.raw.executescript(wc.SCHEMA)
        self.db = sqlite3.connect(":memory:")
        self.db.executescript((ROOT / "tools" / "tennis" / "schema.sql").read_text(encoding="utf-8"))
        self.when = 0

    def add(self, url, kind, body):
        self.when += 1
        self.raw.execute("INSERT OR REPLACE INTO queue (url, kind, priority, status) VALUES (?, ?, 1, 'done')", (url, kind))
        wc.record(self.raw, url, 200, json.dumps(body).encode())
        self.raw.execute("UPDATE response SET fetched_at = ? WHERE url = ?", (f"t{self.when}", url))

    def parse(self, **kwargs):
        return wp.parse_all(self.raw, self.db, **kwargs)

    def rows(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def page(self, player, matches, page=0):
        self.add(f"/players/{player}/matches/?page={page}&pageSize=100&sort=desc", "player_matches",
                 {"player": person(player), "matches": matches})

    def tournament_list(self, matches, group=500, year=2024):
        self.add(f"/tournaments/{group}/{year}/matches?pageSize=500", "tournament_matches",
                 {"matches": matches, "tournament": event(group, year)})


class OneMatch(Fixture):
    def test_a_match_from_both_players_and_the_list_is_one_row(self):
        self.page(ANNA, [player_match(ANNA, BEA)])
        self.page(BEA, [player_match(BEA, ANNA, won=False)])
        self.tournament_list([listed("LS001", BEA, ANNA, winner="3", sets=((3, 6), (4, 6)))])
        self.parse()
        rows = self.rows("SELECT winner_id, loser_id, score, reports, seen_in, conflict, match_id, round FROM wta_match")
        self.assertEqual(rows, [(ANNA, BEA, "6-3  6-4", 3, "both", 0, "LS001", "F")])

    def test_pair_can_meet_twice_only_on_evidence(self):
        self.page(ANNA, [player_match(ANNA, BEA, score="6-1  6-1", round_name="R1", tourn_round="1"),
                         player_match(ANNA, BEA, won=False, score="6-4  6-4", round_name="F", tourn_round="5")])
        self.page(BEA, [player_match(BEA, ANNA, won=False, score="6-1  6-1", round_name="R1", tourn_round="1"),
                        player_match(BEA, ANNA, score="6-4  6-4", round_name="F", tourn_round="5")])
        self.parse()
        rows = self.rows("SELECT round, winner_id, reports FROM wta_match ORDER BY round")
        self.assertEqual(rows, [("F", BEA, 2), ("R1", ANNA, 2)])

    def test_differing_scores_of_a_single_meeting_stay_one_match_and_are_flagged(self):
        self.page(ANNA, [player_match(ANNA, BEA, score="6-3  6-4")])
        self.page(BEA, [player_match(BEA, ANNA, won=False, score="6-3  6-2")])
        self.parse()
        self.assertEqual(self.rows("SELECT COUNT(*), SUM(conflict) FROM wta_match"), [(1, 1)])

    def test_bye_is_not_a_match_but_the_report_is_kept(self):
        self.page(ANNA, [player_match(ANNA, "BYE", reason="B", score="", round_name="R32", tourn_round="1")])
        self.parse()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM wta_match"), [(0,)])
        self.assertEqual(self.rows("SELECT opponent_key, opponent_id FROM wta_player_match"), [("BYE", None)])

    def test_qualifying_and_main_draw_are_different_matches(self):
        self.page(ANNA, [player_match(ANNA, BEA, draw="Q", round_name="Q", tourn_round="3"),
                         player_match(ANNA, BEA, draw="M", round_name="R32", tourn_round="1")])
        self.parse()
        self.assertEqual(sorted(self.rows("SELECT draw, round FROM wta_match")), [("M", "R32"), ("Q", "Q")])

    def test_doubles_teams_merge_in_any_order(self):
        self.page(ANNA, [player_match(ANNA, CARA, flag="D", partner=BEA, opp_partner=DINA)])
        self.page(DINA, [player_match(DINA, BEA, won=False, flag="D", partner=CARA, opp_partner=ANNA)])
        self.parse()
        rows = self.rows("SELECT winner_id, winner2_id, loser_id, loser2_id, reports FROM wta_match")
        self.assertEqual(rows, [(ANNA, BEA, CARA, DINA, 2)])

    def test_winner_codes_of_the_list(self):
        self.tournament_list([
            listed("LS1", ANNA, BEA, winner="4", sets=((6, 4), (4, 3)), round_id="1"),
            listed("LS2", CARA, DINA, winner="7", sets=(), round_id="2"),
        ])
        self.parse()
        rows = dict((r[0], r[1:]) for r in self.rows("SELECT match_id, winner_id, status FROM wta_match"))
        self.assertEqual(rows, {"LS1": (ANNA, "retired"), "LS2": (DINA, "walkover")})

    def test_match_tie_break_in_the_list_is_the_same_score_as_on_the_players_page(self):
        self.page(ANNA, [player_match(ANNA, BEA, flag="D", score="6-4  3-6  13-11")])
        self.tournament_list([listed("LD1", ANNA, BEA, flag="D", winner="2", sets=((6, 4), (3, 6), (1, 0)), tiebreaks={3: 11})])
        self.parse()
        self.assertEqual(self.rows("SELECT reports, conflict FROM wta_match"), [(2, 0)])

    def test_tie_breaks_do_not_make_a_conflict(self):
        self.page(ANNA, [player_match(ANNA, BEA, score="7-6(5)  6-4")])
        self.tournament_list([listed("LS1", ANNA, BEA, sets=((7, 6), (6, 4)), tiebreaks={1: 5})])
        self.parse()
        self.assertEqual(self.rows("SELECT conflict, score FROM wta_match"), [(0, "7-6(5)  6-4")])

    def test_round_names_mean_the_same_from_both_addresses(self):
        self.assertEqual(wp.round_label("Q", "M"), "QF")
        self.assertEqual(wp.round_label("S", "M"), "SF")
        self.assertEqual(wp.round_label("Q", "Q"), "Q")
        self.assertEqual(wp.round_from_list("Q", 32), "QF")
        self.assertEqual(wp.round_from_list("2", 28), "R16")
        self.assertEqual(wp.round_from_list("1", 128), "R128")


class RepeatRuns(Fixture):
    def test_second_run_reads_nothing_and_changes_nothing(self):
        self.page(ANNA, [player_match(ANNA, BEA)])
        self.parse()
        before = self.rows("SELECT * FROM wta_match")
        self.assertEqual(self.parse(), {"events_rebuilt": 0})
        self.assertEqual(self.rows("SELECT * FROM wta_match"), before)

    def test_new_responses_are_picked_up_and_join_known_matches(self):
        self.page(ANNA, [player_match(ANNA, BEA)])
        self.parse()
        self.page(BEA, [player_match(BEA, ANNA, won=False)])
        self.tournament_list([listed("LS001", ANNA, BEA)])
        counts = self.parse()
        self.assertEqual(counts["player_matches"], 1)
        self.assertEqual(counts["tournament_matches"], 1)
        self.assertEqual(self.rows("SELECT COUNT(*), MAX(reports) FROM wta_match"), [(1, 3)])

    def test_a_response_fetched_again_replaces_what_it_said(self):
        self.page(ANNA, [player_match(ANNA, BEA, score="6-3  6-4"), player_match(ANNA, CARA, round_name="SF", tourn_round="4")])
        self.parse()
        self.page(ANNA, [player_match(ANNA, BEA, score="6-3  6-1")])
        self.parse()
        self.assertEqual(self.rows("SELECT loser_id, score FROM wta_match"), [(BEA, "6-3  6-1")])

    def test_an_event_a_refetched_page_no_longer_lists_loses_its_match(self):
        self.page(ANNA, [player_match(ANNA, BEA), player_match(ANNA, CARA, group=600, round_name="R32", tourn_round="1")])
        self.parse()
        self.page(ANNA, [player_match(ANNA, BEA)])
        self.parse()
        self.assertEqual(self.rows("SELECT group_id FROM wta_match"), [(500,)])

    def test_full_rebuild_gives_the_same_rows(self):
        self.page(ANNA, [player_match(ANNA, BEA)])
        self.add("/tournaments/?page=0&pageSize=100&from=2024-01-01&to=2024-12-31", "tournaments_page", {"content": [event()]})
        self.parse()
        before = self.rows("SELECT * FROM wta_match"), self.rows("SELECT group_id, year, level FROM wta_tournament")
        self.parse(full=True)
        self.assertEqual((self.rows("SELECT * FROM wta_match"), self.rows("SELECT group_id, year, level FROM wta_tournament")), before)

    def test_small_batches_read_everything(self):
        for player in (ANNA, BEA, CARA):
            self.page(player, [player_match(player, DINA)])
        self.parse(batch=1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM wta_match"), [(3,)])


class Directories(Fixture):
    def test_blank_players_are_skipped_and_placeholder_birth_dates_dropped(self):
        unknown = {**person(ANNA), "dateOfBirth": "1753-01-01"}
        self.add("/players/?page=0&pageSize=100", "players_page",
                 {"content": [unknown, {"id": 9, "firstName": "", "lastName": "", "fullName": " ", "dateOfBirth": "1753-01-01"}]})
        self.parse()
        self.assertEqual(self.rows("SELECT player_id, birth_date, source FROM wta_player"), [(ANNA, None, "wta-api")])

    def test_ranking_is_kept_when_the_player_is_seen_again(self):
        self.add("/players/ranked?page=0", "ranked_page",
                 [{"player": person(ANNA), "ranking": 3, "points": 4000, "tournamentsPlayed": 18, "rankedAt": "2026-11-02T00:00:00Z"}])
        self.add("/players/?page=0&pageSize=100", "players_page", {"content": [person(ANNA)]})
        self.parse()
        self.assertEqual(self.rows("SELECT singles_rank, singles_points FROM wta_player"), [(3, 4000)])

    def test_tournament_keeps_surface_level_and_prize(self):
        self.add("/tournaments/?page=0&pageSize=100&from=2024-01-01&to=2024-12-31", "tournaments_page", {"content": [event()]})
        self.parse()
        self.assertEqual(self.rows("SELECT level, surface, prize_money, prize_currency, source FROM wta_tournament"),
                         [("WTA 500", "Hard", 1000000, "USD", "wta-api")])

    def test_old_tour_event_is_not_called_itf(self):
        self.assertEqual(wp.tournament_level("ITF", "WTA 250", None), "WTA 250")
        self.assertEqual(wp.tournament_level("ITF", "WTA 250", "II"), "Tier II")
        self.assertEqual(wp.tournament_level("ITF", "ITF", None), "ITF")
        self.assertEqual(wp.tournament_level("WTA 500", "WTA 500", "P"), "WTA 500")
        self.assertEqual(wp.tournament_level(None, None, "R1", "FC"), "Team Cup")

    def test_a_players_copy_of_a_tournament_does_not_overrule_the_directory(self):
        self.add("/tournaments/?page=0&pageSize=100&from=2024-01-01&to=2024-12-31", "tournaments_page",
                 {"content": [event(title="Real Title")]})
        self.parse()
        self.page(ANNA, [player_match(ANNA, BEA, tournament=event(title="Copy"))])
        self.parse()
        self.assertEqual(self.rows("SELECT title, level_code FROM wta_tournament"), [("Real Title", "P")])

    def test_padded_ids(self):
        self.assertEqual(wp.as_int(" 40344"), 40344)
        self.assertEqual(wp.as_int("0808"), 808)
        self.assertIsNone(wp.as_int("BYE"))


class Stats(Fixture):
    BODY = [{"setnum": 0, "acesa": 4, "acesb": 1, "dblflta": 5, "dblfltb": 13, "ptswon1stserva": 32, "ptswon1stservb": 39,
             "ptsplayed1stserva": 55, "ptsplayed1stservb": 51, "ptstotwonserva": 46, "ptstotwonservb": 47,
             "totservplayeda": 86, "totservplayedb": 74, "breakptsconva": 3, "breakptsconvb": 4, "breakptsplayeda": 6,
             "breakptsplayedb": 15, "servgamesplayeda": 7, "servgamesplayedb": 8, "totptswona": 73, "totptswonb": 87}]

    def test_counts_and_what_follows_from_the_other_side(self):
        self.add("/tournaments/500/2024/matches/LS001/stats", "match_stats", self.BODY)
        self.parse()
        row = self.db.execute("""SELECT second_won, second_total, rt_pts_won, rt_pts_total, bp_saved_won, bp_saved_total
                                 FROM wta_match_stats WHERE side='A'""").fetchone()
        self.assertEqual(row, (14, 31, 27, 74, 11, 15))
        # Points won on serve and on return make up the total the source reports.
        self.assertEqual(self.rows("SELECT sv_pts_won + rt_pts_won, tot_pts_won FROM wta_match_stats WHERE side='A'"), [(73, 73)])

    def test_no_statistics_is_recorded_as_asked_and_empty(self):
        self.add("/tournaments/500/2016/matches/RS027/stats", "match_stats", [])
        self.parse()
        self.assertEqual(self.rows("SELECT year, row_count FROM wta_parsed WHERE kind='match_stats'"), [(2016, 0)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM wta_match_stats"), [(0,)])

    def test_stats_reach_the_right_player_through_the_list(self):
        self.tournament_list([listed("LS001", BEA, ANNA, winner="2")])
        self.add("/tournaments/500/2024/matches/LS001/stats", "match_stats", self.BODY)
        self.parse()
        rows = dict(self.rows("SELECT player_id, aces FROM wta_player_result"))
        self.assertEqual(rows, {BEA: 4, ANNA: 1})

    def test_season_and_records(self):
        self.add(f"/players/{ANNA}/year/2024", "player_year", {"player": person(ANNA), "stats": {
            "MatchCount": 60, "Aces": 340, "Double_Faults": 131, "First_Serves_Won": 1534, "First_Serves_Played": 2104,
            "Year_End_Rank": 6, "StatisticLevel": "TOUR", "IsFinal": False}})
        self.add(f"/players/{BEA}/year/2010", "player_year", {"player": person(BEA)})
        self.add(f"/players/{ANNA}/records", "player_records", {
            "player": person(ANNA), "lastSyncedTimestamp": "2026-11-04T14:11:14Z",
            "bySurface": [{"surface": "CLAY", "statistics": {"matchesPlayed": 10, "wins": 7, "losses": 3, "titles": 1, "finals": 2},
                           "bestResult": {"result": "Winner", "resultCode": "W", "firstAchieved": "2020", "timesAchieved": 1, "yearsAchieved": ["2020"]}}],
            "byTournamentLevel": [{"level": "WTA 125", "levelName": "WTA 125", "statistics": {"matchesPlayed": 6, "wins": 3, "losses": 3}}],
            "byTournament": [{"tournament": event(), "statistics": {"matchesPlayed": 4, "wins": 2, "losses": 2},
                              "yearlyResults": [{"year": 2023, "tournamentLevel": "P", "result": "R1", "resultCode": "R1",
                                                 "matchesWon": 1, "matchesLost": 1, "eliminationMatchId": "LS008"}]}],
        })
        self.parse()
        self.assertEqual(self.rows("SELECT player_id, year, matches, aces, year_end_rank FROM wta_season_stats"), [(ANNA, 2024, 60, 340, 6)])
        self.assertEqual(self.rows("SELECT year, row_count FROM wta_parsed WHERE url LIKE '%/year/2010'"), [(2010, 0)])
        self.assertEqual(sorted(r[0] for r in self.rows("SELECT scope FROM wta_record")), ["level", "surface", "tournament"])
        self.assertEqual(self.rows("SELECT wins, best_result FROM wta_record WHERE scope='surface'"), [(7, "Winner")])
        self.assertEqual(self.rows("SELECT year, won, elimination_match_id FROM wta_record_year"), [(2023, 1, "LS008")])


class Views(Fixture):
    def setUp(self):
        super().setUp()
        clay = event(600, 2023, level="WTA 1000", surface="Clay", title="Clay Open")
        itf = event(700, 2024, level="ITF", title="ITF Town")
        self.page(ANNA, [
            player_match(ANNA, BEA),  # final won, hard, 2024
            player_match(ANNA, CARA, round_name="SF", tourn_round="4"),
            player_match(ANNA, DINA, won=False, group=600, year=2023, tournament=clay, round_name="R32", tourn_round="1"),
            player_match(ANNA, BEA, reason="D", score="", group=600, year=2023, tournament=clay, round_name="R16", tourn_round="2"),
            player_match(ANNA, DINA, group=700, year=2024, tournament=itf, level_code="ITF"),
            player_match(ANNA, DINA, draw="Q", round_name="Q", tourn_round="3"),
            player_match(ANNA, CARA, flag="D", partner=BEA, opp_partner=DINA, round_name="R16", tourn_round="1"),
        ])
        self.parse()

    def test_career_counts_the_main_draw_and_separates_itf(self):
        rows = self.rows("SELECT circuit, matches, wins, losses, walkovers, titles, first_year, last_year FROM wta_career "
                         f"WHERE player_id={ANNA} AND discipline='S' ORDER BY circuit")
        self.assertEqual(rows, [("itf", 1, 1, 0, 0, 1, 2024, 2024), ("tour", 3, 2, 1, 1, 1, 2023, 2024)])

    def test_season(self):
        rows = self.rows(f"SELECT year, matches, wins FROM wta_season WHERE player_id={ANNA} AND discipline='S' AND circuit='tour' ORDER BY year")
        self.assertEqual(rows, [(2023, 1, 0), (2024, 2, 2)])

    def test_surface(self):
        rows = dict(self.rows(f"SELECT surface, matches FROM wta_surface WHERE player_id={ANNA} AND discipline='S' AND circuit='tour'"))
        self.assertEqual(rows, {"Hard": 2, "Clay": 1})

    def test_tournament(self):
        rows = self.rows(f"SELECT group_id, year, wins, titles FROM wta_tournament_results WHERE player_id={BEA} AND discipline='S'")
        self.assertEqual(sorted(rows), [(500, 2024, 0, 0), (600, 2023, 0, 0)])

    def test_doubles_are_counted_for_all_four(self):
        for player, won in ((ANNA, 1), (BEA, 1), (CARA, 0), (DINA, 0)):
            self.assertEqual(self.rows(f"SELECT wins FROM wta_career WHERE player_id={player} AND discipline='D'"), [(won,)])

    def test_query_cli_answers_each_question(self):
        self.assertEqual(wq.find_player(self.db, "Alpha"), (ANNA, "Anna Alpha"))
        columns, rows = wq.run(self.db, "season", ANNA, "S", year=2024)
        by_circuit = {r[columns.index("circuit")]: r[columns.index("wins")] for r in rows}
        self.assertEqual(by_circuit, {"itf": 1, "tour": 2})
        for what in ("career", "surface", "tournament", "matches"):
            self.assertTrue(wq.run(self.db, what, ANNA, "S")[1])

    def test_link_to_bolshe_players(self):
        self.db.execute("INSERT INTO player (player_id, wta_id, fs_id, name_ru, source) VALUES ('P1', ?, 'F1', 'Анна', 'bolshe-d1')", (str(ANNA),))
        self.assertEqual(self.rows("SELECT wta_id, fs_id FROM wta_player_link"), [(ANNA, "F1")])


class TennisData(Fixture):
    def td(self, winner, loser, sets=((6, 3), (6, 4)), played="2024-03-02", surface="Hard", comment="Completed", tournament="Test Open"):
        raw = {"WTA": 1, "Location": "Town", "Tournament": tournament, "Date": played, "Tier": "WTA500", "Court": "Outdoor",
               "Surface": surface, "Round": "1st Round", "Best of": 3, "Winner": winner, "Loser": loser, "WRank": 5, "LRank": "N/A",
               "WPts": 100.0, "LPts": None, "Comment": comment, "B365W": 1.5, "B365L": 2.5, "Wsets": 2, "Lsets": 0}
        for n, (w, l) in enumerate(sets, start=1):
            raw[f"W{n}"], raw[f"L{n}"] = float(w), float(l)
        return itd.parse_row(raw, 2024, "2024w/2024.xlsx")

    def wta(self):
        self.page(ANNA, [player_match(ANNA, BEA, round_name="R32", tourn_round="1"),
                         player_match(ANNA, CARA, won=False, score="6-1  6-1", round_name="R16", tourn_round="2")])
        self.parse()
        return wr.wta_rows(self.db, 2024)

    def test_row_is_read_and_excel_dates_become_iso(self):
        row = self.td("Alpha A.", "Beta B.")
        self.assertEqual((row["tour"], row["sets_json"], row["loser_rank"], row["source"]), ("WTA", "[[6, 3], [6, 4]]", None, "tennis-data"))
        self.assertEqual(itd.iso_date(39083.0), "2007-01-01")
        self.assertIsNone(itd.parse_row({"WTA": 1, "Winner": "", "Loser": None}, 2024, "x"))

    def test_same_row_has_the_same_key_when_the_file_grows(self):
        self.assertEqual(self.td("Alpha A.", "Beta B.")["td_key"], self.td("Alpha A.", "Beta B.")["td_key"])

    def test_names_in_the_files_form(self):
        self.assertEqual(wr.split_td_name("Sharapova, M."), ("sharapova", "m"))
        self.assertEqual(wr.split_td_name("Van Uytvanck A."), ("vanuytvanck", "a"))
        self.assertTrue(wr.same_player("Sun T.T.", "Sun", "Tiantian"))
        self.assertTrue(wr.same_player("Muller A.", "Müller", "Anna"))
        self.assertFalse(wr.same_player("Williams S.", "Williams", "Venus"))
        self.assertTrue(wr.same_player("Fernandez L.A.", "Fernandez", "Leylah"))

    def test_reconcile_matches_and_finds_each_difference(self):
        wtas = self.wta()
        tds = [self.td("Alpha A.", "Beta B."),
               self.td("Gamma C.", "Alpha A.", sets=((6, 1), (6, 2))),
               self.td("Delta D.", "Alpha A.")]
        found = wr.reconcile(tds, wtas)
        self.assertEqual((found["matched"], found["same_score"], len(found["score_diffs"]), len(found["only_td"]), len(found["only_wta"])),
                         (2, 1, 1, 1, 0))

    def test_reconcile_notices_a_reversed_winner_and_other_dates(self):
        wtas = self.wta()
        found = wr.reconcile([self.td("Beta B.", "Alpha A.")], wtas)
        self.assertEqual(len(found["winner_diffs"]), 1)
        found = wr.reconcile([self.td("Alpha A.", "Beta B.", played="2024-09-01")], wtas)
        self.assertEqual(found["matched"], 0)

    def test_another_given_name_matches_on_the_second_pass(self):
        wtas = self.wta()
        found = wr.reconcile([self.td("Alpha Z.", "Beta B.")], wtas)
        self.assertEqual(found["matched"], 1)

    def test_retirement_before_a_game_of_the_next_set_is_the_same_score(self):
        self.page(ANNA, [player_match(ANNA, BEA, score="6-4  0-0", reason="R", round_name="R32", tourn_round="1")])
        self.parse()
        found = wr.reconcile([self.td("Alpha A.", "Beta B.", sets=((6, 4),), comment="Retired")], wr.wta_rows(self.db, 2024))
        self.assertEqual((found["same_score"], len(found["score_diffs"])), (1, 0))

    def test_surnames_compare_as_words_not_as_substrings(self):
        self.assertFalse(wr.same_player("Li N.", "Lisicki", None))
        self.assertTrue(wr.same_player("Garcia Perez G.", "Garcia", "Georgina"))
        self.assertTrue(wr.same_player("Van Uytvanck A.", "Van Uytvanck", "Alison"))

    def test_doubles_rounds_are_named_only_from_letters(self):
        self.tournament_list([listed("LD1", ANNA, BEA, flag="D", round_id="4"), listed("LD2", CARA, DINA, flag="D", round_id="S")])
        self.parse()
        self.assertEqual(dict(self.rows("SELECT match_id, round FROM wta_match")), {"LD1": None, "LD2": "SF"})

    def test_coverage_report_reads_all_parts(self):
        self.page(ANNA, [player_match(ANNA, BEA)])
        self.parse()
        text = "\n".join(wr.coverage(self.db, self.raw))
        self.assertIn("player_matches", text)
        self.assertIn("Collected: 1 of 1", text)


class StatsWithoutAList(Fixture):
    def stats(self, match_id, sets, group=500, year=2024, aces=(7, 2)):
        rows = [{"setnum": 0, "acesa": aces[0], "acesb": aces[1], "totservplayeda": 50, "totservplayedb": 50}]
        for n, (a, b, *tb) in enumerate(sets, start=1):
            rows.append({"setnum": n, "scorea": str(a), "scoreb": str(b), "scoretb": str(tb[0]) if tb else "0"})
        self.add(f"/tournaments/{group}/{year}/matches/{match_id}/stats", "match_stats", rows)

    def test_final_is_found_by_round_and_score_and_sides_follow_the_score(self):
        self.page(ANNA, [player_match(ANNA, BEA, score="6-3  6-4", round_name="F")])
        self.stats("LS001", [(3, 6), (4, 6)])  # side B won, so B is Anna
        self.parse()
        self.assertEqual(self.rows("SELECT a1, b1 FROM wta_stats_side"), [(BEA, ANNA)])
        self.assertEqual(dict(self.rows("SELECT player_id, aces FROM wta_player_result")), {ANNA: 2, BEA: 7})

    def test_statistics_collected_before_the_players_page_link_later(self):
        self.stats("LS001", [(6, 3), (6, 4)])
        self.parse()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM wta_stats_side"), [(0,)])
        self.page(ANNA, [player_match(ANNA, BEA)])
        self.parse()
        self.assertEqual(self.rows("SELECT match_id FROM wta_match"), [("LS001",)])

    def test_round_decides_between_equal_scores(self):
        self.page(ANNA, [player_match(ANNA, BEA, round_name="F"), player_match(ANNA, CARA, round_name="SF", tourn_round="4")])
        self.stats("LS002", [(6, 3), (6, 4)])
        self.parse()
        self.assertEqual(self.rows("SELECT loser_id, match_id FROM wta_match WHERE match_id IS NOT NULL"), [(CARA, "LS002")])

    def test_equal_scores_in_one_round_stay_unlinked(self):
        self.page(ANNA, [player_match(ANNA, BEA, round_name="R32", tourn_round="1")])
        self.page(CARA, [player_match(CARA, DINA, round_name="R32", tourn_round="1")])
        self.stats("LS016", [(6, 3), (6, 4)])
        self.parse()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM wta_stats_side"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM wta_match WHERE match_id IS NOT NULL"), [(0,)])

    def test_doubles_and_qualifying_are_told_apart_by_the_id(self):
        self.page(ANNA, [player_match(ANNA, CARA, flag="D", partner=BEA, opp_partner=DINA),
                         player_match(ANNA, DINA, draw="Q", round_name="R16", tourn_round="2")])
        self.stats("LD001", [(6, 3), (6, 4)])
        self.parse()
        self.assertEqual(self.rows("SELECT discipline, draw, match_id FROM wta_match WHERE match_id IS NOT NULL"), [("D", "M", "LD001")])
        self.assertEqual(self.rows("SELECT a1, a2, b1, b2 FROM wta_stats_side"), [(ANNA, BEA, CARA, DINA)])

    def test_listed_match_is_not_linked_twice(self):
        self.page(ANNA, [player_match(ANNA, BEA)])
        self.tournament_list([listed("LS001", ANNA, BEA)])
        self.stats("LS001", [(6, 3), (6, 4)])
        self.parse()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM wta_stats_side"), [(0,)])
        self.assertEqual(self.rows("SELECT match_id FROM wta_match"), [("LS001",)])

    def test_round_of_id(self):
        self.assertEqual([wp.round_of_match_id(n) for n in (1, 2, 3, 4, 7, 8, 15, 16, 31, 32)],
                         ["F", "SF", "SF", "QF", "QF", "R16", "R16", "R32", "R32", "R64"])


class RawStaysUntouched(Fixture):
    def test_batches_leave_no_open_read_on_raw(self):
        self.page(ANNA, [player_match(ANNA, BEA)])
        self.raw.commit()
        self.parse()
        self.assertFalse(self.raw.in_transaction)

    def test_gzip_body_is_what_gets_parsed(self):
        body = {"player": person(ANNA), "matches": []}
        self.raw.execute("INSERT INTO queue (url, kind, priority, status) VALUES ('/players/101/matches/?page=0', 'player_matches', 1, 'done')")
        self.raw.execute("INSERT INTO response VALUES ('/players/101/matches/?page=0', 200, ?, 'x')", (gzip.compress(json.dumps(body).encode()),))
        self.parse()
        self.assertEqual(self.rows("SELECT player_id FROM wta_player"), [(ANNA,)])


if __name__ == "__main__":
    unittest.main()
