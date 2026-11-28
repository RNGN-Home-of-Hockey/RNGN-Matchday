"""The WTA collector: what each response leads to, and that it stops and resumes cleanly."""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "tennis"))

import wta_collect as wc  # noqa: E402


class Discover(unittest.TestCase):
    def test_players_page_skips_blank_entries_and_pages_on(self):
        url = "/players/?page=0&pageSize=100"
        body = {"pageInfo": {"pageSize": 100, "numEntries": 150},
                "content": [{"id": 7, "fullName": "Test Player"}, {"id": 8, "fullName": " "}]}
        urls = [t[0] for t in wc.discover("players_page", url, body)]
        self.assertIn("/players/7/records", urls)
        self.assertFalse(any("/players/8/" in u for u in urls))
        self.assertIn("/players/?page=1&pageSize=100", urls)

    def test_last_players_page_stops(self):
        body = {"pageInfo": {"pageSize": 100, "numEntries": 150}, "content": []}
        self.assertEqual(wc.discover("players_page", "/players/?page=1&pageSize=100", body), [])

    def test_itf_tournaments_have_no_match_list(self):
        body = {"pageInfo": {"pageSize": 100, "numEntries": 2}, "content": [
            {"year": 2019, "tournamentGroup": {"id": 1, "level": "ITF"}},
            {"year": 2019, "tournamentGroup": {"id": 2, "level": "WTA 500"}},
        ]}
        tasks = wc.discover("tournaments_page", "/tournaments/?page=0&pageSize=100&from=2019-01-01", body)
        self.assertEqual([t[0] for t in tasks], ["/tournaments/2/2019/matches?pageSize=500"])

    def test_only_finished_matches_get_stats(self):
        body = {"matches": [{"MatchID": "LS001", "MatchState": "F"}, {"MatchID": "LS002", "MatchState": "U"}]}
        tasks = wc.discover("tournament_matches", "/tournaments/2/2019/matches?pageSize=500", body)
        self.assertEqual([t[0] for t in tasks], ["/tournaments/2/2019/matches/LS001/stats"])

    def test_full_player_page_goes_on_and_each_year_is_asked_once(self):
        url = "/players/7/matches/?page=0&pageSize=100&sort=desc"
        body = {"matches": [{"tourn_year": "2020"}] * 60 + [{"tourn_year": "2021"}] * 40}
        urls = [t[0] for t in wc.discover("player_matches", url, body)]
        self.assertEqual(urls.count("/players/7/year/2020"), 1)
        self.assertIn("/players/7/year/2021", urls)
        self.assertIn("/players/7/matches/?page=1&pageSize=100&sort=desc", urls)

    def test_short_player_page_is_the_last(self):
        url = "/players/7/matches/?page=2&pageSize=100&sort=desc"
        urls = [t[0] for t in wc.discover("player_matches", url, {"matches": [{"tourn_year": "2020"}]})]
        self.assertEqual(urls, ["/players/7/year/2020"])


class StatsWithoutList(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(wc.SCHEMA)

    def event(self, group, year, level="WTA 250", end="2018-06-24", singles=32, doubles=16):
        return {"tournamentGroup": {"id": group, "level": level}, "year": year, "level": level, "endDate": end,
                "singlesDrawSize": singles, "doublesDrawSize": doubles}

    def directory(self, *events):
        url = "/tournaments/?page=0&pageSize=100&from=2018-01-01&to=2018-12-31"
        wc.add_tasks(self.db, [(url, "tournaments_page", 10)])
        wc.step(self.db, url, "tournaments_page", 0, lambda u: (200, json.dumps({"content": list(events)}).encode()))
        self.db.execute("DELETE FROM queue WHERE kind = 'tournament_matches'")

    def list_result(self, group, year, code, body=None):
        url = f"/tournaments/{group}/{year}/matches?pageSize=500"
        wc.add_tasks(self.db, [(url, "tournament_matches", 20)])
        wc.step(self.db, url, "tournament_matches", 0, lambda u: (code, json.dumps(body).encode() if body is not None else None))

    def urls(self):
        return [t[0] for t in wc.missing_stats_tasks(self.db, "2026-11-04")]

    def test_ids_cover_the_full_bracket(self):
        urls = [t[0] for t in wc.stats_tasks_without_list(5, 2018, 48, 24)]
        self.assertIn("/tournaments/5/2018/matches/LS063/stats", urls)
        self.assertNotIn("/tournaments/5/2018/matches/LS064/stats", urls)
        self.assertIn("/tournaments/5/2018/matches/LD031/stats", urls)
        self.assertIn("/tournaments/5/2018/matches/RS063/stats", urls)
        self.assertNotIn("/tournaments/5/2018/matches/RS064/stats", urls)

    def test_a_missing_list_gets_addresses(self):
        self.directory(self.event(5, 2018))
        self.list_result(5, 2018, 404)
        self.assertEqual(len(self.urls()), 31 + 15 + 63)

    def test_an_empty_list_gets_addresses_and_a_full_one_does_not(self):
        self.directory(self.event(5, 2018), self.event(6, 2018))
        self.list_result(5, 2018, 200, {"matches": []})
        self.list_result(6, 2018, 200, {"matches": [{"MatchID": "LS001", "MatchState": "U"}]})
        urls = self.urls()
        self.assertTrue(urls and all(u.startswith("/tournaments/5/") for u in urls))

    def test_events_with_nothing_to_find_are_left_out(self):
        self.directory(self.event(1, 2016, end="2016-06-01"), self.event(2, 2018, level="ITF"), self.event(3, 2018, level="Grand Slam"),
                       self.event(4, 2026, end="2026-12-01"))
        for group, year in ((1, 2016), (2, 2018), (3, 2018), (4, 2026)):
            self.list_result(group, year, 404)
        self.assertEqual(self.urls(), [])

    def test_repeat_adds_nothing_new_and_keeps_done_work(self):
        self.directory(self.event(5, 2018))
        self.list_result(5, 2018, 404)
        tasks = wc.missing_stats_tasks(self.db, "2026-11-04")
        wc.add_tasks(self.db, tasks)
        wc.step(self.db, tasks[0][0], "match_stats", 0, lambda u: (200, b"[]"))
        wc.add_tasks(self.db, wc.missing_stats_tasks(self.db, "2026-11-04"))
        self.assertEqual(self.db.execute("SELECT status FROM queue WHERE url = ?", (tasks[0][0],)).fetchone()[0], "done")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM queue WHERE kind = 'match_stats'").fetchone()[0], 109)


class Run(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(wc.SCHEMA)
        wc.add_tasks(self.db, [("/players/7/records", "player_records", 60)])

    def state(self):
        return self.db.execute("SELECT status, tries FROM queue").fetchone()

    def test_success_stores_the_response_as_received(self):
        wc.step(self.db, "/players/7/records", "player_records", 0, lambda u: (200, json.dumps({"a": 1}).encode()))
        self.assertEqual(self.state(), ("done", 1))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM response WHERE http_status=200").fetchone()[0], 1)

    def test_blocked_stops_and_leaves_the_task_for_later(self):
        for code in (403, 429):
            with self.assertRaises(wc.Blocked):
                wc.step(self.db, "/players/7/records", "player_records", 0, lambda u: (code, None))
        self.assertEqual(self.state(), ("todo", 0))

    def test_missing_is_final(self):
        wc.step(self.db, "/players/7/records", "player_records", 0, lambda u: (404, None))
        self.assertEqual(self.state()[0], "missing")

    def test_server_error_retries_then_gives_up(self):
        for tries in range(wc.MAX_TRIES):
            wc.step(self.db, "/players/7/records", "player_records", tries, lambda u: (500, None))
        self.assertEqual(self.state(), ("failed", wc.MAX_TRIES))

    def test_broken_json_is_retried_not_stored(self):
        wc.step(self.db, "/players/7/records", "player_records", 0, lambda u: (200, b"<html>"))
        self.assertEqual(self.state(), ("todo", 1))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM response").fetchone()[0], 0)

    def test_task_seen_again_keeps_its_state(self):
        wc.step(self.db, "/players/7/records", "player_records", 0, lambda u: (200, b"{}"))
        wc.add_tasks(self.db, [("/players/7/records", "player_records", 10)])
        self.assertEqual(self.state()[0], "done")


if __name__ == "__main__":
    unittest.main()
