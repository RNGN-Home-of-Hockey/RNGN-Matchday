#!/usr/bin/env python3
"""Morning hints for the HoH editors: upcoming NHL events and player milestones.

The bot only tells people what is coming. It publishes nothing anywhere else.

Usage:
  python tools/nhl_morning_digest.py                  print today's digest
  python tools/nhl_morning_digest.py --date 2026-11-09
  python tools/nhl_morning_digest.py --export-events data/nhl_events_2026_27.csv
  python tools/nhl_morning_digest.py --export-records data/nhl_record_watch_status.csv
  python tools/nhl_morning_digest.py --send           needs TELEGRAM_BOT_TOKEN and NHL_DIGEST_CHAT_ID

Editors can change two files without touching code:
  data/nhl_events_manual.json  {"events": [{"date": "2027-03-05", "title": "...", "importance": "high"}]}
  data/nhl_record_watch.json   {"watch": [{"player_id": 1, "stat": "goals", "target": 950, "alert_at": 3}]}
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_nhl_milestones as milestones  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MSK = ZoneInfo("Europe/Moscow")
UA = "HOH-NHL-Digest/1.0"
API = "https://api-web.nhle.com/v1"
ALERT_DAYS = (14, 3)
DEFAULT_ALERT_AT = 3
MAX_RECORD_LINES = 8
TELEGRAM_LIMIT = 4000
INACTIVE_AFTER_DAYS = 30
RUSSIAN_BIRTH_COUNTRIES = {"RUS"}

TEAM_RU = {
    "ANA": "Анахайм", "BOS": "Бостон", "BUF": "Баффало", "CAR": "Каролина", "CBJ": "Коламбус",
    "CGY": "Калгари", "CHI": "Чикаго", "COL": "Колорадо", "DAL": "Даллас", "DET": "Детройт",
    "EDM": "Эдмонтон", "FLA": "Флорида", "LAK": "Лос-Анджелес", "MIN": "Миннесота", "MTL": "Монреаль",
    "NJD": "Нью-Джерси", "NSH": "Нэшвилл", "NYI": "Айлендерс", "NYR": "Рейнджерс", "OTT": "Оттава",
    "PHI": "Филадельфия", "PIT": "Питтсбург", "SEA": "Сиэтл", "SJS": "Сан-Хосе", "STL": "Сент-Луис",
    "TBL": "Тампа-Бэй", "TOR": "Торонто", "UTA": "Юта", "VAN": "Ванкувер", "VGK": "Вегас",
    "WPG": "Виннипег", "WSH": "Вашингтон",
}
VENUE_RU = {
    "Veikkaus Arena": "Хельсинки, Veikkaus Arena",
    "PSD Bank Dome": "Дюссельдорф, PSD Bank Dome",
    "Rice-Eccles Stadium": "Солт-Лейк-Сити, Rice-Eccles Stadium",
    "AT&T Stadium": "Арлингтон, AT&T Stadium",
    "Princess Auto Stadium": "Виннипег, Princess Auto Stadium",
}
EVENT_RU = (
    ("Global Series", "Global Series"),
    ("Winter Classic", "Зимняя классика"),
    ("Stadium Series", "Стадионная серия"),
    ("Heritage Classic", "Heritage Classic"),
)
OUTDOOR_WORDS = ("Classic", "Stadium Series")
MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
             "сентября", "октября", "ноября", "декабря")

# Name in the NHL feed -> (key in player landing careerTotals, Russian genitive plural).
STATS = {
    "goals": ("goals", "голов"),
    "assists": ("assists", "передач"),
    "points": ("points", "очков"),
    "games played": ("gamesPlayed", "матчей"),
    "wins": ("wins", "побед"),
    "shutouts": ("shutouts", "матчей на ноль"),
}
# Lowest target worth a morning message. Smaller round numbers would flood the channel.
MIN_TARGET = {"goals": 300, "assists": 500, "points": 700, "games played": 1000, "wins": 300, "shutouts": 50}
MIN_TARGET_RUSSIAN = {"goals": 100, "assists": 100, "points": 100, "games played": 500, "wins": 50, "shutouts": 10}


def http_json(url: str, attempts: int = 3) -> dict:
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def season_of(day: dt.date) -> str:
    start = day.year if day.month >= 7 else day.year - 1
    return f"{start}{start + 1}"


def fetch_games(day: dt.date) -> list[dict]:
    """All regular-season games of the season. A failed team fetch stops the run on purpose:
    a partial schedule would silently hide events."""
    games = {}
    for team in TEAM_RU:
        payload = http_json(f"{API}/club-schedule-season/{team}/{season_of(day)}")
        for game in payload.get("games", []):
            if game.get("gameType") == 2:
                games[game["id"]] = game
    if not games:
        raise RuntimeError("NHL schedule came back empty")
    return sorted(games.values(), key=lambda g: g["startTimeUTC"])


def game_start(game: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(game["startTimeUTC"].replace("Z", "+00:00"))


def matchup(game: dict) -> str:
    return f'{team_ru(game["awayTeam"]["abbrev"])} — {team_ru(game["homeTeam"]["abbrev"])}'


def team_ru(tri: str) -> str:
    return TEAM_RU.get(tri, tri)


def format_day(day: dt.date) -> str:
    return f"{day.day} {MONTHS_RU[day.month - 1]}"


def format_game_time(game: dict) -> str:
    start = game_start(game).astimezone(MSK)
    return f"{format_day(start.date())}, {start:%H:%M} МСК"


def is_abroad(game: dict) -> bool:
    return not str(game.get("venueTimezone", "")).startswith(("America/", "US/", "Canada/"))


def event_title(name: str) -> str:
    return next((ru for key, ru in EVENT_RU if key in name), name)


def build_events(games: list[dict], manual: list[dict] | None = None) -> list[dict]:
    """Season event base: abroad and outdoor games, special events, big slates, manual rows."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for game in games:
        special = (game.get("specialEvent") or {}).get("name", {}).get("default")
        if special or is_abroad(game):
            groups[(special or "Матч за границей", game["venue"]["default"])].append(game)

    events = []
    for (name, venue), group in groups.items():
        if is_abroad(group[0]):
            kind = "матч за границей"
        elif any(word in name for word in OUTDOOR_WORDS):
            kind = "на открытом воздухе"
        else:
            kind = "особое событие"
        events.append({
            "date": game_start(group[0]).astimezone(MSK).date(),
            "title": event_title(name),
            "place": VENUE_RU.get(venue, venue),
            "kind": kind,
            "importance": "high",
            "games": [f"{matchup(g)}, {format_game_time(g)}" for g in group],
            "source": "расписание НХЛ",
        })

    per_day = Counter(game["gameDate"] for game in games)
    last_day = max(per_day)
    for day, count in per_day.items():
        if count < 14:
            continue
        teams = count * 2
        all_teams = teams >= len(TEAM_RU)
        note = "играют все 32 команды" if all_teams else f"играют {teams} команд из {len(TEAM_RU)}"
        if day == last_day:
            note += ", последний день регулярки"
        note += " (дата по Северной Америке, в Москве в основном следующая ночь)"
        events.append({
            "date": dt.date.fromisoformat(day),
            "title": f"Большой игровой день: {count} матчей",
            "place": note,
            "kind": "большой игровой день",
            "importance": "high" if all_teams else "medium" if count >= 15 else "low",
            "games": [],
            "source": "расписание НХЛ",
        })

    for row in manual or []:
        events.append({
            "date": dt.date.fromisoformat(row["date"]),
            "title": row["title"],
            "place": row.get("place", ""),
            "kind": row.get("kind", "из таблицы редакции"),
            "importance": row.get("importance", "medium"),
            "games": [],
            "source": row.get("source", "таблица редакции"),
        })
    return sorted(events, key=lambda e: (e["date"], e["title"]))


def events_due(events: list[dict], today: dt.date) -> list[dict]:
    due = []
    for event in events:
        days = (event["date"] - today).days
        if days in ALERT_DAYS and event["importance"] in ("high", "medium"):
            due.append({**event, "days": days})
    return due


def games_in_window(games: list[dict], now: dt.datetime) -> dict[str, dict]:
    """Team -> its game in the next 24 hours."""
    upcoming = {}
    for game in games:
        if now <= game_start(game) < now + dt.timedelta(hours=24):
            upcoming[game["awayTeam"]["abbrev"]] = game
            upcoming[game["homeTeam"]["abbrev"]] = game
    return upcoming


def feed_candidates(rows: list[dict]) -> list[dict]:
    """Rows of the official NHL feed still ahead, regular season, for stats we report."""
    out = []
    for row in rows:
        stat = str(row.get("milestone") or "").strip().lower()
        if stat not in STATS or row.get("gameTypeId") != 2:
            continue
        current = milestones.current_value("", row)
        if current is None:
            continue
        out.append({
            "player_id": int(row["playerId"]), "team": row.get("teamAbbrev"), "stat": stat,
            "target": int(row["milestoneAmount"]), "current": int(current),
            "alert_at": DEFAULT_ALERT_AT, "name": row.get("playerFullName"),
        })
    return out


def is_significant(stat: str, target: int, russian: bool) -> bool:
    table = MIN_TARGET_RUSSIAN if russian else MIN_TARGET
    return target >= table[stat]


def is_active(landing: dict, today: dt.date) -> bool:
    """Skip players on the books who are not playing, e.g. long-term injured."""
    recent = landing.get("last5Games") or []
    if not recent or not landing.get("isActive"):
        return False
    last = dt.date.fromisoformat(recent[0]["gameDate"])
    return (today - last).days <= INACTIVE_AFTER_DAYS


def confirm_candidate(candidate: dict, landing: dict, names_ru: dict, today: dt.date) -> dict | None:
    """Recheck a line against the player's own NHL page data. That number wins over the feed."""
    if not is_active(landing, today):
        return None
    totals = (landing.get("careerTotals") or {}).get("regularSeason") or {}
    value = totals.get(STATS[candidate["stat"]][0])
    if value is None:
        return None
    remaining = candidate["target"] - int(value)
    if not 1 <= remaining <= candidate["alert_at"]:
        return None
    russian = landing.get("birthCountry") in RUSSIAN_BIRTH_COUNTRIES
    if not candidate.get("forced") and not is_significant(candidate["stat"], candidate["target"], russian):
        return None
    pid = str(landing.get("playerId") or candidate["player_id"])
    default_name = f'{landing["firstName"]["default"]} {landing["lastName"]["default"]}'
    return {
        **candidate,
        "current": int(value),
        "remaining": remaining,
        "russian": russian,
        "name": names_ru.get(pid) or default_name,
        "team": landing.get("currentTeamAbbrev") or candidate.get("team"),
    }


def collect_records(rows: list[dict], watch: list[dict], upcoming: dict, names_ru: dict,
                    today: dt.date, get_landing=None) -> list[dict]:
    get_landing = get_landing or (lambda pid: http_json(f"{API}/player/{pid}/landing"))
    candidates = {}
    for row in feed_candidates(rows):
        if row["team"] in upcoming and 1 <= row["target"] - row["current"] <= DEFAULT_ALERT_AT:
            candidates[(row["player_id"], row["stat"], row["target"])] = row
    for item in watch:
        key = (int(item["player_id"]), item["stat"], int(item["target"]))
        candidates[key] = {
            "player_id": key[0], "stat": key[1], "target": key[2], "team": None, "forced": True,
            "alert_at": int(item.get("alert_at", DEFAULT_ALERT_AT)), "note": item.get("note"),
        }
    landings = {}
    records = []
    for candidate in candidates.values():
        pid = candidate["player_id"]
        if pid not in landings:
            landings[pid] = get_landing(pid)
        confirmed = confirm_candidate(candidate, landings[pid], names_ru, today)
        if confirmed and confirmed["team"] in upcoming:
            confirmed["game"] = upcoming[confirmed["team"]]
            records.append(confirmed)
    return sorted(records, key=lambda r: (not r["russian"], r["remaining"], -r["target"]))


def opponent(game: dict, team: str) -> str:
    away, home = game["awayTeam"]["abbrev"], game["homeTeam"]["abbrev"]
    return team_ru(home if away == team else away)


def days_phrase(days: int) -> str:
    return "через 3 дня" if days == 3 else "через 2 недели" if days == 14 else f"через {days} дн."


def render(today: dt.date, due: list[dict], records: list[dict]) -> str | None:
    """Digest text, or None when there is nothing useful: then nothing is sent."""
    if not due and not records:
        return None
    lines = [f"☀️ Подсказки редакции на {format_day(today)}"]
    if due:
        lines += ["", "📅 Пора готовиться"]
        for event in due:
            lines.append(f'• {days_phrase(event["days"])}: {event["title"]}, {event["place"]} ({format_day(event["date"])})')
            lines += [f"   {game}" for game in event["games"]]
    if records:
        lines += ["", "🎯 Рекорды: у кого матч в ближайшие сутки"]
        for record in records[:MAX_RECORD_LINES]:
            label = STATS[record["stat"]][1]
            flag = "🇷🇺 " if record["russian"] else ""
            start = game_start(record["game"]).astimezone(MSK)
            lines.append(
                f'• {flag}{record["name"]} ({team_ru(record["team"])}): до {record["target"]} {label} '
                f'осталось {record["remaining"]} (сейчас {record["current"]}). '
                f'Соперник: {opponent(record["game"], record["team"])}, начало в {start:%H:%M} МСК'
            )
        if len(records) > MAX_RECORD_LINES:
            lines.append(f"…и ещё {len(records) - MAX_RECORD_LINES} менее заметных")
    return "\n".join(lines)[:TELEGRAM_LIMIT]


def send_telegram(token: str, chat_id: str, text: str) -> None:
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    with urllib.request.urlopen(req, timeout=30) as response:
        if not json.load(response).get("ok"):
            raise RuntimeError("Telegram did not accept the digest")


def read_json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def export_events(events: list[dict], path: str) -> None:
    labels = {"high": "высокая", "medium": "средняя", "low": "низкая"}
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["Дата", "Событие", "Где / что", "Тип", "Важность", "Матчи", "Источник"])
        for event in events:
            writer.writerow([event["date"].isoformat(), event["title"], event["place"], event["kind"],
                             labels.get(event["importance"], event["importance"]), " | ".join(event["games"]), event["source"]])


def export_records(rows: list[dict], watch: list[dict], names_ru: dict, path: str) -> None:
    """The one-place table of what is being tracked, nearest first."""
    table = []
    for item in watch:
        landing = http_json(f'{API}/player/{item["player_id"]}/landing')
        value = ((landing.get("careerTotals") or {}).get("regularSeason") or {}).get(STATS[item["stat"]][0])
        if value is None:
            continue
        name = names_ru.get(str(item["player_id"])) or f'{landing["firstName"]["default"]} {landing["lastName"]["default"]}'
        table.append((item["target"] - int(value), name, team_ru(landing.get("currentTeamAbbrev", "")), STATS[item["stat"]][1],
                      item["target"], int(value), "свой список", item.get("note", "")))
    tracked = {(int(i["player_id"]), i["stat"], int(i["target"])) for i in watch}
    for row in feed_candidates(rows):
        remaining = row["target"] - row["current"]
        if remaining < 1 or (row["player_id"], row["stat"], row["target"]) in tracked:
            continue
        keep = is_significant(row["stat"], row["target"], False)
        if not keep and is_significant(row["stat"], row["target"], True):
            # The feed has no nationality, so lower targets need a look at the player's page.
            landing = http_json(f'{API}/player/{row["player_id"]}/landing')
            keep = landing.get("birthCountry") in RUSSIAN_BIRTH_COUNTRIES
        if keep:
            name = names_ru.get(str(row["player_id"])) or row["name"]
            table.append((remaining, name, team_ru(row["team"]), STATS[row["stat"]][1], row["target"], row["current"], "НХЛ", ""))
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["Осталось", "Игрок", "Команда", "Что", "Цель", "Сейчас", "Откуда", "Заметка"])
        writer.writerows(sorted(table, key=lambda r: (r[0], -r[4])))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="YYYY-MM-DD, the morning to simulate. Default: today in Moscow")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--export-events")
    parser.add_argument("--export-records")
    args = parser.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(MSK).date()
    if args.date:
        today = dt.date.fromisoformat(args.date)
        now = dt.datetime.combine(today, dt.time(10), MSK).astimezone(dt.timezone.utc)

    games = fetch_games(today)
    events = build_events(games, read_json("data/nhl_events_manual.json")["events"])
    watch = read_json("data/nhl_record_watch.json")["watch"]
    names_ru = read_json("ru_full_names.json")
    rows = milestones.fetch("skaters") + milestones.fetch("goalies")
    if not rows:
        raise RuntimeError("NHL milestone feed came back empty")

    if args.export_events:
        export_events(events, args.export_events)
    if args.export_records:
        export_records(rows, watch, names_ru, args.export_records)
    if args.export_events or args.export_records:
        return 0

    records = collect_records(rows, watch, games_in_window(games, now), names_ru, today)
    text = render(today, events_due(events, today), records)
    if text is None:
        print("Nothing useful today, no message.")
        return 0
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    if args.send:
        send_telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["NHL_DIGEST_CHAT_ID"], text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
