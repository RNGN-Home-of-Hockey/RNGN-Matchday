#!/usr/bin/env python3
"""Collect the WTA's data from api.wtatennis.com into a resumable local store.

Runs on the WTA partnership: terms of wtatennis.com allow mass collection only with written
permission from the WTA, so the partner document must exist before a full run. The client
identifies itself, keeps a pause between requests and stops on 403 or 429 instead of working
around them.

Every response is stored as received (gzip) so that parsing can be redone without new requests.
Work is a queue in SQLite: stop at any moment and the next run goes on from there.

    python tools/tennis/wta_collect.py --max 100     # a pilot
    python tools/tennis/wta_collect.py               # until the queue is empty
    python tools/tennis/wta_collect.py --status
    python tools/tennis/wta_collect.py --add-missing-stats   # see missing_stats_tasks
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.wtatennis.com/tennis"
USER_AGENT = "RNGN-Matchday-data/0.1"
DEFAULT_DB = Path(__file__).resolve().parents[2] / "local-data" / "tennis" / "raw" / "wta.sqlite"
PAGE_SIZE = 100
FIRST_YEAR = 1990
MAX_TRIES = 3
FIRST_STATS_YEAR = 2017
QUALIFYING_IDS = 63

# Lower runs first: the directories, then tour-level matches newest first, then the players.
PRIORITY_DIRECTORY = 10
PRIORITY_TOURNAMENT = 20
PRIORITY_MATCH_STATS = 30
PRIORITY_RANKED_PLAYER = 60
PRIORITY_PLAYER = 70
PRIORITY_PLAYER_YEAR = 80

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
  url TEXT PRIMARY KEY, kind TEXT NOT NULL, priority INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'todo', tries INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_queue_next ON queue (status, priority);
CREATE TABLE IF NOT EXISTS response (
  url TEXT PRIMARY KEY, http_status INTEGER NOT NULL, body_gz BLOB, fetched_at TEXT NOT NULL
);
"""

Task = tuple[str, str, int]  # (url path, kind, priority)


class Blocked(RuntimeError):
    """The server said stop. Nothing here tries to get around it."""


def player_tasks(player_id, priority: int) -> list[Task]:
    return [
        (f"/players/{player_id}/matches/?page=0&pageSize={PAGE_SIZE}&sort=desc", "player_matches", priority),
        (f"/players/{player_id}/records", "player_records", priority),
    ]


def seed_tasks(this_year: int) -> list[Task]:
    tasks: list[Task] = [
        (f"/players/?page=0&pageSize={PAGE_SIZE}", "players_page", PRIORITY_DIRECTORY),
        (f"/players/ranked?page=0&pageSize={PAGE_SIZE}&type=rankSingles&sort=asc&metric=SINGLES",
         "ranked_page", PRIORITY_DIRECTORY),
    ]
    for year in range(this_year, FIRST_YEAR - 1, -1):
        tasks.append((f"/tournaments/?page=0&pageSize={PAGE_SIZE}&from={year}-01-01&to={year}-12-31",
                      "tournaments_page", PRIORITY_DIRECTORY))
    return tasks


def next_page(url: str, page: int) -> str:
    return url.replace(f"page={page}&", f"page={page + 1}&", 1)


def page_of(url: str) -> int:
    return int(url.split("page=", 1)[1].split("&", 1)[0])


def discover(kind: str, url: str, body) -> list[Task]:
    """What a response tells us to fetch next. Pure, so it can be tested and replayed."""
    tasks: list[Task] = []
    if kind == "players_page":
        for player in body.get("content", []):
            if str(player.get("fullName") or "").strip():
                tasks += player_tasks(player["id"], PRIORITY_PLAYER)
        info = body.get("pageInfo", {})
        page = page_of(url)
        if (page + 1) * info.get("pageSize", PAGE_SIZE) < info.get("numEntries", 0):
            tasks.append((next_page(url, page), kind, PRIORITY_DIRECTORY))
    elif kind == "ranked_page":
        for row in body or []:
            tasks += player_tasks(row["player"]["id"], PRIORITY_RANKED_PLAYER)
        if body:
            tasks.append((next_page(url, page_of(url)), kind, PRIORITY_DIRECTORY))
    elif kind == "tournaments_page":
        for tournament in body.get("content", []):
            group = tournament.get("tournamentGroup") or {}
            # ITF events have no match list here; their results come through the players' pages.
            if group.get("id") and group.get("level") != "ITF":
                tasks.append((f"/tournaments/{group['id']}/{tournament['year']}/matches?pageSize=500",
                              "tournament_matches", PRIORITY_TOURNAMENT))
        info = body.get("pageInfo", {})
        page = page_of(url)
        if (page + 1) * info.get("pageSize", PAGE_SIZE) < info.get("numEntries", 0):
            tasks.append((next_page(url, page), kind, PRIORITY_DIRECTORY))
    elif kind == "tournament_matches":
        base = url.split("?", 1)[0]
        for match in body.get("matches", []):
            if match.get("MatchID") and match.get("MatchState") == "F":
                tasks.append((f"{base}/{match['MatchID']}/stats", "match_stats", PRIORITY_MATCH_STATS))
    elif kind == "player_matches":
        matches = body.get("matches", [])
        player_id = url.split("/players/", 1)[1].split("/", 1)[0]
        for year in sorted({str(m.get("tourn_year")) for m in matches if m.get("tourn_year")}):
            tasks.append((f"/players/{player_id}/year/{year}", "player_year", PRIORITY_PLAYER_YEAR))
        if len(matches) >= PAGE_SIZE:
            page = page_of(url)
            tasks.append((next_page(url, page), kind, PRIORITY_PLAYER))
    return tasks


def bracket_size(draw) -> int:
    full = 2
    while full < (draw or 0):
        full *= 2
    return full


def stats_tasks_without_list(group_id: int, year: int, singles_draw, doubles_draw) -> list[Task]:
    """Every match id an event's bracket can have. Ids follow the bracket (LS001 the final, LS002-003
    the semi-finals...), counted on the full bracket, so an id that was a bye simply comes back empty.
    Qualifying (RS) has no size in the directory, and real lists reach 55 whatever the main draw."""
    singles = bracket_size(singles_draw or 32)
    doubles = bracket_size(doubles_draw or 16)
    ids = [f"LS{n:03d}" for n in range(1, singles)] + [f"LD{n:03d}" for n in range(1, doubles)]
    ids += [f"RS{n:03d}" for n in range(1, QUALIFYING_IDS + 1)]
    return [(f"/tournaments/{group_id}/{year}/matches/{i}/stats", "match_stats", PRIORITY_MATCH_STATS) for i in ids]


def body_of(db: sqlite3.Connection, url: str):
    row = db.execute("SELECT body_gz FROM response WHERE url = ?", (url,)).fetchone()
    return json.loads(gzip.decompress(row[0])) if row and row[0] else None


def missing_stats_tasks(db: sqlite3.Connection, today: str) -> list[Task]:
    """Statistics of finished tour events whose match list is missing or empty: /stats still answers
    for many of them from 2017 on, and nothing else leads to those addresses. Slams use unguessable
    ids and ITF has no statistics. Each read is short, as the collector may be writing."""
    events = {}
    pages = [r[0] for r in db.execute("SELECT url FROM queue WHERE kind = 'tournaments_page' AND status = 'done'").fetchall()]
    for page in pages:
        for event in (body_of(db, page) or {}).get("content", []):
            group = event.get("tournamentGroup") or {}
            if (group.get("id") and event.get("year", 0) >= FIRST_STATS_YEAR and (event.get("endDate") or "9") < today
                    and group.get("level") != "ITF" and event.get("level") not in ("ITF", "Grand Slam")):
                events[(group["id"], event["year"])] = (event.get("singlesDrawSize"), event.get("doublesDrawSize"))
    lists = db.execute("SELECT url, status FROM queue WHERE kind = 'tournament_matches' AND status IN ('done', 'missing')").fetchall()
    tasks: list[Task] = []
    for url, status in lists:
        group_id, year = (int(x) for x in url.split("/")[2:4])
        if (group_id, year) in events and (status == "missing" or not (body_of(db, url) or {}).get("matches")):
            tasks += stats_tasks_without_list(group_id, year, *events[(group_id, year)])
    return tasks


def add_tasks(db: sqlite3.Connection, tasks: list[Task]) -> None:
    """Add new work. A task seen again keeps its state but moves up if it became more urgent."""
    db.executemany(
        """INSERT INTO queue (url, kind, priority) VALUES (?, ?, ?)
           ON CONFLICT(url) DO UPDATE SET priority = MIN(priority, excluded.priority)""",
        tasks,
    )


def next_task(db: sqlite3.Connection):
    return db.execute(
        "SELECT url, kind, tries FROM queue WHERE status = 'todo' ORDER BY priority, rowid LIMIT 1"
    ).fetchone()


def record(db: sqlite3.Connection, url: str, status: int, raw: bytes | None) -> None:
    db.execute(
        "INSERT OR REPLACE INTO response (url, http_status, body_gz, fetched_at) VALUES (?, ?, ?, ?)",
        (url, status, gzip.compress(raw) if raw else None, datetime.now(timezone.utc).isoformat()),
    )


def fetch(url: str) -> tuple[int, bytes | None]:
    request = urllib.request.Request(API + url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, None


def step(db: sqlite3.Connection, url: str, kind: str, tries: int, fetcher=fetch) -> int:
    """One request and its bookkeeping. Returns the HTTP status (0 for a network failure)."""
    try:
        status, raw = fetcher(url)
    except (urllib.error.URLError, TimeoutError, OSError):
        status, raw = 0, None
    if status in (403, 429):
        raise Blocked(f"HTTP {status} for {url}")
    body = None
    if status == 200:
        try:
            body = json.loads(raw) if raw and raw.strip() else None
        except ValueError:
            status = 0
    if status == 200:
        record(db, url, 200, raw)
        add_tasks(db, discover(kind, url, body) if body else [])
        db.execute("UPDATE queue SET status = 'done', tries = ? WHERE url = ?", (tries + 1, url))
    elif status == 404:
        record(db, url, 404, None)
        db.execute("UPDATE queue SET status = 'missing', tries = ? WHERE url = ?", (tries + 1, url))
    else:
        state = "failed" if tries + 1 >= MAX_TRIES else "todo"
        # A retried task goes to the back of its priority class, so one bad url cannot hold the run.
        db.execute("UPDATE queue SET status = ?, tries = ?, priority = priority + 1 WHERE url = ?",
                   (state, tries + 1, url))
    db.commit()
    return status


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.executescript(SCHEMA)
    return db


def print_status(db: sqlite3.Connection) -> None:
    for kind, status, count in db.execute(
        "SELECT kind, status, COUNT(*) FROM queue GROUP BY kind, status ORDER BY kind, status"
    ):
        print(f"{kind:20} {status:8} {count}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--pause", type=float, default=1.0, help="seconds between requests")
    parser.add_argument("--max", type=int, default=0, help="stop after this many requests")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--add-missing-stats", action="store_true",
                        help="only queue statistics of events without a match list, then exit; safe during a run")
    args = parser.parse_args()

    db = open_db(args.db)
    if args.status:
        print_status(db)
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    if args.add_missing_stats:
        tasks = missing_stats_tasks(db, today)
        add_tasks(db, tasks)
        db.commit()
        print(f"statistics addresses for events without a list: {len(tasks)}")
        return 0
    add_tasks(db, seed_tasks(datetime.now(timezone.utc).year))
    add_tasks(db, missing_stats_tasks(db, today))
    db.commit()

    done = failures_in_a_row = 0
    while not args.max or done < args.max:
        task = next_task(db)
        if task is None:
            print("queue is empty")
            break
        try:
            status = step(db, *task)
        except Blocked as blocked:
            print(f"STOPPED: {blocked}. Run again later or ask the WTA for access.", flush=True)
            return 2
        done += 1
        failures_in_a_row = failures_in_a_row + 1 if status not in (200, 404) else 0
        if failures_in_a_row >= 20:
            print("STOPPED: 20 failures in a row, the API or the network is down.", flush=True)
            return 3
        if done % 500 == 0:
            print(f"{datetime.now(timezone.utc).isoformat()} requests: {done}", flush=True)
        time.sleep(args.pause)
    print_status(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
