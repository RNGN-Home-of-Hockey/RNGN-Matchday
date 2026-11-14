-- Tennis database (SQLite): players, tournaments, matches, per-match statistics.
-- Column names follow BOLSHE-Live-Center, the first source; `source` says where a row came from,
-- because each source has its own licence and a row must stay traceable to it.

CREATE TABLE IF NOT EXISTS import_run (
  run_id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  rows_json TEXT
);

CREATE TABLE IF NOT EXISTS player (
  player_id TEXT PRIMARY KEY,
  tour TEXT,
  name_ru TEXT, name_ru_short TEXT, name_latin TEXT,
  country_code TEXT, is_russian INTEGER, birth_date TEXT,
  singles_rank INTEGER, singles_points INTEGER, doubles_rank INTEGER,
  elo REAL, elo_hard REAL, elo_clay REAL, elo_grass REAL,
  fs_id TEXT, wta_id TEXT, atp_id TEXT, espn_id TEXT, ta_id TEXT, wikidata_id TEXT,
  source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_player_fs ON player (fs_id);
CREATE INDEX IF NOT EXISTS idx_player_wta ON player (wta_id);

CREATE TABLE IF NOT EXISTS tournament (
  tournament_key TEXT PRIMARY KEY,
  name TEXT, category TEXT, tour TEXT, level TEXT, discipline TEXT,
  surface TEXT, tournament TEXT, country TEXT, first_seen TEXT, prize TEXT,
  source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS match (
  match_id TEXT PRIMARY KEY,
  tournament_key TEXT,
  start_at TEXT, end_at TEXT, date_msk TEXT, status TEXT, note TEXT,
  p1_fs_id TEXT, p2_fs_id TEXT, p1_name TEXT, p2_name TEXT,
  sets_p1 INTEGER, sets_p2 INTEGER, sets_json TEXT, winner INTEGER, is_doubles INTEGER,
  source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_match_p1 ON match (p1_fs_id);
CREATE INDEX IF NOT EXISTS idx_match_p2 ON match (p2_fs_id);
CREATE INDEX IF NOT EXISTS idx_match_tournament ON match (tournament_key);

-- One row per player per match. Counts, not percentages, so that they add up over a season.
CREATE TABLE IF NOT EXISTS match_player_stats (
  match_id TEXT NOT NULL, fs_id TEXT NOT NULL, opp_fs_id TEXT,
  played_at TEXT NOT NULL, surface TEXT, won INTEGER NOT NULL,
  sets_won INTEGER, sets_lost INTEGER, deciding INTEGER,
  aces INTEGER, df INTEGER,
  first_won INTEGER, first_total INTEGER, second_won INTEGER, second_total INTEGER,
  bp_saved_won INTEGER, bp_saved_total INTEGER,
  ret1_won INTEGER, ret1_total INTEGER, ret2_won INTEGER, ret2_total INTEGER,
  bp_conv_won INTEGER, bp_conv_total INTEGER,
  winners INTEGER, ue INTEGER, net_won INTEGER, net_total INTEGER,
  sv_pts_won INTEGER, sv_pts_total INTEGER, rt_pts_won INTEGER, rt_pts_total INTEGER,
  tot_pts_won INTEGER, tot_pts_total INTEGER,
  sv_games_won INTEGER, sv_games_total INTEGER, rt_games_won INTEGER, rt_games_total INTEGER,
  tb_won INTEGER, tb_total INTEGER,
  source TEXT NOT NULL,
  PRIMARY KEY (match_id, fs_id)
);
CREATE INDEX IF NOT EXISTS idx_mps_player ON match_player_stats (fs_id, played_at);

CREATE TABLE IF NOT EXISTS match_odds (
  match_id TEXT PRIMARY KEY, wl_id TEXT, pre1 REAL, pre2 REAL, pre_at TEXT,
  live1 REAL, live2 REAL, live_at TEXT, hcap REAL, hcap1 REAL, hcap2 REAL,
  source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ranking_snapshot (
  tour TEXT NOT NULL, discipline TEXT NOT NULL, ranking_date TEXT NOT NULL, player_id TEXT NOT NULL,
  rank INTEGER, prev_rank INTEGER, points INTEGER, tournaments INTEGER, source TEXT NOT NULL,
  PRIMARY KEY (tour, discipline, ranking_date, player_id)
);

CREATE TABLE IF NOT EXISTS elo_snapshot (
  player_id TEXT NOT NULL, rated_on TEXT NOT NULL,
  elo REAL, elo_rank INTEGER, elo_hard REAL, elo_clay REAL, elo_grass REAL, source TEXT NOT NULL,
  PRIMARY KEY (player_id, rated_on)
);

-- Season, surface and tournament numbers are always computed from the matches, never stored.
DROP VIEW IF EXISTS player_tournament_stats;
DROP VIEW IF EXISTS player_season_stats;
DROP VIEW IF EXISTS player_match;
CREATE VIEW player_match AS
SELECT s.*, substr(s.played_at, 1, 4) AS season, p.player_id, p.tour, p.name_latin, p.name_ru,
       m.tournament_key, t.name AS tournament_name, t.level AS tournament_level
FROM match_player_stats s
LEFT JOIN player p ON p.fs_id = s.fs_id
LEFT JOIN match m ON m.match_id = s.match_id
LEFT JOIN tournament t ON t.tournament_key = m.tournament_key;

CREATE VIEW player_season_stats AS
SELECT fs_id, name_latin, tour, season, surface,
       COUNT(*) AS matches, SUM(won) AS wins, COUNT(*) - SUM(won) AS losses,
       SUM(aces) AS aces, SUM(df) AS df,
       SUM(first_won) AS first_won, SUM(first_total) AS first_total,
       SUM(second_won) AS second_won, SUM(second_total) AS second_total,
       SUM(bp_saved_won) AS bp_saved_won, SUM(bp_saved_total) AS bp_saved_total,
       SUM(bp_conv_won) AS bp_conv_won, SUM(bp_conv_total) AS bp_conv_total,
       SUM(sv_games_won) AS sv_games_won, SUM(sv_games_total) AS sv_games_total,
       SUM(rt_games_won) AS rt_games_won, SUM(rt_games_total) AS rt_games_total,
       SUM(tb_won) AS tb_won, SUM(tb_total) AS tb_total
FROM player_match
GROUP BY fs_id, season, surface;

CREATE VIEW player_tournament_stats AS
SELECT fs_id, name_latin, tournament_key, tournament_name, season,
       COUNT(*) AS matches, SUM(won) AS wins, COUNT(*) - SUM(won) AS losses,
       SUM(aces) AS aces, SUM(df) AS df
FROM player_match
WHERE tournament_key IS NOT NULL
GROUP BY fs_id, tournament_key, season;

-- ---------------------------------------------------------------------------------------------
-- WTA API, parsed by wta_parse.py from raw/wta.sqlite. Keyed by WTA's own ids, so it stays apart
-- from the BOLSHE tables above, which are keyed by Flashscore ids; player.wta_id links the two.
-- wta_player_match and wta_tournament_match keep rows as each address reported them.
-- wta_match is built from both: one row per match however many times it was reported.

CREATE TABLE IF NOT EXISTS wta_parsed (
  url TEXT PRIMARY KEY, kind TEXT NOT NULL, fetched_at TEXT NOT NULL,
  year INTEGER, row_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wta_parsed_kind ON wta_parsed (kind, year);

-- Events whose rows changed and whose wta_match rows still have to be rebuilt.
CREATE TABLE IF NOT EXISTS wta_dirty_event (
  group_id INTEGER NOT NULL, year INTEGER NOT NULL, PRIMARY KEY (group_id, year)
);

CREATE TABLE IF NOT EXISTS wta_player (
  player_id INTEGER PRIMARY KEY,
  first_name TEXT, last_name TEXT, full_name TEXT, country TEXT, birth_date TEXT,
  singles_rank INTEGER, singles_points INTEGER, tournaments_played INTEGER, ranked_at TEXT,
  source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wta_player_name ON wta_player (full_name);

-- level is worked out by wta_parse.tournament_level: level_raw says ITF for most events before
-- 2009, tour events included, and group_level is the event's tier today rather than in that year.
CREATE TABLE IF NOT EXISTS wta_tournament (
  group_id INTEGER NOT NULL, year INTEGER NOT NULL,
  name TEXT, title TEXT, level TEXT, level_raw TEXT, group_level TEXT, level_code TEXT, type_code TEXT,
  surface TEXT, in_out TEXT, city TEXT, country TEXT, start_date TEXT, end_date TEXT,
  singles_draw INTEGER, doubles_draw INTEGER, prize_money INTEGER, prize_currency TEXT,
  source TEXT NOT NULL,
  PRIMARY KEY (group_id, year)
);

-- /players/{id}/matches/. No match id here: scores are in the winner's order, won is for player_id.
CREATE TABLE IF NOT EXISTS wta_player_match (
  player_id INTEGER NOT NULL, group_id INTEGER NOT NULL, year INTEGER NOT NULL,
  discipline TEXT NOT NULL, draw TEXT NOT NULL, tourn_round TEXT NOT NULL,
  opponent_key TEXT NOT NULL,
  opponent_id INTEGER, partner_id INTEGER, opponent_partner_id INTEGER,
  round TEXT, reason_code TEXT, won INTEGER, score TEXT,
  player_rank INTEGER, opponent_rank INTEGER, player_seed INTEGER, opponent_seed INTEGER,
  entry_type TEXT, opponent_entry_type TEXT, points INTEGER, prize_won REAL, start_date TEXT,
  url TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (player_id, group_id, year, discipline, draw, tourn_round, opponent_key)
);
CREATE INDEX IF NOT EXISTS idx_wta_pm_event ON wta_player_match (group_id, year);
CREATE INDEX IF NOT EXISTS idx_wta_pm_url ON wta_player_match (url);

-- /tournaments/{id}/{year}/matches. Sides A and B as the source gives them.
CREATE TABLE IF NOT EXISTS wta_tournament_match (
  group_id INTEGER NOT NULL, year INTEGER NOT NULL, match_id TEXT NOT NULL,
  discipline TEXT, draw TEXT, round_id TEXT, state TEXT, winner_side TEXT, status TEXT,
  a1 INTEGER, a2 INTEGER, b1 INTEGER, b2 INTEGER,
  score_string TEXT, sets_json TEXT, started_at TEXT, court TEXT,
  url TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (group_id, year, match_id)
);

-- One row per match. status: completed, retired, walkover. Byes are not matches and stay out.
-- conflict = 1: reports of this match disagree on the winner or the score (see wta_report.py).
CREATE TABLE IF NOT EXISTS wta_match (
  match_key TEXT PRIMARY KEY,
  group_id INTEGER NOT NULL, year INTEGER NOT NULL,
  discipline TEXT NOT NULL, draw TEXT NOT NULL, round TEXT, round_no INTEGER,
  status TEXT, winner_id INTEGER, winner2_id INTEGER, loser_id INTEGER, loser2_id INTEGER,
  score TEXT, sets_json TEXT,
  winner_rank INTEGER, loser_rank INTEGER, winner_seed INTEGER, loser_seed INTEGER,
  match_date TEXT, started_at TEXT, match_id TEXT,
  seen_in TEXT NOT NULL, reports INTEGER NOT NULL, conflict INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wta_match_event ON wta_match (group_id, year);
CREATE INDEX IF NOT EXISTS idx_wta_match_winner ON wta_match (winner_id);
CREATE INDEX IF NOT EXISTS idx_wta_match_loser ON wta_match (loser_id);
CREATE INDEX IF NOT EXISTS idx_wta_match_id ON wta_match (group_id, year, match_id);

-- /tournaments/{id}/{year}/matches/{MatchID}/stats. set_no 0 is the whole match. Counts, per side;
-- return and break-point-saved numbers come from the other side's row of the same set.
CREATE TABLE IF NOT EXISTS wta_match_stats (
  group_id INTEGER NOT NULL, year INTEGER NOT NULL, match_id TEXT NOT NULL,
  set_no INTEGER NOT NULL, side TEXT NOT NULL, games INTEGER, opp_games INTEGER, tiebreak INTEGER,
  aces INTEGER, df INTEGER,
  first_won INTEGER, first_total INTEGER, second_won INTEGER, second_total INTEGER,
  sv_pts_won INTEGER, sv_pts_total INTEGER, rt_pts_won INTEGER, rt_pts_total INTEGER, tot_pts_won INTEGER,
  bp_conv_won INTEGER, bp_conv_total INTEGER, bp_saved_won INTEGER, bp_saved_total INTEGER,
  sv_games INTEGER,
  url TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (group_id, year, match_id, set_no, side)
);

-- Sides of statistics for a match no list names: /stats has no players, so wta_parse found the
-- match by its round and score. A score shared by two matches of a round stays unlinked.
CREATE TABLE IF NOT EXISTS wta_stats_side (
  group_id INTEGER NOT NULL, year INTEGER NOT NULL, match_id TEXT NOT NULL,
  a1 INTEGER, a2 INTEGER, b1 INTEGER, b2 INTEGER, source TEXT NOT NULL,
  PRIMARY KEY (group_id, year, match_id)
);

-- /players/{id}/year/{year}: tour-level season totals as the WTA computes them.
CREATE TABLE IF NOT EXISTS wta_season_stats (
  player_id INTEGER NOT NULL, year INTEGER NOT NULL,
  matches INTEGER, aces INTEGER, df INTEGER,
  first_won INTEGER, first_total INTEGER, second_won INTEGER, second_total INTEGER,
  bp_faced INTEGER, bp_lost INTEGER, bp_chances INTEGER, bp_converted INTEGER,
  sv_games INTEGER, rt_games INTEGER,
  ret1_won INTEGER, ret1_total INTEGER, ret2_won INTEGER, ret2_total INTEGER,
  current_rank INTEGER, year_end_rank INTEGER, statistic_level TEXT, is_final INTEGER,
  url TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (player_id, year)
);

-- /players/{id}/records. scope: level, surface or tournament; the WTA's own totals.
CREATE TABLE IF NOT EXISTS wta_record (
  player_id INTEGER NOT NULL, scope TEXT NOT NULL, record_key TEXT NOT NULL, name TEXT,
  matches INTEGER, wins INTEGER, losses INTEGER, win_pct REAL, titles INTEGER, finals INTEGER,
  best_result TEXT, best_code TEXT, best_first_year TEXT, best_times INTEGER, best_years_json TEXT,
  synced_at TEXT, url TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (player_id, scope, record_key)
);

CREATE TABLE IF NOT EXISTS wta_record_year (
  player_id INTEGER NOT NULL, group_id INTEGER NOT NULL, year INTEGER NOT NULL,
  level TEXT, result TEXT, result_code TEXT, won INTEGER, lost INTEGER,
  elimination_match_id TEXT, url TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (player_id, group_id, year)
);

-- tennis-data.co.uk yearly files, loaded by import_tennis_data.py. Tour-level main draws, singles.
CREATE TABLE IF NOT EXISTS td_match (
  td_key TEXT PRIMARY KEY,
  tour TEXT NOT NULL, season INTEGER NOT NULL, file TEXT NOT NULL,
  location TEXT, tournament TEXT, match_date TEXT, tier TEXT, court TEXT, surface TEXT, round TEXT,
  best_of INTEGER, winner TEXT, loser TEXT, winner_rank INTEGER, loser_rank INTEGER,
  winner_points INTEGER, loser_points INTEGER, sets_json TEXT, sets_w INTEGER, sets_l INTEGER,
  comment TEXT, odds_json TEXT,
  source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_td_season ON td_match (tour, season);

-- Winline's open tennis line, by winline_snapshot.py. A price is stored only when it changes, so
-- the rows of a market are its history and the last one before start_at is its closing price.
CREATE TABLE IF NOT EXISTS winline_fetch (
  taken_at TEXT PRIMARY KEY, events INTEGER NOT NULL, lines INTEGER NOT NULL, changed INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS winline_event (
  wl_id TEXT PRIMARY KEY, circuit TEXT, tournament TEXT, team1 TEXT, team2 TEXT, team1_id TEXT, team2_id TEXT,
  team1_en TEXT, team2_en TEXT,
  start_at TEXT, url TEXT, is_doubles INTEGER NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_winline_event_start ON winline_event (start_at);
-- value_key is value as text, '' for a market without one: a primary key cannot hold NULL safely.
CREATE TABLE IF NOT EXISTS winline_line (
  wl_id TEXT NOT NULL, market TEXT NOT NULL, value_key TEXT NOT NULL, taken_at TEXT NOT NULL,
  value REAL, odd1 REAL, odd2 REAL, source TEXT NOT NULL,
  PRIMARY KEY (wl_id, market, value_key, taken_at)
);

-- The line a past match itself had, built by wta_lines.py, so a result can be read against its own
-- price and not today's. Everything is from the winner's side. Prices and lines may come from
-- different sources: price_source says tennis-data, bolshe-winline or winline-feed, line_source the last two.
CREATE TABLE IF NOT EXISTS wta_match_line (
  match_key TEXT PRIMARY KEY,
  winner_price REAL, loser_price REAL, price_source TEXT, price_bookmaker TEXT,
  handicap REAL, handicap_winner_price REAL, handicap_loser_price REAL,
  total REAL, over_price REAL, under_price REAL, line_source TEXT
);

DROP VIEW IF EXISTS winline_current;
CREATE VIEW winline_current AS
SELECT l.wl_id, l.market, l.value, l.odd1, l.odd2, l.taken_at
FROM winline_line l
JOIN (SELECT wl_id, market, value_key, MAX(taken_at) AS latest FROM winline_line GROUP BY wl_id, market, value_key) x
  ON x.wl_id = l.wl_id AND x.market = l.market AND x.value_key = l.value_key AND x.latest = l.taken_at;

DROP VIEW IF EXISTS wta_career;
DROP VIEW IF EXISTS wta_season;
DROP VIEW IF EXISTS wta_surface;
DROP VIEW IF EXISTS wta_tournament_results;
DROP VIEW IF EXISTS wta_player_result;
DROP VIEW IF EXISTS wta_player_side;
DROP VIEW IF EXISTS wta_player_link;

CREATE VIEW wta_player_link AS
SELECT w.player_id AS wta_id, w.full_name, b.player_id AS bolshe_player_id, b.fs_id, b.name_ru
FROM wta_player w JOIN player b ON b.wta_id = CAST(w.player_id AS TEXT);

CREATE VIEW wta_player_side AS
SELECT match_key, winner_id AS player_id, winner2_id AS partner_id, loser_id AS opponent_id, loser2_id AS opponent2_id,
       1 AS won, winner_rank AS player_rank, loser_rank AS opponent_rank FROM wta_match WHERE winner_id IS NOT NULL
UNION ALL
SELECT match_key, winner2_id, winner_id, loser_id, loser2_id, 1, NULL, loser_rank FROM wta_match WHERE winner2_id IS NOT NULL
UNION ALL
SELECT match_key, loser_id, loser2_id, winner_id, winner2_id, 0, loser_rank, winner_rank FROM wta_match WHERE loser_id IS NOT NULL
UNION ALL
SELECT match_key, loser2_id, loser_id, winner_id, winner2_id, 0, NULL, winner_rank FROM wta_match WHERE loser2_id IS NOT NULL;

-- One row per player per match, with the event and, where the WTA has them, the match statistics.
CREATE VIEW wta_player_result AS
SELECT x.player_id, x.partner_id, x.opponent_id, x.opponent2_id, x.won, x.player_rank, x.opponent_rank,
       p.full_name AS player_name, o.full_name AS opponent_name,
       m.match_key, m.group_id, m.year, m.discipline, m.draw, m.round, m.status, m.score,
       m.match_date, m.match_id, m.conflict,
       t.name AS tournament_name, t.title AS tournament_title, t.level, t.surface, t.in_out,
       t.start_date, t.end_date, t.prize_money,
       s.aces, s.df, s.first_won, s.first_total, s.second_won, s.second_total,
       s.sv_pts_won, s.sv_pts_total, s.rt_pts_won, s.rt_pts_total, s.tot_pts_won,
       s.bp_conv_won, s.bp_conv_total, s.bp_saved_won, s.bp_saved_total, s.sv_games,
       CASE WHEN s.match_id IS NULL THEN 0 ELSE 1 END AS has_stats,
       m.source
FROM wta_player_side x
JOIN wta_match m ON m.match_key = x.match_key
LEFT JOIN wta_player p ON p.player_id = x.player_id
LEFT JOIN wta_player o ON o.player_id = x.opponent_id
LEFT JOIN wta_tournament t ON t.group_id = m.group_id AND t.year = m.year
LEFT JOIN wta_tournament_match l ON l.group_id = m.group_id AND l.year = m.year AND l.match_id = m.match_id
LEFT JOIN wta_stats_side d ON d.group_id = m.group_id AND d.year = m.year AND d.match_id = m.match_id
LEFT JOIN wta_match_stats s ON s.group_id = m.group_id AND s.year = m.year AND s.match_id = m.match_id AND s.set_no = 0
     AND s.side = CASE WHEN x.player_id IN (l.a1, l.a2, d.a1, d.a2) THEN 'A' ELSE 'B' END;

-- Main draws only, the way the WTA counts a record. Walkovers count neither way but a walkover
-- final still decides a title. circuit separates ITF from tour-level play. Qualifying and single
-- matches are in wta_player_result.
CREATE VIEW wta_career AS
SELECT player_id, player_name, discipline, CASE WHEN level = 'ITF' THEN 'itf' ELSE 'tour' END AS circuit,
       MIN(year) AS first_year, MAX(year) AS last_year,
       SUM(status != 'walkover') AS matches, SUM(won AND status != 'walkover') AS wins,
       SUM(NOT won AND status != 'walkover') AS losses, SUM(status = 'walkover') AS walkovers,
       SUM(round = 'F' AND won) AS titles, SUM(round = 'F') AS finals,
       SUM(has_stats) AS matches_with_stats, SUM(aces) AS aces, SUM(df) AS df,
       SUM(first_won) AS first_won, SUM(first_total) AS first_total,
       SUM(second_won) AS second_won, SUM(second_total) AS second_total,
       SUM(rt_pts_won) AS rt_pts_won, SUM(rt_pts_total) AS rt_pts_total,
       SUM(bp_conv_won) AS bp_conv_won, SUM(bp_conv_total) AS bp_conv_total,
       SUM(bp_saved_won) AS bp_saved_won, SUM(bp_saved_total) AS bp_saved_total
FROM wta_player_result WHERE draw = 'M'
GROUP BY player_id, discipline, circuit;

CREATE VIEW wta_season AS
SELECT player_id, player_name, discipline, year, CASE WHEN level = 'ITF' THEN 'itf' ELSE 'tour' END AS circuit,
       SUM(status != 'walkover') AS matches, SUM(won AND status != 'walkover') AS wins,
       SUM(NOT won AND status != 'walkover') AS losses, SUM(status = 'walkover') AS walkovers,
       SUM(round = 'F' AND won) AS titles, SUM(round = 'F') AS finals,
       SUM(has_stats) AS matches_with_stats, SUM(aces) AS aces, SUM(df) AS df,
       SUM(first_won) AS first_won, SUM(first_total) AS first_total,
       SUM(second_won) AS second_won, SUM(second_total) AS second_total,
       SUM(rt_pts_won) AS rt_pts_won, SUM(rt_pts_total) AS rt_pts_total,
       SUM(bp_conv_won) AS bp_conv_won, SUM(bp_conv_total) AS bp_conv_total,
       SUM(bp_saved_won) AS bp_saved_won, SUM(bp_saved_total) AS bp_saved_total
FROM wta_player_result WHERE draw = 'M'
GROUP BY player_id, discipline, year, circuit;

CREATE VIEW wta_surface AS
SELECT player_id, player_name, discipline, surface, CASE WHEN level = 'ITF' THEN 'itf' ELSE 'tour' END AS circuit,
       MIN(year) AS first_year, MAX(year) AS last_year,
       SUM(status != 'walkover') AS matches, SUM(won AND status != 'walkover') AS wins,
       SUM(NOT won AND status != 'walkover') AS losses, SUM(status = 'walkover') AS walkovers,
       SUM(round = 'F' AND won) AS titles, SUM(round = 'F') AS finals,
       SUM(has_stats) AS matches_with_stats, SUM(aces) AS aces, SUM(df) AS df,
       SUM(first_won) AS first_won, SUM(first_total) AS first_total,
       SUM(second_won) AS second_won, SUM(second_total) AS second_total,
       SUM(rt_pts_won) AS rt_pts_won, SUM(rt_pts_total) AS rt_pts_total,
       SUM(bp_conv_won) AS bp_conv_won, SUM(bp_conv_total) AS bp_conv_total,
       SUM(bp_saved_won) AS bp_saved_won, SUM(bp_saved_total) AS bp_saved_total
FROM wta_player_result WHERE draw = 'M'
GROUP BY player_id, discipline, surface, circuit;

-- A player at one tournament, one row per year; sum over group_id for the whole history there.
CREATE VIEW wta_tournament_results AS
SELECT player_id, player_name, discipline, group_id, year, tournament_name, tournament_title, level, surface,
       SUM(status != 'walkover') AS matches, SUM(won AND status != 'walkover') AS wins,
       SUM(NOT won AND status != 'walkover') AS losses, SUM(status = 'walkover') AS walkovers,
       SUM(round = 'F' AND won) AS titles, SUM(round = 'F') AS finals,
       SUM(has_stats) AS matches_with_stats, SUM(aces) AS aces, SUM(df) AS df,
       SUM(first_won) AS first_won, SUM(first_total) AS first_total,
       SUM(second_won) AS second_won, SUM(second_total) AS second_total,
       SUM(rt_pts_won) AS rt_pts_won, SUM(rt_pts_total) AS rt_pts_total,
       SUM(bp_conv_won) AS bp_conv_won, SUM(bp_conv_total) AS bp_conv_total,
       SUM(bp_saved_won) AS bp_saved_won, SUM(bp_saved_total) AS bp_saved_total
FROM wta_player_result WHERE draw = 'M'
GROUP BY player_id, discipline, group_id, year;
