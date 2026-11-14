#!/usr/bin/env python3
"""Draft plates for WTA matches on Winline's line: facts, and prices they can stand next to.

Follows the hockey pipeline in docs/BETTING_INSIGHT_ENGINE.md: signal, evidence with its sample,
a real price, an editor who decides. A trial on 787 finished matches found no proven edge in
history against this market, so a plate states what happened and the price, never that a bet is
good. The gate below only picks what is worth an editor's look.

    python tools/tennis/tennis_plates.py
    python tools/tennis/tennis_plates.py --date 2026-11-05
    python tools/tennis/tennis_plates.py --player Саснович --json
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[2] / "local-data" / "tennis" / "tennis.sqlite"
MIN_ODDS = 1.50  # as the hockey selector
MAX_ODDS = 3.50
MIN_SAMPLE = 10
MIN_HIT = 0.60
SURFACE_MONTHS = 24
SURFACE_LIMIT = 20
FORM_MONTHS = 12
FORM_LIMIT = 10
Z90 = 1.2816
MAX_PLATES = 4
MIN_PRICED = 5
MIN_ROLE = 8
ROLE_LIMIT = 100
RESULTS_LIMIT = 20
TOURS = ("WTA", "ATP")
SURFACE_RU = {"hard": "харде", "clay": "грунте", "grass": "траве", "carpet": "ковре"}


def wilson_low(hits: int, n: int) -> float:
    if not n:
        return 0.0
    p = hits / n
    d = 1 + Z90 ** 2 / n
    return (p + Z90 ** 2 / (2 * n) - Z90 * math.sqrt(p * (1 - p) / n + Z90 ** 2 / (4 * n * n))) / d


def fair_price(mine: float, other: float) -> float:
    """What the market expects of this side, the bookmaker's margin taken out."""
    return (1 / mine) / (1 / mine + 1 / other)


def folded(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text.replace("ё", "е").replace("Ё", "Е")).lower()
    return "".join(c for c in plain if c.isalpha() and not unicodedata.combining(c))


def games_of(score: str | None) -> list[tuple[int, int]]:
    return [(int(a), int(b)) for a, b in re.findall(r"(\d+)-(\d+)", re.sub(r"\(\d+\)", "", score or ""))]


def load_history(db: sqlite3.Connection) -> dict:
    """Singles results by WTA id, newest first. Walkovers are not matches; a retirement counts for
    the result but not for games, as it was cut short."""
    players = defaultdict(list)
    for winner, loser, score, status, played, surface, event, winner_name, loser_name, winner_price, loser_price, handicap, total_line in db.execute(
            """SELECT m.winner_id, m.loser_id, m.score, m.status, COALESCE(m.match_date, t.start_date), t.surface, t.name,
                      w.full_name, l.full_name, x.winner_price, x.loser_price, x.handicap, x.total
               FROM wta_match m JOIN wta_tournament t ON t.group_id = m.group_id AND t.year = m.year
               LEFT JOIN wta_player w ON w.player_id = m.winner_id LEFT JOIN wta_player l ON l.player_id = m.loser_id
               LEFT JOIN wta_match_line x ON x.match_key = m.match_key
               WHERE m.discipline = 'S' AND m.status IN ('completed', 'retired')
                 AND m.winner_id IS NOT NULL AND m.loser_id IS NOT NULL"""):
        sets = games_of(score)
        base = {"date": played, "surface": (surface or "").lower(), "full": status == "completed",
                "total": sum(a + b for a, b in sets), "score": score, "tournament": event, "status": status, "total_line": total_line}
        margin = sum(a - b for a, b in sets)
        players[winner].append({**base, "won": True, "margin": margin, "opp": loser, "own_name": winner_name, "opp_name": loser_name,
                                "price": winner_price, "opp_price": loser_price, "own_handicap": handicap})
        players[loser].append({**base, "won": False, "margin": -margin, "opp": winner, "own_name": loser_name, "opp_name": winner_name,
                               "price": loser_price, "opp_price": winner_price, "own_handicap": None if handicap is None else -handicap})
    for rows in players.values():
        rows.sort(key=lambda r: r["date"] or "", reverse=True)
    return players


def load_atp_history(db: sqlite3.Connection, tour: str = "ATP") -> dict:
    """The men's tour from tennis-data until it has a collector of its own: tour-level main draws,
    closing prices on the winner, no handicap or total of the match. Same row shape as load_history."""
    from wta_lines import td_price  # noqa: PLC0415 - wta_lines imports this module
    players = defaultdict(list)
    for winner, loser, played, surface, event, sets_json, comment, odds in db.execute(
            """SELECT winner, loser, match_date, surface, tournament, sets_json, comment, odds_json FROM td_match
               WHERE tour = ? AND comment IN ('Completed', 'Retired') AND match_date IS NOT NULL""", (tour,)):
        sets = json.loads(sets_json) if sets_json else []
        margin = sum(a - b for a, b in sets)
        full = comment == "Completed"
        price = td_price(odds) or {}
        base = {"date": played, "surface": (surface or "").lower(), "full": full, "total": sum(a + b for a, b in sets),
                "score": "  ".join(f"{a}-{b}" for a, b in sets), "tournament": event, "status": "completed" if full else "retired",
                "total_line": None, "own_handicap": None}
        key_w, key_l = td_key(winner), td_key(loser)
        players[key_w].append({**base, "won": True, "margin": margin, "opp": key_l, "own_name": winner, "opp_name": loser,
                               "price": price.get("winner_price"), "opp_price": price.get("loser_price")})
        players[key_l].append({**base, "won": False, "margin": -margin, "opp": key_w, "own_name": loser, "opp_name": winner,
                               "price": price.get("loser_price"), "opp_price": price.get("winner_price")})
    for rows in players.values():
        rows.sort(key=lambda r: r["date"] or "", reverse=True)
    return players


def recent(rows: list[dict], cutoff: str, surface=None, months=SURFACE_MONTHS, limit=SURFACE_LIMIT) -> list[dict]:
    since = (date.fromisoformat(cutoff) - timedelta(days=30 * months)).isoformat()
    found = [r for r in rows if r["date"] and since <= r["date"] < cutoff and (not surface or r["surface"] == surface)]
    return found[:limit]


def td_key(name: str) -> str:
    """A tennis-data name as a player's key: there are no ids in those files."""
    words = name.replace(",", " ").split()
    initials = [w for w in words if w.endswith(".")]
    return f"{folded(' '.join(w for w in words if not w.endswith('.')))}|{folded(initials[0])[:1] if initials else ''}"


def atp_key(latin: str | None) -> str | None:
    words = (latin or "").split()
    return f"{folded(' '.join(words[1:]))}|{folded(words[0])[:1]}" if len(words) >= 2 else None


def player_key(tour: str, wta_id, latin):
    if tour == "WTA":
        return int(wta_id) if wta_id else None
    return atp_key(latin)


def player_index(db: sqlite3.Connection, tour: str = "WTA") -> dict:
    """(surname, initial) -> player keys, from BOLSHE's Russian names. Winline writes 'Саснович А.'."""
    index = defaultdict(set)
    for name, wta_id, latin in db.execute("SELECT name_ru, wta_id, name_latin FROM player WHERE name_ru IS NOT NULL AND tour = ?", (tour,)):
        words = name.split()
        key = player_key(tour, wta_id, latin)
        if len(words) >= 2 and key:
            index[(folded(" ".join(words[1:])), folded(words[0])[:1])].add(key)
    return index


def find_wta_id(team: str | None, index: dict) -> int | None:
    """Only a single candidate counts: a wrong player on air is worse than no plate."""
    words = (team or "").split()
    initials = [w for w in words if w.endswith(".")]
    surname = folded(" ".join(w for w in words if not w.endswith(".")))
    if not surname or not initials:
        return None
    found = index.get((surname, folded(initials[0])[:1]), set())
    return next(iter(found)) if len(found) == 1 else None


def learned_players(db: sqlite3.Connection, names: dict, tour: str = "WTA") -> dict:
    """Winline player id -> WTA id, from matches BOLSHE has already tied to Winline. Its two players
    may be in the other order, which the prices tell, or a name when the prices are level. An id
    that ever points at two players is dropped."""
    learned = defaultdict(set)
    for team1, team2, name1, name2, odd1, odd2, pre1, pre2, wta1, wta2, latin1, latin2 in db.execute(
            """SELECT e.team1_id, e.team2_id, e.team1, e.team2, l.odd1, l.odd2, o.pre1, o.pre2, a.wta_id, b.wta_id, a.name_latin, b.name_latin
               FROM winline_event e
               JOIN match_odds o ON o.wl_id = e.wl_id JOIN match m ON m.match_id = o.match_id
               JOIN player a ON a.fs_id = m.p1_fs_id JOIN player b ON b.fs_id = m.p2_fs_id
               JOIN winline_line l ON l.wl_id = e.wl_id AND l.market = 'Победитель'
               WHERE a.tour = ? AND o.pre1 IS NOT NULL AND e.is_doubles = 0
               GROUP BY e.wl_id""", (tour,)):
        id1, id2 = player_key(tour, wta1, latin1), player_key(tour, wta2, latin2)
        if not id1 or not id2:
            continue
        straight = abs(odd1 - pre1) + abs(odd2 - pre2)
        crossed = abs(odd1 - pre2) + abs(odd2 - pre1)
        if straight == crossed or pre1 == pre2:
            known = find_wta_id(name1, names) or find_wta_id(name2, names)
            if known not in (id1, id2):
                continue
            first = known if find_wta_id(name1, names) == known else (id1 if known == id2 else id2)
        else:
            first = id1 if straight < crossed else id2
        second = id2 if first == id1 else id1
        if team1 and team2:
            learned[team1].add(first)
            learned[team2].add(second)
    return {wl: next(iter(ids)) for wl, ids in learned.items() if len(ids) == 1}


def surface_index(db: sqlite3.Connection, tour: str = "WTA") -> dict:
    """City -> surface, newest event winning, from BOLSHE's directory. Winline's feed has no surface."""
    index = {}
    for city, surface in db.execute(
            "SELECT tournament, surface FROM tournament WHERE tour = ? AND surface IS NOT NULL AND tournament IS NOT NULL ORDER BY first_seen", (tour,)):
        index[folded(city)] = surface.lower()
    return index


def surface_of(tournament: str | None, index: dict) -> str | None:
    return index.get(folded((tournament or "").split(",")[0]))


def record(rows: list[dict]) -> tuple[int, int]:
    wins = sum(r["won"] for r in rows)
    return wins, len(rows) - wins


def priced_record(rows: list[dict]) -> dict | None:
    """Results read against the price of the same match: as favourite and as underdog."""
    priced = [r for r in rows if r.get("price") and r.get("opp_price") and r["price"] != r["opp_price"]]
    if not priced:
        return None
    favourite = [r for r in priced if r["price"] < r["opp_price"]]
    underdog = [r for r in priced if r["price"] > r["opp_price"]]
    return {"n": len(priced), "favourite": record(favourite), "underdog": record(underdog)}


def priced_words(found: dict) -> str:
    return f"фаворитом {found['favourite'][0]}-{found['favourite'][1]}, андердогом {found['underdog'][0]}-{found['underdog'][1]}"


def fact_cards(event: dict, hist: dict, form: dict, h2h: list[dict], surface: str | None) -> list[dict]:
    """What can be said whatever the line. No gate beyond a minimum of matches."""
    cards = []
    names = {1: event["team1"], 2: event["team2"]}
    for side in (1, 2):
        if len(form[side]) >= 5:
            wins, losses = record(form[side])
            cards.append({"kind": "form", "side": side, "hits": wins, "n": wins + losses,
                          "what": f"Победы и поражения за {FORM_MONTHS} месяцев, на всех покрытиях.",
                          "text": f"{names[side]}: {wins}-{losses} в последних {wins + losses} матчах"})
        if surface and len(hist[side]) >= MIN_SAMPLE:
            wins, losses = record(hist[side])
            cards.append({"kind": "surface", "side": side, "hits": wins, "n": wins + losses,
                          "what": f"Победы и поражения только на этом покрытии, не больше {SURFACE_LIMIT} матчей за {SURFACE_MONTHS // 12} года.",
                          "text": f"{names[side]}: {wins}-{losses} в последних {wins + losses} матчах на {SURFACE_RU.get(surface, surface)}"})
        found = priced_record(hist[side])
        if found and found["n"] >= MIN_PRICED:
            cards.append({"kind": "priced", "side": side, "hits": found["favourite"][0] + found["underdog"][0], "n": found["n"],
                          "what": f"По цене каждого из этих матчей: кто был фаворитом у букмекеров тогда. Цены известны для {found['n']} матчей.",
                          "text": f"{names[side]}: {priced_words(found)}"})
        done = [r for r in hist[side] if r.get("full")]
        capped = [r for r in done if r.get("own_handicap") is not None and r["margin"] + r["own_handicap"] != 0]
        if len(capped) >= MIN_PRICED:
            hits = sum(r["margin"] + r["own_handicap"] > 0 for r in capped)
            cards.append({"kind": "own_handicap", "side": side, "hits": hits, "n": len(capped),
                          "what": "Фора бралась та, что стояла на тот самый матч, а не сегодняшняя.",
                          "text": f"{names[side]}: своя фора сыграла в {hits} из {len(capped)}"})
        totalled = [r for r in done if r.get("total_line") is not None and r["total"] != r["total_line"]]
        if len(totalled) >= MIN_PRICED:
            over = sum(r["total"] > r["total_line"] for r in totalled)
            cards.append({"kind": "own_total", "side": side, "hits": over, "n": len(totalled),
                          "what": "Тотал брался тот, что стоял на тот самый матч, а не сегодняшний.",
                          "text": f"{names[side]}: свой тотал, больше {over}, меньше {len(totalled) - over}"})
    if h2h:
        first, second = record(h2h)
        if first == second:
            text = f"Личные встречи: {first}-{second}"
        else:
            leader = names[1] if first > second else names[2]
            text = f"Личные встречи: {leader} ведёт {max(first, second)}-{min(first, second)}"
        cards.append({"kind": "h2h", "side": None, "hits": first, "n": first + second, "text": text})
    return cards


def priced(side_price: float, other_price: float, hits: int, n: int, min_n: int = MIN_SAMPLE, min_hit: float = MIN_HIT) -> dict | None:
    """The gate: a real price in range, enough matches, and a record above what the price expects."""
    if not (MIN_ODDS <= side_price <= MAX_ODDS) or n < min_n or hits / n < min_hit:
        return None
    fair, low = fair_price(side_price, other_price), wilson_low(hits, n)
    if low <= fair:
        return None
    return {"price": side_price, "market_expects": round(fair, 3), "hits": hits, "n": n,
            "low": round(low, 3), "score": round(low - fair, 3)}


def scope_words(surface: str | None) -> str:
    place = f"на {SURFACE_RU.get(surface, surface)} " if surface else ""
    return f"{place}за {SURFACE_MONTHS // 12} года"


def reason_words(gate: dict) -> str:
    """Why this line was picked, in the words an editor can check against the numbers."""
    return (f"Цена {gate['price']} значит, что рынок ждёт такого исхода примерно в {round(gate['market_expects'] * 100)} % случаев "
            f"(маржа букмекера убрана). По этой истории даже осторожная оценка даёт {round(gate['low'] * 100)} %, "
            "это выше, поэтому строка попала в отбор. Это описание прошлого, а не прогноз: соперники в этих матчах были другие.")


def evidence_row(row: dict, player: str, hit: bool, note: str) -> dict:
    return {"date": row["date"], "player": player, "opponent": row.get("opp_name"), "score": row.get("score"),
            "tournament": row.get("tournament"), "won": row["won"], "hit": hit, "note": note,
            "price": row.get("price"), "opp_price": row.get("opp_price")}


def same_role(rows: list[dict], favourite: bool) -> list[dict]:
    """Past matches with a known price where the player was in the role she or he has today."""
    return [r for r in rows if r.get("price") and r.get("opp_price") and r["price"] != r["opp_price"]
            and (r["price"] < r["opp_price"]) == favourite]


def winner_prices(lines: list[dict]) -> dict:
    for line in lines:
        if line["market"] == "Победитель":
            return {1: line["odd1"], 2: line["odd2"]}
    return {}


def run_of(rows: list[dict]) -> tuple[int, bool | None]:
    """The current run: how many results in a row, newest first, and whether they are wins."""
    if not rows:
        return 0, None
    won = rows[0]["won"]
    count = 0
    for row in rows:
        if row["won"] != won:
            break
        count += 1
    return count, won


def elo_chance(mine: float, other: float) -> float:
    return 1 / (1 + 10 ** ((other - mine) / 400))


def elo_of(info: dict | None, surface: str | None):
    if not info:
        return None
    return info.get(f"elo_{surface}") or info.get("elo")


def candidates(event: dict, prices: dict, form: dict, hist: dict, pool: dict, h2h: list[dict], info: dict, surface: str | None) -> list[dict]:
    """Everything worth saying, each with how interesting it is. The score ranks stories for an editor
    and says nothing about a bet: on 25,000 matches a record above the market lost more than average."""
    names = {1: event["team1"], 2: event["team2"]}
    fair = {1: fair_price(prices[1], prices[2]), 2: fair_price(prices[2], prices[1])} if prices else {}
    found = []

    def add(score, kind, side, text, what, rows=None, hit=None):
        card = {"kind": "plate", "family": kind, "score": score, "side": side, "text": text, "what": what}
        if rows:
            hits = [hit(r) for r in rows]
            card.update(hits=sum(hits), n=len(rows), matches=[
                evidence_row(r, names[side], h, "победа" if r["won"] else "поражение") for r, h in zip(rows, hits)])
        found.append(card)

    won = lambda r: r["won"]  # noqa: E731
    for side in (1, 2):
        name = names[side]
        count, wins = run_of(form[side])
        if count >= 3:
            word = "побед" if wins else "поражений"
            add(min(60 + 5 * count, 95), "run", side, f"{name}: {word} подряд — {count}", "Подряд, считая самый свежий матч в базе.",
                form[side][:count], (won if wins else lambda r: not r["won"]))
        played = form[side]
        if len(played) >= MIN_PRICED:
            hits = sum(r["won"] for r in played)
            rate = hits / len(played)
            add(70 if rate >= 0.8 or rate <= 0.2 else 30, "form", side, f"{name}: {hits}-{len(played) - hits} в последних {len(played)} матчах",
                f"Последние матчи за {FORM_MONTHS} месяцев, на всех покрытиях.", played, won)
        played = hist[side]
        if surface and len(played) >= MIN_PRICED:
            hits = sum(r["won"] for r in played)
            rate = hits / len(played)
            add(65 if rate >= 0.75 or rate <= 0.3 else 25, "surface", side,
                f"{name}: {hits}-{len(played) - hits} на {SURFACE_RU.get(surface, surface)}", f"Последние матчи на этом покрытии, {scope_words(surface)}.", played, won)
        if prices and prices[1] != prices[2]:
            favourite = prices[side] < prices[3 - side]
            played = same_role(pool[side], favourite)
            if len(played) >= MIN_PRICED:
                hits = sum(r["won"] for r in played)
                role = "фаворит" if favourite else "андердог"
                add(75 if abs(hits / len(played) - fair[side]) >= 0.15 else 35, "role", side,
                    f"{name}: {'фаворитом' if favourite else 'андердогом'} {hits}-{len(played) - hits}",
                    f"Сегодня {role}. Прошлые матчи в той же роли, по цене каждого из них, {scope_words(surface)}.", played, won)
    if h2h:
        first, second = record(h2h)
        if first == second:
            add(40, "h2h", None, f"Личные встречи: {first}-{second}", "Все встречи в базе.")
        else:
            leader = 1 if first > second else 2
            total = first + second
            add(80 if total >= 3 and min(first, second) == 0 else 55 if total >= 2 else 35, "h2h", leader,
                f"Личные встречи: {names[leader]} ведёт {max(first, second)}-{min(first, second)}", "Все встречи в базе.")
    rank = {side: (info.get(side) or {}).get("rank") for side in (1, 2)}
    if rank[1] and rank[2]:
        better = 1 if rank[1] < rank[2] else 2
        surprise = bool(prices) and prices[better] > prices[3 - better] and abs(rank[1] - rank[2]) >= 10
        add(70 if surprise else 30, "rank", better, f"Рейтинг: {names[1]} №{rank[1]}, {names[2]} №{rank[2]}",
            "Выше в рейтинге, но у букмекеров андердог." if surprise else "Текущий рейтинг из копии BOLSHE.")
    elo = {side: elo_of(info.get(side), surface) for side in (1, 2)}
    if elo[1] and elo[2]:
        chance = elo_chance(elo[1], elo[2])
        top = 1 if chance >= 0.5 else 2
        mine = chance if top == 1 else 1 - chance
        tail = f", рынок {round(fair[top] * 100)} %" if fair else ""
        add(70 if fair and abs(mine - fair[top]) >= 0.10 else 25, "elo", top, f"По Эло {names[top]} {round(mine * 100)} %{tail}",
            "Шанс на победу по рейтингу Эло из копии BOLSHE, с учётом покрытия, если он известен.")
    if prices:
        if prices[1] == prices[2]:
            add(10, "market", None, f"Рынок поровну: {prices[1]} и {prices[2]}", "Букмекеры не выделяют фаворита.")
        else:
            top = 1 if prices[1] < prices[2] else 2
            add(10, "market", top, f"Фаворит у букмекеров: {names[top]}, цена {prices[top]}", "Цена Winline на победителя матча.")
    return found


def select_plates(event: dict, prices: dict, found: list[dict]) -> list[dict]:
    """The most interesting stories, each next to the price of the player it is about. A price is a
    fact beside the story, not advice."""
    plates = sorted(found, key=lambda c: -c["score"])[:MAX_PLATES]
    names = {1: event["team1"], 2: event["team2"]}
    for card in plates:
        side = card["side"]
        if prices and side:
            card["price_text"] = f"Победа {names[side]} — {prices[side]}"
            card["why"] = f"Цена {prices[side]}: рынок ждёт победы примерно в {round(fair_price(prices[side], prices[3 - side]) * 100)} % случаев, маржа букмекера убрана."
    return plates


def resolve(side_id, team, ids: dict, learned: dict):
    return learned.get(side_id) or find_wta_id(team, ids)


def name_words(name: str | None) -> frozenset:
    """A full name as a set of words, so 'Shelton, Ben' and 'Ben Shelton' are one name."""
    return frozenset(folded(word) for word in re.split(r"[\s,/-]+", name or "") if folded(word))


def by_english(name: str | None, english: dict | None, players: dict):
    """A Latin name to a player. A whole name and a single candidate only: a wrong player on air is
    worse than no plate. Men have no ids in tennis-data, so their key is made from the name."""
    words = name_words(name)
    if len(words) < 2:
        return None
    if english:
        return english.get(words)
    plain = " ".join(reversed([part.strip() for part in name.split(",")])) if "," in name else name
    key = atp_key(plain)
    return key if key in players else None


def english_index(pairs) -> dict:
    """(name, key) to words -> key; a name two players share links nobody."""
    index, clash = {}, set()
    for name, key in pairs:
        words = name_words(name)
        if len(words) < 2 or not key:
            continue
        if words in index and index[words] != key:
            clash.add(words)
        index[words] = key
    return {words: key for words, key in index.items() if words not in clash}


def build(event: dict, lines: list[dict], players: dict, player_ids: dict, surfaces: dict, learned: dict | None = None, bolshe=None, english: dict | None = None) -> dict:
    cutoff = (event["start_at"] or "")[:10]
    learned = learned or {}
    id1 = resolve(event.get("team1_id"), event["team1"], player_ids, learned) or by_english(event.get("team1_en"), english, players)
    id2 = resolve(event.get("team2_id"), event["team2"], player_ids, learned) or by_english(event.get("team2_en"), english, players)
    prices = winner_prices(lines)
    fs1, fs2 = bolshe.link(event, prices) if bolshe else (None, None)
    info = {1: bolshe.info.get(fs1) if bolshe else None, 2: bolshe.info.get(fs2) if bolshe else None}
    result = {"wl_id": event["wl_id"], "tournament": event["tournament"], "start_at": event["start_at"],
              "team1": event["team1"], "team2": event["team2"], "url": event["url"], "linked": bool(id1 and id2),
              "facts": [], "lines": [], "notes": [], "form_rows": {1: [], 2: []}, "h2h_rows": [], "history": "tour"}
    if not (id1 and id2) and fs1 and fs2:
        id1, id2, players = fs1, fs2, bolshe.players
        result.update(linked=True, history="bolshe")
        result["notes"].append("История только за последний месяц: для этого турнира нет своей базы")
    surface = surface_of(event["tournament"], surfaces) or (bolshe.surface(event["wl_id"]) if bolshe else None)
    if not (id1 and id2 and cutoff):
        result["notes"].append("нет однозначной связи с игроками в базе")
        empty = {1: [], 2: []}
        result["lines"] = select_plates(event, prices, candidates(event, prices, empty, empty, empty, [], info, surface))
        return result
    if not surface:
        result["notes"].append("покрытие турнира неизвестно: нужен свежий справочник BOLSHE")
    rows = {1: players.get(id1, []), 2: players.get(id2, [])}
    hist = {side: recent(rows[side], cutoff, surface) if surface else [] for side in (1, 2)}
    form = {side: recent(rows[side], cutoff, None, FORM_MONTHS, FORM_LIMIT) for side in (1, 2)}
    h2h = [r for r in rows[1] if r["opp"] == id2 and r["date"] and r["date"] < cutoff]
    pool = {side: recent(rows[side], cutoff, surface, limit=ROLE_LIMIT) if surface else [] for side in (1, 2)}
    result["facts"] = fact_cards(event, hist, form, h2h, surface)
    result["lines"] = select_plates(event, prices, candidates(event, prices, form, hist, pool, h2h, info, surface))
    result["surface"] = surface
    result["form_rows"] = {side: recent(rows[side], cutoff, None, 120, RESULTS_LIMIT) for side in (1, 2)}
    result["h2h_rows"] = h2h[:30]
    return result


def circuit_tour(circuit: str | None) -> str:
    """Winline's circuit names to a tour: 'World Tennis, Мужчины', 'Челленджер (М)', 'UTR (Ж)'..."""
    text = circuit or ""
    return "ATP" if text == "ATP" or "Мужч" in text or "(М)" in text else "WTA"


@dataclass
class Bolshe:
    """The fallback for every event without a base of its own: BOLSHE's copy has a month of matches
    at all levels, and rankings and Elo for players of both tours."""

    players: dict
    info: dict
    ids: dict
    wl: dict
    english: dict = field(default_factory=dict)

    def surface(self, wl_id: str) -> str | None:
        found = self.wl.get(str(wl_id))
        return found[4] if found else None

    def link(self, event: dict, prices: dict) -> tuple:
        ids = self.ids.get(circuit_tour(event.get("circuit")), {})
        english = self.english.get(circuit_tour(event.get("circuit")), {})
        first = find_wta_id(event["team1"], ids) or english.get(name_words(event.get("team1_en")))
        second = find_wta_id(event["team2"], ids) or english.get(name_words(event.get("team2_en")))
        found = self.wl.get(str(event["wl_id"]))
        if not found:
            return first, second
        p1, p2, pre1, pre2, _ = found
        if first in (p1, p2):
            return first, p2 if first == p1 else p1
        if second in (p1, p2):
            return (p2 if second == p1 else p1), second
        if prices and pre1 and pre2 and pre1 != pre2:
            straight = abs(prices[1] - pre1) + abs(prices[2] - pre2)
            crossed = abs(prices[1] - pre2) + abs(prices[2] - pre1)
            return (p1, p2) if straight <= crossed else (p2, p1)
        return None, None


def load_bolshe(db: sqlite3.Connection) -> Bolshe:
    players = defaultdict(list)
    wl = {}
    for p1, p2, name1, name2, day, status, winner, sets_json, event, surface, wl_id, pre1, pre2, hcap in db.execute(
            """SELECT m.p1_fs_id, m.p2_fs_id, m.p1_name, m.p2_name, m.date_msk, m.status, m.winner, m.sets_json, t.tournament, t.surface,
                      o.wl_id, o.pre1, o.pre2, o.hcap
               FROM match m LEFT JOIN tournament t ON t.tournament_key = m.tournament_key LEFT JOIN match_odds o ON o.match_id = m.match_id
               WHERE m.is_doubles = 0 AND m.p1_fs_id IS NOT NULL AND m.p2_fs_id IS NOT NULL"""):
        if wl_id:
            wl[str(wl_id)] = (p1, p2, pre1, pre2, (surface or "").lower() or None)
        if winner not in (1, 2) or not day or status not in ("finished", "retired"):
            continue
        sets = [(s.get("p1") or 0, s.get("p2") or 0) for s in json.loads(sets_json)] if sets_json else []
        margin = sum(a - b for a, b in sets) * (1 if winner == 1 else -1)
        full = status == "finished"
        ordered = sets if winner == 1 else [(b, a) for a, b in sets]
        base = {"date": day, "surface": (surface or "").lower(), "full": full, "total": sum(a + b for a, b in sets), "tournament": event,
                "score": "  ".join(f"{a}-{b}" for a, b in ordered), "status": "completed" if full else "retired", "total_line": None}
        first_won = winner == 1
        players[p1].append({**base, "won": first_won, "margin": margin if first_won else -margin, "opp": p2, "own_name": name1, "opp_name": name2,
                            "price": pre1, "opp_price": pre2, "own_handicap": hcap})
        players[p2].append({**base, "won": not first_won, "margin": -margin if first_won else margin, "opp": p1, "own_name": name2, "opp_name": name1,
                            "price": pre2, "opp_price": pre1, "own_handicap": None if hcap is None else -hcap})
    for rows in players.values():
        rows.sort(key=lambda r: r["date"] or "", reverse=True)
    info, ids = {}, {"WTA": defaultdict(set), "ATP": defaultdict(set)}
    for fs, tour, name, rank, elo, hard, clay, grass in db.execute(
            "SELECT fs_id, tour, name_ru, singles_rank, elo, elo_hard, elo_clay, elo_grass FROM player WHERE fs_id IS NOT NULL"):
        info[fs] = {"rank": rank, "elo": elo, "elo_hard": hard, "elo_clay": clay, "elo_grass": grass}
        words = (name or "").split()
        if len(words) >= 2 and tour in ids:
            ids[tour][(folded(" ".join(words[1:])), folded(words[0])[:1])].add(fs)
    english = {tour: english_index(db.execute("SELECT name_latin, fs_id FROM player WHERE tour = ? AND fs_id IS NOT NULL", (tour,)))
               for tour in ("WTA", "ATP")}
    return Bolshe(players, info, ids, wl, english)


@dataclass
class Tour:
    """What the same engine needs for one tour; only where the history comes from differs."""

    players: dict
    ids: dict
    surfaces: dict
    learned: dict
    english: dict = field(default_factory=dict)


def load_tour(db: sqlite3.Connection, tour: str) -> Tour:
    ids = player_index(db, tour)
    players = load_history(db) if tour == "WTA" else load_atp_history(db)
    # Women have ids behind their full names; the men's key comes from the name itself.
    english = english_index(db.execute("SELECT full_name, player_id FROM wta_player")) if tour == "WTA" else {}
    return Tour(players, ids, surface_index(db, tour), learned_players(db, ids, tour), english)


def upcoming(db: sqlite3.Connection, day: str | None, player: str | None) -> list[dict]:
    """Singles of both tours still to start, from the last prices seen."""
    db.row_factory = sqlite3.Row
    last = db.execute("SELECT MAX(taken_at) FROM winline_fetch").fetchone()[0]
    events = [dict(r) for r in db.execute(
        """SELECT * FROM winline_event WHERE is_doubles = 0 AND last_seen = ? ORDER BY start_at""", (last,))]
    lines = defaultdict(list)
    for row in db.execute("SELECT * FROM winline_current"):
        lines[row["wl_id"]].append(dict(row))
    db.row_factory = None
    if day:
        events = [e for e in events if (e["start_at"] or "").startswith(day)]
    if player:
        wanted = folded(player)
        events = [e for e in events if wanted in folded(e["team1"] or "") or wanted in folded(e["team2"] or "")]
    return [{**e, "lines": lines.get(e["wl_id"], [])} for e in events]


def coverage_note(db: sqlite3.Connection) -> str:
    pages = db.execute("SELECT COUNT(*) FROM wta_parsed WHERE kind = 'player_matches'").fetchone()[0]
    if pages:
        return ""
    return "История пока только из списков турниров: тур и WTA 125 с 2016 года, без ITF и Больших шлемов до 2025."


def show(results: list[dict]) -> None:
    for r in results:
        print(f"\n{(r['start_at'] or '')[:16].replace('T', ' ')} | {r['tournament']}")
        print(f"  {r['team1']} — {r['team2']}")
        for note in r["notes"]:
            print(f"  ! {note}")
        for card in r["facts"]:
            print(f"  факт: {card['text']}")
        for card in r["lines"]:
            print(f"  плашка: {card['text']} | {card.get('price_text', '')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--date", help="YYYY-MM-DD of the match")
    parser.add_argument("--player", help="part of a player's name as Winline writes it")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    db = sqlite3.connect(f"{args.db.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    tours = {tour: load_tour(db, tour) for tour in TOURS}
    bolshe = load_bolshe(db)
    results = []
    for e in upcoming(db, args.date, args.player):
        tour = tours.get(e["circuit"]) or Tour({}, {}, {}, {})
        results.append(build(e, e["lines"], tour.players, tour.ids, tour.surfaces, tour.learned, bolshe, tour.english))
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return 0
    note = coverage_note(db)
    if note:
        print(note)
    linked = sum(r["linked"] for r in results)
    print(f"Одиночных матчей в линии: {len(results)}, связаны с игроками: {linked}, с линией для плашки: {sum(bool(r['lines']) for r in results)}")
    show(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
