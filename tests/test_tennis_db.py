"""The tennis database: repeat imports stay clean, season numbers add up from the matches."""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "tennis"))

from import_bolshe import upsert  # noqa: E402


def stat(match_id, played_at, surface, won, aces):
    return {"match_id": match_id, "fs_id": "F1", "played_at": played_at, "surface": surface, "won": won, "aces": aces}


class TennisDb(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript((ROOT / "tools" / "tennis" / "schema.sql").read_text(encoding="utf-8"))
        upsert(self.db, "player", [{"player_id": "P1", "tour": "WTA", "name_latin": "Test Player", "fs_id": "F1"}])

    def test_season_adds_up_by_surface(self):
        upsert(self.db, "match_player_stats", [
            stat("A", "2026-03-01T10:00:00Z", "hard", 1, 5),
            stat("B", "2026-03-03T10:00:00Z", "hard", 0, 2),
            stat("C", "2026-05-01T10:00:00Z", "clay", 1, 1),
            stat("D", "2025-03-01T10:00:00Z", "hard", 1, 9),
        ])
        row = self.db.execute(
            "SELECT * FROM player_season_stats WHERE fs_id='F1' AND season='2026' AND surface='hard'"
        ).fetchone()
        self.assertEqual((row["matches"], row["wins"], row["losses"], row["aces"]), (2, 1, 1, 7))
        self.assertEqual(row["tour"], "WTA")

    def test_repeat_import_does_not_duplicate(self):
        row = stat("A", "2026-03-01T10:00:00Z", "hard", 1, 5)
        upsert(self.db, "match_player_stats", [row])
        upsert(self.db, "match_player_stats", [{**row, "aces": 6}])
        rows = self.db.execute("SELECT aces FROM match_player_stats").fetchall()
        self.assertEqual([r["aces"] for r in rows], [6])

    def test_columns_we_do_not_model_are_dropped_and_source_is_kept(self):
        upsert(self.db, "tournament", [{"tournament_key": "T1", "name": "Wuhan", "logo": "x.png"}])
        row = self.db.execute("SELECT * FROM tournament").fetchone()
        self.assertEqual((row["name"], row["source"]), ("Wuhan", "bolshe-d1"))

    def test_tournament_stats_need_a_known_match(self):
        upsert(self.db, "tournament", [{"tournament_key": "T1", "name": "Wuhan"}])
        upsert(self.db, "match", [{"match_id": "A", "tournament_key": "T1"}])
        upsert(self.db, "match_player_stats", [
            stat("A", "2026-03-01T10:00:00Z", "hard", 1, 5),
            stat("B", "2026-03-03T10:00:00Z", "hard", 0, 2),
        ])
        rows = self.db.execute("SELECT * FROM player_tournament_stats").fetchall()
        self.assertEqual([(r["tournament_name"], r["matches"]) for r in rows], [("Wuhan", 1)])


if __name__ == "__main__":
    unittest.main()
