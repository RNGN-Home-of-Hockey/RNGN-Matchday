#!/usr/bin/env python3
"""Coverage of the WTA data, and how it compares with tennis-data.co.uk.

    python tools/tennis/wta_report.py --out local-data/tennis/coverage.md
    python tools/tennis/wta_report.py --compare 324166:2023,2024 230220:2007,2015 --events 2026

tennis-data has tour-level main-draw singles from 2007, so only those can be compared. It names
players as "Surname I.", so a match is found by both surnames, the first initial where the file
gives one, the winner, and the date falling within the WTA event's dates.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE.parents[1] / "local-data" / "tennis" / "tennis.sqlite"
DEFAULT_RAW = HERE.parents[1] / "local-data" / "tennis" / "raw" / "wta.sqlite"
sys.path.insert(0, str(HERE))
from wta_parse import games_of, open_raw  # noqa: E402

DATE_SLACK = timedelta(days=4)  # a file may date a match by the event's start or by the day before it


def folded(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name)
    return re.sub(r"[^a-z]", "", "".join(c for c in plain if not unicodedata.combining(c)).lower())


def split_td_name(name: str) -> tuple[str, str]:
    """('sharapova', 'm') from 'Sharapova, M.'; ('vanuytvanck', 'a') from 'Van Uytvanck A.'."""
    words = name.replace(",", " ").split()
    initials = [w for w in words if w.endswith(".")]
    surname = [w for w in words if not w.endswith(".")]
    return folded(" ".join(surname)), folded(initials[0])[:1] if initials else ""


def same_player(td_name: str, last_name: str | None, first_name: str | None) -> bool:
    """Same surname, whole or as part of a compound one, and the same initial if both are known."""
    if not last_name:
        return False
    surname, initial = split_td_name(td_name)
    if not surname:
        return False
    if surname != folded(last_name):
        file_words = {folded(w) for w in re.split(r"[\s,-]+", td_name) if not w.endswith(".") and folded(w)}
        wta_words = {folded(w) for w in re.split(r"[\s-]+", last_name) if folded(w)}
        if not (file_words <= wta_words or wta_words <= file_words):
            return False
    return not initial or not first_name or folded(first_name)[:1] == initial


def td_rows(db: sqlite3.Connection, season: int) -> list[dict]:
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM td_match WHERE tour = 'WTA' AND season = ? AND (comment IS NULL OR comment != 'Cancelled')", (season,))]
    db.row_factory = None
    return rows


def wta_rows(db: sqlite3.Connection, year: int, player_id: int | None = None) -> list[dict]:
    sql = """SELECT m.match_key, m.score, m.status, m.round, m.match_date, m.conflict, m.group_id,
                    t.title, t.level, t.surface, t.start_date, t.end_date,
                    w.last_name AS winner_last, w.first_name AS winner_first, w.player_id AS winner_id,
                    l.last_name AS loser_last, l.first_name AS loser_first, l.player_id AS loser_id
             FROM wta_match m
             LEFT JOIN wta_tournament t ON t.group_id = m.group_id AND t.year = m.year
             LEFT JOIN wta_player w ON w.player_id = m.winner_id
             LEFT JOIN wta_player l ON l.player_id = m.loser_id
             WHERE m.year = ? AND m.discipline = 'S' AND m.draw = 'M'"""
    values: list = [year]
    if player_id is not None:
        sql += " AND (m.winner_id = ? OR m.loser_id = ?)"
        values += [player_id, player_id]
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(sql, values)]
    db.row_factory = None
    return rows


def in_event(td: dict, wta: dict) -> bool:
    if not td.get("match_date") or not wta.get("start_date"):
        return True
    played = date.fromisoformat(td["match_date"])
    start = date.fromisoformat(wta["start_date"])
    end = date.fromisoformat(wta["end_date"]) if wta.get("end_date") else start + timedelta(days=21)
    return start - DATE_SLACK <= played <= end + DATE_SLACK


def without_empty_sets(games: tuple) -> tuple:
    """A retirement before a game of the next set: 6-4 0-0 in WTA, 6-4 in the file."""
    return tuple(s for s in games if tuple(s) != (0, 0))


def td_games(td: dict) -> tuple:
    return tuple(tuple(s) for s in json.loads(td["sets_json"])) if td.get("sets_json") else ()


def involves(td: dict, wta: dict, initials: bool = True) -> str | None:
    """'same' if the two rows are one match with one winner, 'swapped' if the winner differs."""
    def player(name, who):
        return same_player(td[name], wta[f"{who}_last"], wta[f"{who}_first"] if initials else None)
    if player("winner", "winner") and player("loser", "loser"):
        return "same"
    if player("winner", "loser") and player("loser", "winner"):
        return "swapped"
    return None


def reconcile(tds: list[dict], wtas: list[dict]) -> dict:
    """Pair tennis-data rows with WTA rows, one to one. Only WTA tour-level rows can be expected in
    the file, so WTA rows left over are split by level to show what else there is. Initials count
    on a first pass; a second one drops them, since the file may use another given name (Osorio M.
    is Camila Osorio). A married or changed surname stays unmatched and shows up on both sides."""
    by_event = defaultdict(list)
    for wta in wtas:
        by_event[wta.get("start_date")].append(wta)
    used: set = set()
    result = Counter()
    pairs_found, score_diffs, surface_diffs, winner_diffs = [], [], [], []
    remaining = tds
    for initials in (True, False):
        unmatched = []
        for td in remaining:
            pairs = [(involves(td, w, initials), w) for event in by_event.values() if in_event(td, event[0])
                     for w in event if w["match_key"] not in used]
            pairs = [p for p in pairs if p[0]]
            if not pairs:
                unmatched.append(td)
                continue
            # A pair can meet twice a season; the dates narrowed it and the score settles it.
            pairs.sort(key=lambda p: (p[0] != "same", games_of(p[1]["score"]) != td_games(td)))
            used.add(pairs[0][1]["match_key"])
            pairs_found.append((td, *pairs[0]))
        remaining = unmatched
    only_td = remaining
    for td, how, wta in pairs_found:
        result["matched"] += 1
        if how == "swapped":
            winner_diffs.append((td, wta))
            continue
        games_w, games_t = without_empty_sets(games_of(wta["score"])), without_empty_sets(td_games(td))
        if games_w == games_t:
            result["same_score"] += 1
        elif not games_w or not games_t:
            result["score_missing_one_side"] += 1
        else:
            score_diffs.append((td, wta))
        if td.get("surface") and wta.get("surface") and td["surface"].lower() != wta["surface"].lower():
            surface_diffs.append((td, wta))
    only_wta = [w for w in wtas if w["match_key"] not in used]
    return {
        "td_rows": len(tds), "wta_rows": len(wtas), "matched": result["matched"],
        "same_score": result["same_score"], "score_missing_one_side": result["score_missing_one_side"],
        "pairs": pairs_found, "score_diffs": score_diffs, "winner_diffs": winner_diffs, "surface_diffs": surface_diffs,
        "only_td": only_td, "only_wta": only_wta,
        "only_wta_by_level": Counter(w.get("level") or "?" for w in only_wta),
    }


def compare_player(db: sqlite3.Connection, player_id: int, year: int) -> dict:
    row = db.execute("SELECT last_name, first_name, full_name FROM wta_player WHERE player_id = ?", (player_id,)).fetchone()
    if not row:
        raise SystemExit(f"unknown player {player_id}")
    last, first, name = row
    tds = [t for t in td_rows(db, year) if same_player(t["winner"], last, first) or same_player(t["loser"], last, first)]
    return {"name": name, "year": year, **reconcile(tds, wta_rows(db, year, player_id))}


def compare_events(db: sqlite3.Connection, year: int) -> dict:
    """All the matches of events the WTA tables hold for a year, against the file's same events.
    A file row counts as missing from WTA only when its event matched for some other row."""
    wtas = [w for w in wta_rows(db, year) if (w.get("level") or "") != "ITF"]
    tds = td_rows(db, year)
    result = reconcile(tds, wtas)
    matched_events = Counter(t["tournament"] for t in tds) - Counter(t["tournament"] for t in result["only_td"])
    result["only_td"] = [t for t in result["only_td"] if matched_events[t["tournament"]] > 0]
    result["td_rows"] = sum(1 for t in tds if matched_events[t["tournament"]] > 0)
    result["events_in_both"] = len(+matched_events)
    return result


def describe(found: dict) -> str:
    extra = ", ".join(f"{k} {v}" for k, v in found["only_wta_by_level"].most_common())
    return (f"file {found['td_rows']}, WTA {found['wta_rows']}, matched {found['matched']}, same score {found['same_score']}, "
            f"score differs {len(found['score_diffs'])}, winner differs {len(found['winner_diffs'])}, "
            f"surface differs {len(found['surface_diffs'])}, only in file {len(found['only_td'])}, "
            f"only in WTA {len(found['only_wta'])}" + (f" ({extra})" if extra else ""))


def examples(found: dict, limit: int = 3) -> list[str]:
    lines = []
    for td, wta in found["score_diffs"][:limit]:
        lines.append(f"  score: {td['tournament']} {td['winner']} d {td['loser']} file {td['sets_json']} ({td['comment']}), WTA {wta['score']} ({wta['status']})")
    for td, _wta in found["winner_diffs"][:limit]:
        lines.append(f"  winner: {td['tournament']} {td['winner']} d {td['loser']} in the file, the other way round in WTA")
    for td in found["only_td"][:limit]:
        lines.append(f"  only in file: {td['match_date']} {td['tournament']} {td['round']} {td['winner']} d {td['loser']}")
    for wta in found["only_wta"][:limit]:
        lines.append(f"  only in WTA: {wta['start_date']} {wta['title']} {wta['round']} {wta['winner_last']} d {wta['loser_last']} [{wta['level']}]")
    return lines


def pct(part, whole) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "-"


def coverage(db: sqlite3.Connection, raw: sqlite3.Connection | None) -> list[str]:
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
    lines = ["# WTA data coverage", ""]
    if raw is not None:
        queue = raw.execute("SELECT kind, status, COUNT(*) FROM queue GROUP BY kind, status").fetchall()
        kinds = defaultdict(Counter)
        for kind, status, count in queue:
            kinds[kind][status] = count
        total = sum(sum(c.values()) for c in kinds.values())
        fetched = sum(c["done"] + c["missing"] + c["failed"] for c in kinds.values())
        lines += [f"Collected: {fetched} of {total} addresses known so far ({pct(fetched, total)}). The queue grows as pages are read.", "",
                  "| kind | known | done | missing | failed | todo |", "|---|---|---|---|---|---|"]
        for kind, c in sorted(kinds.items()):
            lines.append(f"| {kind} | {sum(c.values())} | {c['done']} | {c['missing']} | {c['failed']} | {c['todo']} |")
        lines.append("")
    lines += ["## Parsed", "", "| table | rows |", "|---|---|"]
    for table in ("wta_player", "wta_tournament", "wta_player_match", "wta_tournament_match", "wta_match",
                  "wta_match_stats", "wta_season_stats", "wta_record", "td_match"):
        lines.append(f"| {table} | {q(f'SELECT COUNT(*) FROM {table}')[0][0]} |")
    conflicts = q("SELECT COUNT(*) FROM wta_match WHERE conflict = 1")[0][0]
    merged = q("SELECT COUNT(*) FROM wta_match WHERE reports > 1")[0][0]
    lines += ["", f"Matches reported more than once and merged: {merged}. Reports that disagree: {conflicts}.", ""]
    stats_total = q("SELECT COUNT(DISTINCT group_id || '-' || year || '-' || match_id) FROM wta_match_stats")[0][0]
    stats_linked = q("""SELECT COUNT(DISTINCT s.group_id || '-' || s.year || '-' || s.match_id) FROM wta_match_stats s
                        JOIN wta_match m ON m.group_id = s.group_id AND m.year = s.year AND m.match_id = s.match_id""")[0][0]
    no_list = q("SELECT COUNT(*) FROM wta_stats_side")[0][0]
    lines += [f"Matches with statistics: {stats_total}; tied to a known match: {stats_linked}, of them {no_list} by round and score "
              "because no list names them. The rest wait for players' pages, or share a score in their round.", ""]
    lines += ["## By year", "", "Tournaments by level; match lists fetched and how many had matches; "
              "match statistics fetched and how many had numbers; season statistics.", "",
              "| year | tour events | ITF events | lists | with matches | stats asked | with numbers | seasons | matches |",
              "|---|---|---|---|---|---|---|---|---|"]
    events = {y: (t, i) for y, t, i in q(
        "SELECT year, SUM(level != 'ITF'), SUM(level = 'ITF') FROM wta_tournament GROUP BY year")}
    parsed = defaultdict(lambda: [0, 0])
    for kind, year, count, full in q("SELECT kind, year, COUNT(*), SUM(row_count > 0) FROM wta_parsed WHERE year IS NOT NULL GROUP BY kind, year"):
        parsed[(kind, year)] = [count, full]
    matches = dict(q("SELECT year, COUNT(*) FROM wta_match GROUP BY year"))
    for year in sorted(events, reverse=True):
        lists, stats, seasons = parsed[("tournament_matches", year)], parsed[("match_stats", year)], parsed[("player_year", year)]
        lines.append(f"| {year} | {events[year][0]} | {events[year][1]} | {lists[0]} | {lists[1]} | {stats[0]} | {stats[1]} | {seasons[1]} | {matches.get(year, 0)} |")
    itf_matches = q("SELECT COUNT(*), COUNT(DISTINCT m.group_id || '-' || m.year), SUM(m.match_id IS NOT NULL) FROM wta_match m JOIN wta_tournament t ON t.group_id = m.group_id AND t.year = m.year WHERE t.level = 'ITF'")[0]
    itf_events = q("SELECT COUNT(*), MIN(year), MAX(year) FROM wta_tournament WHERE level = 'ITF'")[0]
    itf_stats = q("SELECT COUNT(*) FROM wta_match_stats s JOIN wta_tournament t ON t.group_id = s.group_id AND t.year = s.year WHERE t.level = 'ITF'")[0][0]
    lines += ["", "## ITF", "",
              f"Events listed: {itf_events[0]} ({itf_events[1]}-{itf_events[2]}). Matches known so far: {itf_matches[0]} in {itf_matches[1]} events, "
              f"{itf_matches[2] or 0} of them from a tournament list. Match statistics rows: {itf_stats}.", ""]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--out", type=Path, help="also write the report here")
    parser.add_argument("--compare", nargs="*", default=[], metavar="ID:YEAR,YEAR", help="compare these players with tennis-data")
    parser.add_argument("--events", nargs="*", type=int, default=[], metavar="YEAR", help="compare all parsed events of these years")
    args = parser.parse_args()
    db = sqlite3.connect(f"{args.db.resolve().as_uri()}?mode=ro", uri=True)
    lines = coverage(db, open_raw(args.raw) if args.raw.exists() else None)
    if args.compare:
        lines += ["## tennis-data, by player", ""]
        for item in args.compare:
            player, years = item.split(":")
            for year in years.split(","):
                found = compare_player(db, int(player), int(year))
                lines += [f"{found['name']} {year}: {describe(found)}", *examples(found), ""]
    if args.events:
        lines += ["## tennis-data, by event", ""]
        for year in args.events:
            found = compare_events(db, year)
            lines += [f"{year}, {found['events_in_both']} events in both: {describe(found)}", *examples(found, 5), ""]
    text = "\n".join(lines)
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, "replace").decode(encoding))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
