#!/usr/bin/env python3
"""Keep Winline's tennis line with its history: every market, stored whenever a price changes.

The open feed only ever shows the current price, so a day not recorded is lost. It is the same
feed BOLSHE reads, at the same 10-minute pace. On the shared server a systemd timer runs it, see
install_on_server.sh; a 403 or 429 stops --loop.

    python tools/tennis/winline_snapshot.py            # once
    python tools/tennis/winline_snapshot.py --loop     # every 10 minutes
"""
from __future__ import annotations

import argparse
import html
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE.parents[1] / "local-data" / "tennis" / "tennis.sqlite"
FEED = "https://back.winline.ru/banners/prematch_mainsports"
FEED_EN = "https://back.winline.ru/banners/prematch_mainsports_eng"  # the same matches with full Latin names
USER_AGENT = "RNGN-Matchday-data/0.1"
SOURCE = "winline-feed"


def attributes(tag: str) -> dict:
    return {k: html.unescape(v) for k, v in re.findall(r'([\w]+)="([^"]*)"', tag)}


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_feed(text: str) -> tuple[list[dict], list[dict]]:
    """Tennis events and their lines. Only two-way lines exist for tennis."""
    block = re.search(r'<Sport[^>]*Name="Теннис".*?</Sport>', text, re.S)
    if not block:
        return [], []
    events, lines = [], []
    for country_tag, country in re.findall(r"<Country ([^>]*)>(.*?)</Country>", block.group(0), re.S):
        circuit = attributes(country_tag).get("Name")
        for tournament_tag, tournament in re.findall(r"<Tournament ([^>]*)>(.*?)</Tournament>", country, re.S):
            name = attributes(tournament_tag).get("Name") or ""
            for match_tag, body in re.findall(r"<Match ([^>]*)>(.*?)</Match>", tournament, re.S):
                m = attributes(match_tag)
                if not m.get("Id"):
                    continue
                events.append({
                    "wl_id": m["Id"], "circuit": circuit, "tournament": name, "team1": m.get("Team1"), "team2": m.get("Team2"),
                    "team1_id": m.get("Id1"), "team2_id": m.get("Id2"), "start_at": m.get("MatchDate"),
                    "url": m.get("MatchUrl"), "is_doubles": int("Пары" in name or "/" in (m.get("Team1") or "")),
                })
                for line_tag in re.findall(r"<line ([^>]*)>", body):
                    line = attributes(line_tag)
                    if line.get("freetext") and number(line.get("odd1")) and number(line.get("odd2")):
                        lines.append({"wl_id": m["Id"], "market": line["freetext"], "value": number(line.get("value")),
                                      "odd1": number(line["odd1"]), "odd2": number(line["odd2"])})
    return events, lines


def store(db: sqlite3.Connection, events: list[dict], lines: list[dict], taken_at: str) -> int:
    """Returns how many prices changed. A line that left the feed keeps its last price."""
    db.executemany(
        """INSERT INTO winline_event (wl_id, circuit, tournament, team1, team2, team1_id, team2_id, start_at, url,
                                      is_doubles, first_seen, last_seen, source)
           VALUES (:wl_id, :circuit, :tournament, :team1, :team2, :team1_id, :team2_id, :start_at, :url,
                   :is_doubles, :now, :now, :source)
           ON CONFLICT(wl_id) DO UPDATE SET start_at = excluded.start_at, team1 = excluded.team1, team2 = excluded.team2,
             team1_id = excluded.team1_id, team2_id = excluded.team2_id, last_seen = excluded.last_seen""",
        [{**e, "now": taken_at, "source": SOURCE} for e in events])
    known = {(w, m, k): (a, b) for w, m, k, a, b in db.execute(
        """SELECT l.wl_id, l.market, l.value_key, l.odd1, l.odd2 FROM winline_line l
           JOIN (SELECT wl_id, market, value_key, MAX(taken_at) AS latest FROM winline_line GROUP BY wl_id, market, value_key) x
             ON x.wl_id = l.wl_id AND x.market = l.market AND x.value_key = l.value_key AND x.latest = l.taken_at""")}
    changed = []
    for line in lines:
        key = "" if line["value"] is None else str(line["value"])
        if known.get((line["wl_id"], line["market"], key)) != (line["odd1"], line["odd2"]):
            changed.append((line["wl_id"], line["market"], key, taken_at, line["value"], line["odd1"], line["odd2"], SOURCE))
    # A feed can repeat a line within a match; the last one stands.
    db.executemany("INSERT OR REPLACE INTO winline_line VALUES (?, ?, ?, ?, ?, ?, ?, ?)", changed)
    db.execute("INSERT OR REPLACE INTO winline_fetch VALUES (?, ?, ?, ?)", (taken_at, len(events), len(lines), len(changed)))
    db.commit()
    return len(changed)


def parse_names(text: str) -> list[tuple]:
    """(name 1, name 2, match id) from the English feed; the Latin names tie players to the bases."""
    block = re.search(r'<Sport[^>]*Name="Tennis".*?</Sport>', text, re.S)
    if not block:
        return []
    names = []
    for tag in re.findall(r"<Match ([^>]*)>", block.group(0)):
        m = attributes(tag)
        if m.get("Id") and m.get("Team1") and m.get("Team2"):
            names.append((m["Team1"], m["Team2"], m["Id"]))
    return names


def store_names(db: sqlite3.Connection, names: list[tuple]) -> None:
    db.executemany("UPDATE winline_event SET team1_en = ?, team2_en = ? WHERE wl_id = ?", names)
    db.commit()


def add_name_columns(db: sqlite3.Connection) -> None:
    """A database made before the English feed was read lacks these two columns."""
    known = {row[1] for row in db.execute("PRAGMA table_info(winline_event)")}
    for column in ("team1_en", "team2_en"):
        if column not in known:
            db.execute(f"ALTER TABLE winline_event ADD COLUMN {column} TEXT")
    db.commit()


def fetch(url: str = FEED) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8", "replace")


def run_once(db: sqlite3.Connection) -> None:
    events, lines = parse_feed(fetch())
    if not events:
        print(f"{datetime.now(timezone.utc).isoformat()} no tennis in the feed, nothing stored", flush=True)
        return
    taken_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changed = store(db, events, lines, taken_at)
    names = parse_names(fetch(FEED_EN))
    store_names(db, names)
    print(f"{taken_at} events {len(events)}, lines {len(lines)}, changed {changed}, latin names {len(names)}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--minutes", type=float, default=10.0)
    args = parser.parse_args()
    db = sqlite3.connect(args.db, timeout=60)
    db.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    add_name_columns(db)
    while True:
        try:
            run_once(db)
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                print(f"STOPPED: HTTP {error.code} from the feed.", flush=True)
                return 2
            print(f"HTTP {error.code}, trying again next time", flush=True)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            print(f"network: {error}, trying again next time", flush=True)
        if not args.loop:
            return 0
        time.sleep(args.minutes * 60)


if __name__ == "__main__":
    sys.exit(main())
