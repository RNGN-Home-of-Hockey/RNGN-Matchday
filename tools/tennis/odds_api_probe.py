#!/usr/bin/env python3
"""A look at The Odds API before buying anything: which tennis it has, how deep, what a download costs.

The key is asked for hidden, kept only in local-data (not in git) and never printed; the API takes it in
the address, so addresses are never printed either. Spends at most --max-credits.

    python tools/tennis/odds_api_probe.py save-key
    python tools/tennis/odds_api_probe.py probe --max-credits 100
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEY_FILE = HERE.parents[1] / "local-data" / "tennis" / "secrets" / "odds_api.key"
API = "https://api.the-odds-api.com/v4"
USER_AGENT = "RNGN-Matchday-data/0.1"
# A Slam each year from before the history is said to begin, to find where it starts.
DEPTH_DATES = ("2026-01-25", "2024-01-20", "2023-01-20", "2021-02-12", "2020-09-05", "2020-01-25", "2019-01-20")
MARKETS = "h2h,spreads,totals"
DEFAULT_DB = HERE.parents[1] / "local-data" / "tennis" / "tennis.sqlite"
OUT = HERE.parents[1] / "local-data" / "tennis" / "raw" / "odds_api_depth.json"
PAUSE = 0.3
# A fragment of their tournament key -> how tennis-data names it.
TD_PATTERNS = {
    "aus_open": "Australian Open", "french_open": "French Open", "wimbledon": "Wimbledon", "us_open": "US Open",
    "indian_wells": "Indian Wells", "miami": "Miami", "madrid": "Madrid", "italian": "Rome", "canadian": "Montreal|Toronto",
    "cincinnati": "Cincinnati", "china_open": "Beijing", "shanghai": "Shanghai", "paris_masters": "Paris.*Masters|Masters.*Paris|Paris.*Rolex|Paris Masters",
    "dubai": "Dubai", "qatar": "Doha", "barcelona": "Barcelona", "halle": "Halle", "hamburg": "Hamburg", "japan_open": "Tokyo",
    "munich": "Munich", "queens": "Queen", "swiss_indoors": "Basel", "vienna": "Vienna", "washington": "Washington",
    "monte_carlo": "Monte", "bad_homburg": "Bad Homburg", "charleston": "Charleston", "german_open": "Berlin", "guadalajara": "Guadalajara",
    "monterrey": "Monterrey", "ningbo": "Ningbo", "pan_pacific": "Tokyo", "singapore": "Singapore", "strasbourg": "Strasbourg",
    "stuttgart": "Stuttgart", "wuhan": "Wuhan",
}


def read_key() -> str:
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        raise SystemExit("Ключа нет: сначала save-key") from None


def save_key() -> int:
    key = getpass.getpass("Вставьте ключ (на экране не появится): ").strip()
    if not key:
        print("Пусто, ничего не сохранено")
        return 1
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(key)
    print(f"Ключ сохранён, знаков: {len(key)}")
    return 0


class Client:
    def __init__(self, key: str, max_credits: int):
        self.key, self.left_to_spend = key, max_credits
        self.remaining = self.used = None

    def get(self, path: str, cost: int = 0, **params):
        """(status, body). Refuses what would go over the budget. Never shows the address."""
        if cost > self.left_to_spend:
            return None, {"message": "пропущено: вышло бы за лимит кредитов"}
        query = urllib.parse.urlencode({"apiKey": self.key, **params})
        request = urllib.request.Request(f"{API}{path}?{query}", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, raw, headers = response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            status, raw, headers = error.code, error.read(), error.headers
        except (urllib.error.URLError, TimeoutError) as error:
            return None, {"message": f"сеть: {type(error).__name__}"}
        self.remaining = headers.get("x-requests-remaining", self.remaining)
        self.used = headers.get("x-requests-used", self.used)
        spent = headers.get("x-requests-last")
        self.left_to_spend -= int(spent) if spent and spent.isdigit() else 0
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, {"message": raw[:200].decode("utf-8", "replace")}


def message_of(body) -> str:
    return (body.get("message") or body.get("error_code") or "") if isinstance(body, dict) else ""


def bookmaker_markets(events: list) -> tuple[set, dict]:
    books, markets = set(), {}
    for event in events:
        for book in event.get("bookmakers", []):
            books.add(book["key"])
            for market in book.get("markets", []):
                markets[market["key"]] = markets.get(market["key"], 0) + 1
    return books, markets


def probe(client: Client) -> int:
    status, sports = client.get("/sports", all="true")
    if status != 200:
        print("Ключ не принят:", status, message_of(sports))
        return 1
    tennis = [s for s in sports if s.get("group") == "Tennis"]
    print(f"Теннисных турниров у них: {len(tennis)}; остаток кредитов: {client.remaining}, потрачено: {client.used}")
    for sport in tennis:
        print(f"  {sport['key']:36} {'идёт' if sport.get('active') else '-':5} {sport.get('title')}")

    active = [s["key"] for s in tennis if s.get("active")][:1]
    if active:
        status, events = client.get(f"/sports/{active[0]}/odds", cost=3, regions="eu", markets=MARKETS)
        if status == 200:
            books, markets = bookmaker_markets(events)
            print(f"\nСейчас {active[0]}: матчей {len(events)}, контор {len(books)}, рынки по строкам {markets}")
        else:
            print("\nТекущие коэффициенты:", status, message_of(events))

    print("\nИстория, Australian Open / US Open WTA:")
    for day in DEPTH_DATES:
        key = "tennis_wta_us_open" if day.startswith("2020-09") else "tennis_wta_aus_open_singles"
        status, events = client.get(f"/historical/sports/{key}/events", cost=1, date=f"{day}T12:00:00Z")
        if status != 200:
            print(f"  {day} {key}: {status} {message_of(events)}")
            if status in (401, 403):
                print("  История этому тарифу недоступна, дальше не спрашиваю.")
                break
            continue
        found = events.get("data", []) if isinstance(events, dict) else []
        print(f"  {day} {key}: событий {len(found)}, снимок {events.get('timestamp') if isinstance(events, dict) else '?'}")
        if not found:
            continue
        status, odds = client.get(f"/historical/sports/{key}/odds", cost=30, date=f"{day}T12:00:00Z", regions="eu", markets=MARKETS)
        if status == 200:
            data = odds.get("data", [])
            books, markets = bookmaker_markets(data)
            print(f"    коэффициенты: матчей {len(data)}, контор {len(books)}, рынки по строкам {markets}")
        else:
            print(f"    коэффициенты: {status} {message_of(odds)}")
    print(f"\nОстаток кредитов: {client.remaining}, потрачено всего: {client.used}")
    return 0


def tournament_days(db: sqlite3.Connection, tour: str, pattern: str) -> dict:
    """Season -> [(day, matches that day)], and only the main event: a pattern is a tournament's name or place."""
    rx = re.compile(pattern, re.I)
    days = defaultdict(Counter)
    for season, location, name, played in db.execute(
            "SELECT season, location, tournament, match_date FROM td_match WHERE tour = ? AND season >= 2020 AND match_date IS NOT NULL", (tour,)):
        if rx.search(f"{location} {name}"):
            days[season][played] += 1
    return {season: sorted(found.items()) for season, found in days.items()}


def fewest_snapshots(seen_by_day: dict) -> int:
    """How many snapshots a download would need to see every match seen on any day, greedily."""
    left = set().union(*seen_by_day.values()) if seen_by_day else set()
    needed = 0
    while left:
        best = max(seen_by_day.values(), key=lambda ids: len(ids & left))
        left -= best
        needed += 1
    return needed


def depth(client: Client, db: sqlite3.Connection) -> int:
    """Spends the budget on historical event lists: every tournament by season, then whole tournaments day by day."""
    keys = {s["key"]: s for s in client.get("/sports", all="true")[1] if s.get("group") == "Tennis"}
    seasons = defaultdict(dict)
    print("Сезон за сезоном: сколько матчей API показывает в середине турнира")
    for key in keys:
        tour = "ATP" if "_atp_" in key else "WTA"
        pattern = next((p for fragment, p in TD_PATTERNS.items() if fragment in key), None)
        if not pattern:
            print(f"  {key}: нет соответствия в tennis-data")
            continue
        for season, days in sorted(tournament_days(db, tour, pattern).items()):
            if client.left_to_spend < 1:
                break
            day = days[len(days) // 2][0]
            status, body = client.get(f"/historical/sports/{key}/events", cost=1, date=f"{day}T12:00:00Z")
            if status == 200:
                seasons[key][season] = {"day": day, "events": len(body.get("data", [])), "td_matches": sum(n for _, n in days)}
            time.sleep(PAUSE)
        row = " ".join(f"{s}:{v['events']}" for s, v in sorted(seasons[key].items()))
        print(f"  {key}: {row}  остаток {client.remaining}", flush=True)
    samples = []
    print("\nДень за днём: доля матчей и число снимков")
    # Biggest and smallest events first, newest seasons first, until the credits run out.
    order = [k for k in keys if k in seasons and seasons[k]]
    order.sort(key=lambda k: -max(v["td_matches"] for v in seasons[k].values()))
    for season in (2025, 2023, 2021, 2024, 2022):
        for key in order[::3]:
            info = seasons[key].get(season)
            if not info or not info["events"] or client.left_to_spend < 16:
                continue
            tour = "ATP" if "_atp_" in key else "WTA"
            pattern = next(p for fragment, p in TD_PATTERNS.items() if fragment in key)
            days = tournament_days(db, tour, pattern)[season]
            seen = {}
            for day, _ in days:
                status, body = client.get(f"/historical/sports/{key}/events", cost=1, date=f"{day}T06:00:00Z")
                if status == 200:
                    seen[day] = {e["id"] for e in body.get("data", [])}
                time.sleep(PAUSE)
            union = set().union(*seen.values()) if seen else set()
            needed = fewest_snapshots(seen)
            matches = sum(n for _, n in days)
            samples.append({"key": key, "season": season, "days": len(days), "td_matches": matches, "api_events": len(union), "fewest_snapshots": needed})
            print(f"  {key} {season}: дней {len(days)}, матчей в tennis-data {matches}, событий в API {len(union)}, снимков минимум {needed}, остаток {client.remaining}", flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"seasons": seasons, "samples": samples}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nОстаток кредитов: {client.remaining}, потрачено всего: {client.used}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["save-key", "probe", "depth"])
    parser.add_argument("--max-credits", type=int, default=100)
    args = parser.parse_args()
    if args.command == "save-key":
        return save_key()
    if args.command == "depth":
        return depth(Client(read_key(), args.max_credits), sqlite3.connect(f"{DEFAULT_DB.resolve().as_uri()}?mode=ro", uri=True))
    return probe(Client(read_key(), args.max_credits))


if __name__ == "__main__":
    sys.exit(main())
