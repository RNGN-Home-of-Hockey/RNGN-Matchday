#!/usr/bin/env python3
"""Parse the stored WTA responses (raw/wta.sqlite) into plain tables in tennis.sqlite.

Run it as often as you like while the collector works: only responses not parsed yet are read,
and only the events they touch get their wta_match rows rebuilt. --full starts over from raw.

The collector keeps writing to the raw database in the default journal mode, and its writes give
up after 5 seconds of waiting. So raw is opened read-only and read in short batches, each fully
fetched before any parsing, so that a long parse never holds a lock the collector is waiting for.

    python tools/tennis/wta_parse.py
    python tools/tennis/wta_parse.py --full
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_RAW = HERE.parents[1] / "local-data" / "tennis" / "raw" / "wta.sqlite"
DEFAULT_DB = HERE.parents[1] / "local-data" / "tennis" / "tennis.sqlite"
SOURCE = "wta-api"
BATCH = 200
NO_BIRTH_DATE = "1753-01-01"  # the source's placeholder for "unknown"

# Tiers as /players/{id}/matches/ codes them. P and PM kept their codes when renamed, so these only
# name an event whose own level is missing or a misleading ITF.
LEVEL_BY_CODE = {
    "GS": "Grand Slam", "ITF": "ITF", "PM": "Premier Mandatory", "P5": "Premier 5", "P": "Premier",
    "IS": "International", "I": "Tier I", "II": "Tier II", "III": "Tier III", "IV": "Tier IV", "V": "Tier V",
    "C": "WTA 125", "CH": "Finals",
}
ROUND_BY_NAME = {"Q": "QF", "QF": "QF", "S": "SF", "SF": "SF", "F": "F"}
STATUS_BY_REASON = {"W": "completed", "R": "retired", "D": "walkover"}
# The tournament list's Winner: which side won and how.
WINNER_CODES = {"2": ("A", "completed"), "3": ("B", "completed"), "4": ("A", "retired"), "5": ("B", "retired"),
                "6": ("A", "walkover"), "7": ("B", "walkover")}
RAW_TABLES = ("wta_parsed", "wta_dirty_event", "wta_player", "wta_tournament", "wta_player_match",
              "wta_tournament_match", "wta_match", "wta_match_stats", "wta_stats_side", "wta_season_stats",
              "wta_record", "wta_record_year")


def as_int(value):
    """Ids and numbers arrive as ints, floats, padded strings (' 40344', '0808') or words ('BYE')."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def clean(value):
    text = str(value).strip() if value is not None else ""
    return text or None


def parse_player(player: dict | None) -> dict | None:
    if not player:
        return None
    player_id = as_int(player.get("id"))
    name = clean(player.get("fullName"))
    if player_id is None or name is None:
        return None
    birth = clean(player.get("dateOfBirth"))
    return {
        "player_id": player_id, "first_name": clean(player.get("firstName")),
        "last_name": clean(player.get("lastName")), "full_name": name,
        "country": clean(player.get("countryCode")), "birth_date": None if birth == NO_BIRTH_DATE else birth,
    }


def parse_ranked_row(row: dict) -> dict | None:
    player = parse_player(row.get("player"))
    if player:
        player.update(singles_rank=row.get("ranking"), singles_points=row.get("points"),
                      tournaments_played=row.get("tournamentsPlayed"), ranked_at=clean(row.get("rankedAt")))
    return player


def parse_tournament(event: dict | None) -> dict | None:
    if not event:
        return None
    group = event.get("tournamentGroup") or {}
    group_id, year = as_int(group.get("id")), as_int(event.get("year"))
    if group_id is None or year is None:
        return None
    return {
        "group_id": group_id, "year": year, "name": clean(group.get("name")), "title": clean(event.get("title")),
        "level_raw": clean(event.get("level")), "group_level": clean(group.get("level")),
        "surface": clean(event.get("surface")), "in_out": clean(event.get("inOutdoor")),
        "city": clean(event.get("city")), "country": clean(event.get("country")),
        "start_date": clean(event.get("startDate")), "end_date": clean(event.get("endDate")),
        "singles_draw": event.get("singlesDrawSize") or None, "doubles_draw": event.get("doublesDrawSize") or None,
        "prize_money": event.get("prizeMoney") or None, "prize_currency": clean(event.get("prizeMoneyCurrency")),
    }


def tournament_level(level_raw, group_level, level_code, type_code=None):
    """The event's tier in that year. See the note on wta_tournament in schema.sql."""
    if level_raw and level_raw != "ITF":
        return level_raw
    if type_code == "FC":
        return "Team Cup"
    if level_code in LEVEL_BY_CODE:
        return LEVEL_BY_CODE[level_code]
    if level_raw == "ITF" and group_level and group_level != "ITF":
        return group_level
    return level_raw or group_level


def round_label(name, draw) -> str | None:
    """Round in one vocabulary. In a qualifying draw the source's Q is not a quarter-final."""
    name = clean(name)
    if name is None:
        return None
    return name if draw == "Q" else ROUND_BY_NAME.get(name, name)


def round_from_list(round_id, draw_size) -> str | None:
    """The tournament list letters its late rounds and numbers the early ones; a number is named
    from the size of the main draw."""
    round_id = clean(round_id)
    if round_id in ROUND_BY_NAME:
        return ROUND_BY_NAME[round_id]
    round_no = as_int(round_id)
    if not round_no or not draw_size:
        return None
    full = 2
    while full < draw_size:
        full *= 2
    players_left = full >> (round_no - 1)
    return {2: "F", 4: "SF", 8: "QF"}.get(players_left, f"R{players_left}") if players_left >= 2 else None


def games_of(score: str | None) -> tuple:
    """(6, 3), (7, 6)... from '6-3  7-6(4)'; tie-break points and retired notes are dropped."""
    return tuple((int(a), int(b)) for a, b in re.findall(r"(\d+)-(\d+)", re.sub(r"\(\d+\)", "", score or "")))


def score_from_sets(sets: list[list]) -> str | None:
    """Sets as [winner games, loser games, loser tie-break points], in the players-page format."""
    parts = []
    for won, lost, tiebreak in sets:
        parts.append(f"{won}-{lost}" + (f"({tiebreak})" if tiebreak not in (None, "") else ""))
    return "  ".join(parts) or None


def list_set(a: int, b: int, tiebreak: int | None) -> list:
    """A set from the tournament list. It writes a match tie-break as 1-0 with the loser's points
    alongside, so 1-0(11) is 13-11; the players' pages write the same set as 13-11."""
    if tiebreak is not None and {a, b} == {0, 1}:
        high = max(10, tiebreak + 2)
        return [high, tiebreak, None] if a > b else [tiebreak, high, None]
    return [a, b, tiebreak]


def parse_player_match(url: str, match: dict) -> dict | None:
    group_id, year = as_int(match.get("tourn_nbr")), as_int(match.get("tourn_year"))
    player_id = as_int(match.get("player_1"))
    if group_id is None or year is None or player_id is None:
        return None
    draw = clean(match.get("qpm_flag")) or "M"
    opponent = clean(match.get("player_2")) or "?"
    opponent_partner = clean(match.get("player_4"))
    winner = as_int(match.get("winner"))
    start = clean(match.get("StartDate"))
    return {
        "player_id": player_id, "group_id": group_id, "year": year,
        "discipline": clean(match.get("s_d_flag")) or "S", "draw": draw,
        "tourn_round": clean(match.get("tourn_round")) or "",
        "opponent_key": opponent + (f"/{opponent_partner}" if opponent_partner else ""),
        "opponent_id": as_int(opponent), "partner_id": as_int(match.get("player_3")),
        "opponent_partner_id": as_int(opponent_partner),
        "round": round_label(match.get("round_name"), draw), "reason_code": clean(match.get("reason_code")),
        "won": None if winner is None else int(winner == 1), "score": clean(match.get("scores")),
        "player_rank": match.get("rank_1"), "opponent_rank": match.get("rank_2"),
        "player_seed": match.get("seed_1"), "opponent_seed": match.get("seed_2"),
        "entry_type": clean(match.get("entry_type_1")), "opponent_entry_type": clean(match.get("entry_type_2")),
        "points": match.get("points_1"), "prize_won": match.get("PrizeWon"),
        "start_date": start[:10] if start else None, "url": url,
    }


def parse_tournament_match(url: str, match: dict, group_id: int, year: int) -> dict | None:
    match_id = clean(match.get("MatchID"))
    if match_id is None:
        return None
    sets = []
    for n in range(1, 6):
        a, b = as_int(match.get(f"ScoreSet{n}A")), as_int(match.get(f"ScoreSet{n}B"))
        if a is not None and b is not None:
            sets.append(list_set(a, b, as_int(match.get(f"ScoreTbSet{n}"))))
    side, status = WINNER_CODES.get(str(match.get("Winner")), (None, None))
    return {
        "group_id": group_id, "year": year, "match_id": match_id,
        "discipline": clean(match.get("DrawMatchType")), "draw": clean(match.get("DrawLevelType")),
        "round_id": clean(match.get("RoundID")), "state": clean(match.get("MatchState")),
        "winner_side": side, "status": status,
        "a1": as_int(match.get("PlayerIDA")), "a2": as_int(match.get("PlayerIDA2")),
        "b1": as_int(match.get("PlayerIDB")), "b2": as_int(match.get("PlayerIDB2")),
        "score_string": clean(match.get("ScoreString")), "sets_json": json.dumps(sets) if sets else None,
        "started_at": clean(match.get("MatchTimeStamp")), "court": clean((match.get("Venue") or {}).get("name")),
        "url": url,
    }


def players_of_tournament_match(match: dict) -> list[dict]:
    """The list names its players; it is the only place some of them appear before their own pages."""
    found = []
    for side in ("A", "A2", "B", "B2"):
        player_id = as_int(match.get(f"PlayerID{side}"))
        first, last = clean(match.get(f"PlayerNameFirst{side}")), clean(match.get(f"PlayerNameLast{side}"))
        if player_id is not None and (first or last):
            found.append({"player_id": player_id, "first_name": first, "last_name": last,
                          "full_name": " ".join(x for x in (first, last) if x),
                          "country": clean(match.get(f"PlayerCountry{side}")), "birth_date": None})
    return found


def parse_match_stats(url: str, body: list, group_id: int, year: int, match_id: str) -> list[dict]:
    """Two rows per set. The source's pts1stservlost is left out: it means different things by event
    (first-serve points lost on tour, all service points lost at Slams). It is derivable anyway."""
    rows = []
    for row in body or []:
        for side, mine, other in (("A", "a", "b"), ("B", "b", "a")):
            def get(name, who=mine):
                return as_int(row.get(f"{name}{who}"))
            first_won, first_total = get("ptswon1stserv"), get("ptsplayed1stserv")
            sv_won, sv_total = get("ptstotwonserv"), get("totservplayed")
            opp_won, opp_total = get("ptstotwonserv", other), get("totservplayed", other)
            opp_bp_won, opp_bp_total = get("breakptsconv", other), get("breakptsplayed", other)
            rows.append({
                "group_id": group_id, "year": year, "match_id": match_id,
                "set_no": as_int(row.get("setnum")) or 0, "side": side,
                "games": get("score"), "opp_games": get("score", other), "tiebreak": as_int(row.get("scoretb")) or None,
                "aces": get("aces"), "df": get("dblflt"),
                "first_won": first_won, "first_total": first_total,
                "second_won": _minus(sv_won, first_won), "second_total": _minus(sv_total, first_total),
                "sv_pts_won": sv_won, "sv_pts_total": sv_total,
                "rt_pts_won": _minus(opp_total, opp_won), "rt_pts_total": opp_total,
                "tot_pts_won": get("totptswon"),
                "bp_conv_won": get("breakptsconv"), "bp_conv_total": get("breakptsplayed"),
                "bp_saved_won": _minus(opp_bp_total, opp_bp_won), "bp_saved_total": opp_bp_total,
                "sv_games": get("servgamesplayed"), "url": url,
            })
    return rows


def _minus(a, b):
    return None if a is None or b is None else a - b


def parse_season(url: str, body: dict, player_id: int, year: int) -> dict | None:
    stats = body.get("stats")
    if not stats:
        return None
    return {
        "player_id": player_id, "year": year, "matches": stats.get("MatchCount"), "aces": stats.get("Aces"),
        "df": stats.get("Double_Faults"), "first_won": stats.get("First_Serves_Won"),
        "first_total": stats.get("First_Serves_Played"), "second_won": stats.get("Second_Serves_Won"),
        "second_total": stats.get("Second_Serves_Played"), "bp_faced": stats.get("Break_Points_Faced"),
        "bp_lost": stats.get("Break_Points_Lost"), "bp_chances": stats.get("Break_Point_Chances"),
        "bp_converted": stats.get("Break_Points_Converted"), "sv_games": stats.get("Service_Games_Played"),
        "rt_games": stats.get("Return_Games_Played"), "ret1_won": stats.get("First_Return_Won"),
        "ret1_total": stats.get("First_Serve_Return_Chances"), "ret2_won": stats.get("Second_Return_Won"),
        "ret2_total": stats.get("Second_Return_Chances"), "current_rank": stats.get("Current_Rank"),
        "year_end_rank": stats.get("Year_End_Rank"), "statistic_level": clean(stats.get("StatisticLevel")),
        "is_final": int(bool(stats.get("IsFinal"))), "url": url,
    }


def parse_records(url: str, body: dict, player_id: int) -> tuple[list[dict], list[dict]]:
    synced = clean(body.get("lastSyncedTimestamp"))

    def record(scope, key, name, entry):
        stats, best = entry.get("statistics") or {}, entry.get("bestResult") or {}
        return {
            "player_id": player_id, "scope": scope, "record_key": str(key), "name": name,
            "matches": stats.get("matchesPlayed"), "wins": stats.get("wins"), "losses": stats.get("losses"),
            "win_pct": stats.get("winPercentage"), "titles": stats.get("titles"), "finals": stats.get("finals"),
            "best_result": clean(best.get("result")), "best_code": clean(best.get("resultCode")),
            "best_first_year": clean(best.get("firstAchieved")), "best_times": best.get("timesAchieved"),
            "best_years_json": json.dumps(best.get("yearsAchieved")) if best.get("yearsAchieved") else None,
            "synced_at": synced, "url": url,
        }

    records, years = [], []
    for entry in body.get("byTournamentLevel") or []:
        records.append(record("level", entry.get("level"), entry.get("levelName"), entry))
    for entry in body.get("bySurface") or []:
        records.append(record("surface", entry.get("surface"), entry.get("surface"), entry))
    for entry in body.get("byTournament") or []:
        event = entry.get("tournament") or {}
        group_id = as_int((event.get("tournamentGroup") or {}).get("id"))
        if group_id is None:
            continue
        records.append(record("tournament", group_id, clean(event.get("title")), entry))
        for result in entry.get("yearlyResults") or []:
            year = as_int(result.get("year"))
            if year is not None:
                years.append({
                    "player_id": player_id, "group_id": group_id, "year": year,
                    "level": clean(result.get("tournamentLevel")), "result": clean(result.get("result")),
                    "result_code": clean(result.get("resultCode")), "won": result.get("matchesWon"),
                    "lost": result.get("matchesLost"),
                    "elimination_match_id": clean(result.get("eliminationMatchId")), "url": url,
                })
    return records, years


# --- one match from many reports ---------------------------------------------------------------

def observations_from_player_rows(rows: list[dict]) -> list[dict]:
    found = []
    for row in rows:
        if row["reason_code"] == "B" or row["opponent_id"] is None:
            continue  # a bye, or a slot with nobody named: nothing was played
        team1 = tuple(sorted(x for x in (row["player_id"], row["partner_id"]) if x is not None))
        team2 = tuple(sorted(x for x in (row["opponent_id"], row["opponent_partner_id"]) if x is not None))
        winner = None if row["won"] is None else (team1 if row["won"] else team2)
        won = bool(row["won"])
        found.append({
            "kind": "player", "reporter": row["player_id"], "round_key": row["tourn_round"],
            "discipline": row["discipline"], "draw": row["draw"], "teams": (team1, team2), "winner": winner,
            "games": games_of(row["score"]), "score": row["score"], "sets_json": None,
            "round": row["round"], "round_no": as_int(row["tourn_round"]),
            "status": STATUS_BY_REASON.get(row["reason_code"]), "match_id": None, "started_at": None,
            "match_date": row["start_date"],
            "winner_rank": row["player_rank"] if won else row["opponent_rank"],
            "loser_rank": row["opponent_rank"] if won else row["player_rank"],
            "winner_seed": row["player_seed"] if won else row["opponent_seed"],
            "loser_seed": row["opponent_seed"] if won else row["player_seed"],
        })
    return found


def observations_from_tournament_rows(rows: list[dict], singles_draw=None, doubles_draw=None) -> list[dict]:
    found = []
    for row in rows:
        if row["state"] not in ("F", "R") or row["winner_side"] is None:
            continue  # not played yet
        team_a = tuple(sorted(x for x in (row["a1"], row["a2"]) if x is not None))
        team_b = tuple(sorted(x for x in (row["b1"], row["b2"]) if x is not None))
        if not team_a or not team_b:
            continue
        a_won = row["winner_side"] == "A"
        sets = json.loads(row["sets_json"]) if row["sets_json"] else []
        winner_sets = [[a, b, tb] if a_won else [b, a, tb] for a, b, tb in sets]
        # Doubles rounds are numbered as if in a bigger draw, so only their letters can be trusted.
        draw_size = None if row["discipline"] == "D" else singles_draw
        found.append({
            "kind": "tournament", "reporter": None, "round_key": None,
            "discipline": row["discipline"] or "S", "draw": row["draw"] or "M",
            "teams": (team_a, team_b), "winner": team_a if a_won else team_b,
            "games": tuple((a, b) for a, b, _ in winner_sets), "score": score_from_sets(winner_sets),
            "sets_json": json.dumps(winner_sets) if winner_sets else None,
            "round": round_from_list(row["round_id"], draw_size) if row["draw"] == "M" else None,
            "round_no": as_int(row["round_id"]),
            "status": row["status"],
            "match_id": row["match_id"], "started_at": row["started_at"],
            "match_date": row["started_at"][:10] if row["started_at"] else None,
            "winner_rank": None, "loser_rank": None, "winner_seed": None, "loser_seed": None,
        })
    return found


def pair_key(obs: dict) -> str:
    teams = sorted("_".join(str(x) for x in team) for team in obs["teams"])
    return f"{obs['discipline']}-{obs['draw']}-{teams[0]}-{teams[1]}"


def split_meetings(reports: list[dict]) -> list[list[dict]]:
    """Reports of one pair at one event, split into matches. They meet once in a knockout draw, so
    a differing score stays one match and is flagged. Twice (a round robin, a final after it) only
    when a player's own page lists two rounds, or the list two match ids."""
    rounds = defaultdict(set)
    for obs in reports:
        if obs["kind"] == "player":
            rounds[obs["reporter"]].add(obs["round_key"])
    meetings = max([len(v) for v in rounds.values()] + [len({o["match_id"] for o in reports if o["match_id"]})])
    if meetings <= 1:
        return [reports]
    clusters = defaultdict(list)
    for obs in reports:
        clusters[(obs["winner"], obs["games"])].append(obs)
    return sorted(clusters.values(), key=lambda c: min(o["round_no"] or 0 for o in c))


def first(reports, field, prefer=None):
    ordered = sorted(reports, key=lambda o: o["kind"] != prefer) if prefer else reports
    return next((o[field] for o in ordered if o[field] not in (None, "")), None)


def merged_match(group_id: int, year: int, key: str, reports: list[dict]) -> dict:
    winners = Counter(o["winner"] for o in reports if o["winner"])
    winner = winners.most_common(1)[0][0] if winners else None
    teams = reports[0]["teams"]
    loser = (teams[1] if winner == teams[0] else teams[0]) if winner else None
    scored = {o["games"] for o in reports if o["games"]}
    agreeing = [o for o in reports if o["winner"] == winner] or reports
    kinds = {o["kind"] for o in reports}
    started = first(reports, "started_at")
    return {
        "match_key": key, "group_id": group_id, "year": year,
        "discipline": reports[0]["discipline"], "draw": reports[0]["draw"],
        "round": first(reports, "round", "player"), "round_no": first(reports, "round_no", "player"),
        "status": first(reports, "status", "player"),
        "winner_id": winner[0] if winner else None, "winner2_id": winner[1] if winner and len(winner) > 1 else None,
        "loser_id": loser[0] if loser else None, "loser2_id": loser[1] if loser and len(loser) > 1 else None,
        "score": first(agreeing, "score", "player"), "sets_json": first(agreeing, "sets_json"),
        "winner_rank": first(agreeing, "winner_rank"), "loser_rank": first(agreeing, "loser_rank"),
        "winner_seed": first(agreeing, "winner_seed"), "loser_seed": first(agreeing, "loser_seed"),
        "match_date": started[:10] if started else first(reports, "match_date"),
        "started_at": started, "match_id": first(reports, "match_id"),
        "seen_in": "both" if len(kinds) == 2 else next(iter(kinds)), "reports": len(reports),
        "conflict": int(len(winners) > 1 or len(scored) > 1),
    }


def round_of_match_id(number: int) -> str:
    """Ids follow the bracket: 1 the final, 2-3 the semi-finals, 4-7 the quarter-finals, 8-15 R16..."""
    players = 1 << number.bit_length()
    return {2: "F", 4: "SF", 8: "QF"}.get(players, f"R{players}")


def link_unlisted_stats(group_id: int, year: int, matches: list[dict], sets_by_id: dict) -> list[dict]:
    """Find the match of statistics that no list names, by draw, round and score, and tell which side
    won. Sets the match's match_id and returns the wta_stats_side rows. Never guesses: a score that
    fits two matches links neither. A knockout round comes first, as a round robin breaks the rule."""
    draws = {"LS": ("S", "M"), "LD": ("D", "M"), "RS": ("S", "Q")}
    sides = []
    for match_id, sets in sets_by_id.items():
        found = re.fullmatch(r"([A-Z]{2})(\d+)", match_id)
        if not found or found.group(1) not in draws:
            continue
        discipline, draw = draws[found.group(1)]
        cleaned = [list_set(a, b, tb) for a, b, tb in sets if a is not None and b is not None]
        a_view = tuple((a, b) for a, b, _ in cleaned if (a, b) != (0, 0))
        b_view = tuple((b, a) for a, b in a_view)
        fits = []
        for match in matches:
            if match["discipline"] != discipline or match["draw"] != draw or match["match_id"]:
                continue
            games = tuple(g for g in games_of(match["score"]) if g != (0, 0))
            if games and games in (a_view, b_view):
                fits.append((match, games == a_view))
        in_round = [f for f in fits if draw == "M" and f[0]["round"] == round_of_match_id(int(found.group(2)))]
        chosen = in_round or fits
        if len(chosen) != 1 or a_view == b_view:
            continue
        match, a_won = chosen[0]
        match["match_id"] = match_id
        winners, losers = (match["winner_id"], match["winner2_id"]), (match["loser_id"], match["loser2_id"])
        (a1, a2), (b1, b2) = (winners, losers) if a_won else (losers, winners)
        sides.append({"group_id": group_id, "year": year, "match_id": match_id, "a1": a1, "a2": a2, "b1": b1, "b2": b2})
    return sides


def merge_observations(group_id: int, year: int, observations: list[dict]) -> list[dict]:
    by_pair = defaultdict(list)
    for obs in observations:
        by_pair[pair_key(obs)].append(obs)
    rows = []
    for pair, reports in by_pair.items():
        for number, meeting in enumerate(split_meetings(reports), start=1):
            suffix = f"#{number}" if number > 1 else ""
            rows.append(merged_match(group_id, year, f"{group_id}-{year}-{pair}{suffix}", meeting))
    return rows


# --- database -----------------------------------------------------------------------------------

def columns_of(db: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]


def replace_rows(db: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    columns = columns_of(db, table)
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    db.executemany(f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({marks})",
                   [[SOURCE if c == "source" else row.get(c) for c in columns] for row in rows])


def merge_rows(db: sqlite3.Connection, table: str, keys: tuple, rows: list[dict], newer_wins: bool = True) -> None:
    """Insert, or fill in a known row. A value is never lost to a row that does not have it."""
    if not rows:
        return
    columns = columns_of(db, table)
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    order = "excluded.{0}, {0}" if newer_wins else "{0}, excluded.{0}"
    updates = ", ".join(f"{c} = COALESCE({order.format(c)})" for c in columns if c not in keys)
    db.executemany(
        f"INSERT INTO {table} ({names}) VALUES ({marks}) ON CONFLICT({', '.join(keys)}) DO UPDATE SET {updates}",
        [[SOURCE if c == "source" else row.get(c) for c in columns] for row in rows],
    )


def apply_response(db: sqlite3.Connection, kind: str, url: str, body) -> tuple[int, int | None, set]:
    """Store one response. Returns (rows found, year if the address has one, events touched)."""
    dirty: set = set()
    if body is None:
        return 0, year_of(kind, url), dirty
    if kind == "players_page":
        players = [p for p in map(parse_player, body.get("content", [])) if p]
        merge_rows(db, "wta_player", ("player_id",), players)
        return len(players), None, dirty
    if kind == "ranked_page":
        players = [p for p in map(parse_ranked_row, body or []) if p]
        merge_rows(db, "wta_player", ("player_id",), players)
        return len(players), None, dirty
    if kind == "tournaments_page":
        events = [e for e in map(parse_tournament, body.get("content", [])) if e]
        merge_rows(db, "wta_tournament", ("group_id", "year"), events)
        return len(events), year_of(kind, url), dirty
    if kind == "player_matches":
        return apply_player_matches(db, url, body, dirty)
    if kind == "tournament_matches":
        return apply_tournament_matches(db, url, body, dirty)
    if kind == "match_stats":
        group_id, year, match_id = re.search(r"/tournaments/(\d+)/(\d+)/matches/([^/]+)/stats", url).groups()
        rows = parse_match_stats(url, body, int(group_id), int(year), match_id)
        replace_rows(db, "wta_match_stats", rows)
        dirty.add((int(group_id), int(year)))  # it may belong to a match no list names
        return len(rows), int(year), dirty
    player_id = int(re.search(r"/players/(\d+)/", url).group(1))
    if kind == "player_year":
        year = int(url.rsplit("/", 1)[1])
        row = parse_season(url, body, player_id, year)
        replace_rows(db, "wta_season_stats", [row] if row else [])
        merge_rows(db, "wta_player", ("player_id",), [p for p in [parse_player(body.get("player"))] if p])
        return int(row is not None), year, dirty
    if kind == "player_records":
        records, years = parse_records(url, body, player_id)
        replace_rows(db, "wta_record", records)
        replace_rows(db, "wta_record_year", years)
        merge_rows(db, "wta_player", ("player_id",), [p for p in [parse_player(body.get("player"))] if p])
        return len(records), None, dirty
    raise ValueError(f"unknown kind {kind}")


def apply_player_matches(db, url, body, dirty):
    rows, players, events = [], [], []
    me = parse_player(body.get("player"))
    if me:
        players.append(me)
    for match in body.get("matches", []):
        row = parse_player_match(url, match)
        if row is None:
            continue
        rows.append(row)
        dirty.add((row["group_id"], row["year"]))
        players += [p for p in map(parse_player, (match.get("opponent"), match.get("partner"),
                                                  match.get("opponent_partner"))) if p]
        event = parse_tournament(match.get("tournament")) or {"group_id": row["group_id"], "year": row["year"]}
        event["group_id"], event["year"] = row["group_id"], row["year"]
        event["level_code"] = clean(match.get("TournamentLevel"))
        event["type_code"] = clean(match.get("TournamentType"))
        event["name"] = event.get("name") or clean(match.get("TournamentName"))
        event["surface"] = event.get("surface") or (clean(match.get("Surface")) or "").title() or None
        event["start_date"] = event.get("start_date") or row["start_date"]
        events.append(event)
    replace_rows(db, "wta_player_match", rows)
    merge_rows(db, "wta_player", ("player_id",), players, newer_wins=False)
    # The tournaments page outranks a copy embedded here, except for the two codes only this has.
    merge_rows(db, "wta_tournament", ("group_id", "year"), events, newer_wins=False)
    return len(rows), None, dirty


def apply_tournament_matches(db, url, body, dirty):
    group_id, year = (int(x) for x in re.search(r"/tournaments/(\d+)/(\d+)/matches", url).groups())
    matches = body.get("matches", [])
    rows = [r for r in (parse_tournament_match(url, m, group_id, year) for m in matches) if r]
    replace_rows(db, "wta_tournament_match", rows)
    players = [p for m in matches for p in players_of_tournament_match(m)]
    merge_rows(db, "wta_player", ("player_id",), players, newer_wins=False)
    event = parse_tournament(body.get("tournament"))
    if event:
        event["group_id"], event["year"] = group_id, year
        merge_rows(db, "wta_tournament", ("group_id", "year"), [event], newer_wins=False)
    if rows:
        dirty.add((group_id, year))
    return len(rows), year, dirty


def year_of(kind: str, url: str) -> int | None:
    pattern = {"tournaments_page": r"from=(\d{4})", "tournament_matches": r"/tournaments/\d+/(\d+)/",
               "match_stats": r"/tournaments/\d+/(\d+)/", "player_year": r"/year/(\d+)"}.get(kind)
    found = re.search(pattern, url) if pattern else None
    return int(found.group(1)) if found else None


def rebuild_event(db: sqlite3.Connection, group_id: int, year: int) -> int:
    db.row_factory = sqlite3.Row
    players = [dict(r) for r in db.execute("SELECT * FROM wta_player_match WHERE group_id=? AND year=?", (group_id, year))]
    listed = [dict(r) for r in db.execute("SELECT * FROM wta_tournament_match WHERE group_id=? AND year=?", (group_id, year))]
    event = db.execute("SELECT singles_draw, doubles_draw FROM wta_tournament WHERE group_id=? AND year=?",
                       (group_id, year)).fetchone()
    db.row_factory = None
    singles, doubles = (event["singles_draw"], event["doubles_draw"]) if event else (None, None)
    rows = merge_observations(group_id, year, observations_from_player_rows(players)
                              + observations_from_tournament_rows(listed, singles, doubles))
    listed_ids = {row["match_id"] for row in listed}
    sets_by_id = defaultdict(list)
    for match_id, a, b, tiebreak in db.execute(
            """SELECT match_id, games, opp_games, tiebreak FROM wta_match_stats
               WHERE group_id=? AND year=? AND side='A' AND set_no > 0 ORDER BY match_id, set_no""", (group_id, year)):
        if match_id not in listed_ids:
            sets_by_id[match_id].append((a, b, tiebreak))
    sides = link_unlisted_stats(group_id, year, rows, sets_by_id)
    db.execute("DELETE FROM wta_match WHERE group_id=? AND year=?", (group_id, year))
    db.execute("DELETE FROM wta_stats_side WHERE group_id=? AND year=?", (group_id, year))
    replace_rows(db, "wta_match", rows)
    replace_rows(db, "wta_stats_side", sides)
    return len(rows)


def rebuild_dirty(db: sqlite3.Connection) -> int:
    events = db.execute("SELECT group_id, year FROM wta_dirty_event").fetchall()
    for group_id, year in events:
        rebuild_event(db, group_id, year)
    db.execute("DELETE FROM wta_dirty_event")
    db.commit()
    return len(events)


def refresh_levels(db: sqlite3.Connection) -> None:
    db.create_function("tournament_level", 4, tournament_level, deterministic=True)
    db.execute("UPDATE wta_tournament SET level = tournament_level(level_raw, group_level, level_code, type_code) "
               "WHERE level IS NOT tournament_level(level_raw, group_level, level_code, type_code)")
    db.commit()


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=60)  # winline_snapshot.py writes to the same database
    db.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    return db


def open_raw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)


def forget_url(db: sqlite3.Connection, url: str) -> None:
    """A response fetched again replaces what it said before, and its old events get rebuilt."""
    for table in ("wta_player_match", "wta_tournament_match", "wta_match_stats"):
        db.execute(f"INSERT OR IGNORE INTO wta_dirty_event SELECT DISTINCT group_id, year FROM {table} WHERE url = ?", (url,))
    for table in ("wta_player_match", "wta_tournament_match", "wta_match_stats", "wta_season_stats",
                  "wta_record", "wta_record_year"):
        db.execute(f"DELETE FROM {table} WHERE url = ?", (url,))


def parse_all(raw: sqlite3.Connection, db: sqlite3.Connection, batch: int = BATCH, full: bool = False) -> dict:
    if full:
        for table in RAW_TABLES:
            db.execute(f"DELETE FROM {table}")
        db.commit()
    done = dict(db.execute("SELECT url, fetched_at FROM wta_parsed"))
    todo = [row for row in raw.execute(
        """SELECT r.url, q.kind, r.fetched_at FROM response r JOIN queue q ON q.url = r.url
           WHERE r.http_status = 200 AND r.body_gz IS NOT NULL""").fetchall() if done.get(row[0]) != row[2]]
    counts: Counter = Counter()
    for start in range(0, len(todo), batch):
        part = todo[start:start + batch]
        marks = ", ".join("?" for _ in part)
        bodies = dict(raw.execute(f"SELECT url, body_gz FROM response WHERE url IN ({marks})",
                                  [row[0] for row in part]).fetchall())
        for url, kind, fetched_at in part:
            if bodies.get(url) is None:
                continue
            text = gzip.decompress(bodies[url])
            body = json.loads(text) if text.strip() else None
            if url in done:
                forget_url(db, url)
            rows, year, dirty = apply_response(db, kind, url, body)
            db.executemany("INSERT OR IGNORE INTO wta_dirty_event (group_id, year) VALUES (?, ?)", list(dirty))
            db.execute("INSERT OR REPLACE INTO wta_parsed (url, kind, fetched_at, year, row_count) VALUES (?, ?, ?, ?, ?)",
                       (url, kind, fetched_at, year, rows))
            counts[kind] += 1
        db.commit()
    counts["events_rebuilt"] = rebuild_dirty(db)
    refresh_levels(db)
    return dict(counts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--full", action="store_true", help="forget what was parsed and start over from raw")
    args = parser.parse_args()
    db = open_db(args.db)
    print(json.dumps(parse_all(open_raw(args.raw), db, full=args.full)))
    for table in ("wta_player", "wta_tournament", "wta_player_match", "wta_tournament_match", "wta_match",
                  "wta_match_stats", "wta_season_stats", "wta_record"):
        print(f"{table:24} {db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
