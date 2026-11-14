#!/usr/bin/env python3
"""Matchday: the tennis match page with Winline's line and what goes with it, served on the shared
server at /matchday of the «Больше» address. Read-only over tennis.sqlite, which the snapshot, collector
and parse units keep fresh; it changes nothing and holds no accounts.

The shape follows the EPL operator site: matches by day, /matchday/#matches/<Winline id>, tabs for
plates and markets. A plate is a fact next to a real price, never a claim that a bet is good.
Everything but the login page needs an account (matchday_accounts.py): e-mail and password.

    python tools/tennis/matchday_server.py --port 8107
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import matchday_accounts as accounts_module  # noqa: E402
from matchday_accounts import AccountError  # noqa: E402
import tennis_plates  # noqa: E402

DEFAULT_DB = HERE.parents[1] / "local-data" / "tennis" / "tennis.sqlite"
SITE = HERE / "matchday_site"
PREFIX = "/matchday"
STATIC = {"app.js": "text/javascript; charset=utf-8", "styles.css": "text/css; charset=utf-8",
          "index.html": "text/html; charset=utf-8", "login.js": "text/javascript; charset=utf-8"}
OPEN_FILES = {"styles.css", "login.js"}
COOKIE = "md_session"
MAX_TRIES = 20  # per address; a second, wider limit than the one per account
TRIES_WINDOW = 900
MAX_BODY = 16 * 1024
MOVE = 0.03  # a price counts as moved at 3 %, as on the EPL site
RECENT_DAYS = 2
HISTORY_TTL = 600
PLATE_NOTE = "Плашки пока только для одиночного разряда"


class LoginLimiter:
    """Wrong passwords per address; memory only, so a restart forgets them."""

    def __init__(self, limit: int = MAX_TRIES, window: float = TRIES_WINDOW):
        self.limit, self.window = limit, window
        self.lock = threading.Lock()
        self.failures: dict[str, list[float]] = {}

    def _recent(self, address: str, now: float) -> list[float]:
        recent = [t for t in self.failures.get(address, []) if now - t < self.window]
        if recent:
            self.failures[address] = recent
        else:
            self.failures.pop(address, None)
        return recent

    def locked(self, address: str, now: float | None = None) -> bool:
        with self.lock:
            return len(self._recent(address, now if now is not None else time.monotonic())) >= self.limit

    def failed(self, address: str, now: float | None = None) -> None:
        with self.lock:
            moment = now if now is not None else time.monotonic()
            self.failures[address] = [*self._recent(address, moment), moment]

    def clear(self, address: str) -> None:
        with self.lock:
            self.failures.pop(address, None)


def open_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


class History:
    """Results and player links, reloaded at most every 10 minutes: the parse unit runs hourly."""

    def __init__(self, path: Path, ttl: float = HISTORY_TTL):
        self.path, self.ttl = path, ttl
        self.lock = threading.Lock()
        self.loaded: dict[str, tuple[float, tennis_plates.Tour]] = {}

    def get(self, tour: str):
        with self.lock:
            loaded_at, found = self.loaded.get(tour, (None, None))
            if found is None or time.monotonic() - loaded_at >= self.ttl:
                db = open_db(self.path)
                db.row_factory = None
                found = tennis_plates.load_bolshe(db) if tour == "BOLSHE" else tennis_plates.load_tour(db, tour)
                db.close()
                self.loaded[tour] = (time.monotonic(), found)
            return found


def family_of(market: str) -> tuple[int, str]:
    """(place in the page, family name). Set markets come after the whole-match ones."""
    if market == "Победитель":
        return 0, "Исход матча"
    if market == "Фора":
        return 1, "Фора по геймам"
    if market == "Тотал":
        return 2, "Тотал геймов"
    found = re.match(r"(\d+) сет (Исход|Фора|Тотал)", market)
    if found:
        kind = {"Исход": "исход", "Фора": "фора", "Тотал": "тотал"}[found.group(2)]
        return 10 + int(found.group(1)), f"{found.group(1)} сет: {kind}"
    return 90, market


def moved(now: float, first: float) -> bool:
    return bool(first) and abs(now / first - 1) >= MOVE


def group_markets(rows: list[dict], team1: str | None, team2: str | None) -> list[dict]:
    """Rows of winline_line, oldest first, into families of rungs with their price history."""
    rungs: dict[tuple, dict] = {}
    for row in rows:
        key = (row["market"], row["value_key"])
        rung = rungs.setdefault(key, {"market": row["market"], "value": row["value"], "history": []})
        rung["history"].append({"at": row["taken_at"], "odd1": row["odd1"], "odd2": row["odd2"]})
    families: dict[tuple, dict] = {}
    for rung in rungs.values():
        first, last = rung["history"][0], rung["history"][-1]
        rung.update(odd1=last["odd1"], odd2=last["odd2"], was1=first["odd1"] if moved(last["odd1"], first["odd1"]) else None,
                    was2=first["odd2"] if moved(last["odd2"], first["odd2"]) else None)
        order, name = family_of(rung["market"])
        total = "Тотал" in rung["market"]
        family = families.setdefault((order, name), {"name": name, "side1": "Больше" if total else team1,
                                                     "side2": "Меньше" if total else team2, "rungs": []})
        family["rungs"].append(rung)
    for family in families.values():
        family["rungs"].sort(key=lambda r: (r["value"] is not None, r["value"] or 0))
    return [families[key] for key in sorted(families)]


def latest_lines(db: sqlite3.Connection, wl_id: str) -> list[dict]:
    # MAX with bare columns gives the columns of the row holding the maximum, in SQLite.
    return [dict(r) for r in db.execute(
        """SELECT market, value, odd1, odd2, MAX(taken_at) AS taken_at FROM winline_line
           WHERE wl_id = ? GROUP BY market, value_key""", (wl_id,))]


def last_fetch(db: sqlite3.Connection) -> dict:
    row = db.execute("SELECT taken_at, events, lines, changed FROM winline_fetch ORDER BY taken_at DESC LIMIT 1").fetchone()
    return dict(row) if row else {}


def plates_for(event: dict, lines: list[dict], history: History) -> dict | None:
    if event["is_doubles"]:
        return None
    tour = history.get(event["circuit"]) if event["circuit"] in tennis_plates.TOURS else tennis_plates.Tour({}, {}, {}, {})
    return tennis_plates.build(event, lines, tour.players, tour.ids, tour.surfaces, tour.learned, history.get("BOLSHE"), tour.english)


def list_matches(db: sqlite3.Connection, history: History, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    fetch = last_fetch(db)
    if not fetch:
        return {"fetch": {}, "matches": []}
    since = (now - timedelta(days=RECENT_DAYS)).isoformat(timespec="seconds")
    events = [dict(r) for r in db.execute(
        "SELECT * FROM winline_event WHERE last_seen = ? OR last_seen >= ? ORDER BY start_at", (fetch["taken_at"], since))]
    ids = json.dumps([e["wl_id"] for e in events])
    winners = {r["wl_id"]: r for r in db.execute(
        """SELECT wl_id, odd1, odd2, MAX(taken_at) FROM winline_line
           WHERE wl_id IN (SELECT value FROM json_each(?)) AND market = 'Победитель' GROUP BY wl_id""", (ids,))}
    counts = dict(db.execute(
        """SELECT wl_id, COUNT(DISTINCT market || '|' || value_key) FROM winline_line
           WHERE wl_id IN (SELECT value FROM json_each(?)) GROUP BY wl_id""", (ids,)).fetchall())
    matches = []
    for event in events:
        winner = winners.get(event["wl_id"])
        in_line = event["last_seen"] == fetch["taken_at"]
        plates = 0
        if in_line and not event["is_doubles"]:
            built = plates_for(event, latest_lines(db, event["wl_id"]), history)
            plates = len(built["lines"]) if built else 0
        matches.append({
            "id": event["wl_id"], "circuit": event["circuit"], "tournament": event["tournament"], "team1": event["team1"],
            "team2": event["team2"], "start_at": event["start_at"], "is_doubles": bool(event["is_doubles"]), "in_line": in_line,
            "odd1": winner["odd1"] if winner else None, "odd2": winner["odd2"] if winner else None,
            "markets": counts.get(event["wl_id"], 0), "plates": plates,
        })
    return {"fetch": fetch, "matches": matches}


def result_of(row: dict) -> dict:
    """A past match from one player's side, as the page shows it. A match cut short by a retirement
    settles neither handicap nor total, so its games are not given."""
    full = row.get("full")
    own, other = row.get("own_name"), row.get("opp_name")
    return {"played": row["date"], "winner": own if row["won"] else other, "loser": other if row["won"] else own,
            "score": row.get("score"), "status": row.get("status"), "tournament": row.get("tournament"), "surface": row.get("surface"),
            "won": row["won"], "price": row.get("price"), "opp_price": row.get("opp_price"), "own_handicap": row.get("own_handicap"),
            "total_line": row.get("total_line"), "margin": row["margin"] if full else None, "total": row["total"] if full else None}


def match_detail(db: sqlite3.Connection, history: History, wl_id: str) -> dict | None:
    row = db.execute("SELECT * FROM winline_event WHERE wl_id = ?", (wl_id,)).fetchone()
    if row is None:
        return None
    event = dict(row)
    rows = [dict(r) for r in db.execute(
        "SELECT market, value_key, value, odd1, odd2, taken_at FROM winline_line WHERE wl_id = ? ORDER BY taken_at", (wl_id,))]
    built = plates_for(event, latest_lines(db, wl_id), history)
    detail = {
        "id": wl_id, "circuit": event["circuit"], "tournament": event["tournament"], "team1": event["team1"], "team2": event["team2"],
        "start_at": event["start_at"], "url": event["url"], "is_doubles": bool(event["is_doubles"]),
        "first_seen": event["first_seen"], "last_seen": event["last_seen"], "fetch": last_fetch(db),
        "markets": group_markets(rows, event["team1"], event["team2"]),
        "facts": [], "lines": [], "notes": [], "h2h": [], "form": [],
    }
    if built is None:
        detail["notes"].append(PLATE_NOTE)
        return detail
    detail.update(facts=built["facts"], lines=built["lines"], notes=built["notes"])
    if built["linked"]:
        detail["surface"] = built.get("surface")
        detail["h2h"] = [result_of(row) for row in built["h2h_rows"]]
        detail["form"] = [{"name": name, "side": side, "results": [result_of(row) for row in built["form_rows"][side]]}
                          for side, name in ((1, event["team1"]), (2, event["team2"]))]
    return detail


class Handler(BaseHTTPRequestHandler):
    server_version = "matchday"
    sys_version = ""

    def respond(self, status: int, body: bytes, content_type: str, cache: str = "no-store", cookie: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'self'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status: int = 200, cookie: str | None = None) -> None:
        self.respond(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", cookie=cookie)

    def do_GET(self) -> None:  # noqa: N802 - the name http.server asks for
        path = urlsplit(self.path).path
        try:
            self.route(path)
        except sqlite3.OperationalError as error:
            self.log_error("database: %s", error)
            self.send_json({"error": "база занята, попробуйте ещё раз"}, 503)
        except BrokenPipeError:
            pass

    def do_POST(self) -> None:  # noqa: N802 - the name http.server asks for
        path = urlsplit(self.path).path
        try:
            self.post(path)
        except AccountError as error:
            self.send_json({"error": error.message}, error.status)
        except BrokenPipeError:
            pass

    def read_body(self) -> dict | None:
        """POSTs must be JSON: a form on another site cannot send that, so no request can be forged."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        kind = self.headers.get("Content-Type", "").split(";")[0].strip()
        if not 0 <= length <= MAX_BODY or kind != "application/json":
            self.close_connection = True
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def post(self, path: str) -> None:
        if not path.startswith(f"{PREFIX}/api/"):
            self.send_json({"error": "not found"}, 404)
            return
        rest = path[len(PREFIX) + 5:]
        body = self.read_body()
        if body is None:
            self.send_json({"error": "bad request"}, 400)
            return
        accounts = self.server.accounts
        if rest == "login":
            self.login(body)
            return
        if rest == "logout":
            accounts.logout(self.session_token())
            self.send_json({"ok": True}, cookie=f"{COOKIE}=; Path={PREFIX}; Max-Age=0; HttpOnly; Secure; SameSite=Lax")
            return
        user = self.current_user()
        if not user:
            self.send_json({"error": "нужен вход"}, 401)
            return
        if rest == "password":
            accounts.change_password(user["email"], body.get("old"), body.get("new"), self.session_token())
            self.send_json({"ok": True})
            return
        if not rest.startswith("users/"):
            self.send_json({"error": "not found"}, 404)
            return
        if user["role"] != "admin":
            self.send_json({"error": "только для администратора"}, 403)
            return
        if rest == "users/add":
            self.send_json({"ok": True, "user": accounts.invite(body.get("name"), body.get("email"), body.get("role") or "commentator")})
        elif rest == "users/access":
            self.send_json({"ok": True, "user": accounts.set_access(body.get("email"), body.get("active"), user["email"])})
        elif rest == "users/reset":
            accounts.reset(body.get("email"))
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "not found"}, 404)

    def redirect(self, where: str, status: int = 302) -> None:
        self.send_response(status)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def client_address_text(self) -> str:
        # Caddy, the only thing that can reach this port, puts the visitor's address last.
        forwarded = self.headers.get("X-Forwarded-For", "")
        return forwarded.split(",")[-1].strip() or self.client_address[0]

    def session_token(self) -> str | None:
        try:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        except CookieError:
            return None
        return morsel.value if morsel else None

    def current_user(self) -> dict | None:
        return self.server.accounts.session_user(self.session_token())

    def login(self, body: dict) -> None:
        address = self.client_address_text()
        if self.server.limiter.locked(address):
            raise AccountError("locked")
        try:
            user, token = self.server.accounts.login(body.get("email"), body.get("password"))
        except AccountError as error:
            if error.code == "wrong":
                self.server.limiter.failed(address)
            raise
        self.server.limiter.clear(address)
        cookie = (f"{COOKIE}={token}; Path={PREFIX}; Max-Age={accounts_module.SESSION_SECONDS}; "
                  "HttpOnly; Secure; SameSite=Lax")
        self.send_json({"ok": True, "user": user}, cookie=cookie)

    def route(self, path: str) -> None:
        if path == PREFIX:
            self.redirect(f"{PREFIX}/")
            return
        if path == f"{PREFIX}/login":
            self.respond(200, (SITE / "login.html").read_bytes(), "text/html; charset=utf-8", "no-cache")
            return
        if not path.startswith(f"{PREFIX}/"):
            self.send_json({"error": "not found"}, 404)
            return
        rest = path[len(PREFIX) + 1:]
        if rest in OPEN_FILES:
            self.static(rest)
            return
        user = self.current_user()
        if not user:
            if rest.startswith("api/"):
                self.send_json({"error": "нужен вход"}, 401)
            else:
                self.redirect(f"{PREFIX}/login")
            return
        if rest == "":
            self.static("index.html")
        elif rest in STATIC:
            self.static(rest)
        elif rest == "api/me":
            self.send_json({"user": user})
        elif rest == "api/users":
            if user["role"] != "admin":
                self.send_json({"error": "только для администратора"}, 403)
            else:
                self.send_json({"users": self.server.accounts.list_users()})
        elif rest == "api/health":
            db = open_db(self.server.db_path)
            self.send_json({"ok": True, "fetch": last_fetch(db)})
            db.close()
        elif rest == "api/matches":
            db = open_db(self.server.db_path)
            self.send_json(list_matches(db, self.server.history))
            db.close()
        else:
            found = re.fullmatch(r"api/matches/(\d{1,20})", rest)
            if not found:
                self.send_json({"error": "not found"}, 404)
                return
            db = open_db(self.server.db_path)
            detail = match_detail(db, self.server.history, found.group(1))
            db.close()
            self.send_json(detail if detail else {"error": "нет такого матча"}, 200 if detail else 404)

    def static(self, name: str) -> None:
        self.respond(200, (SITE / name).read_bytes(), STATIC[name], "no-cache")

    def log_message(self, format, *args) -> None:  # noqa: A002 - only problems go to the journal
        if args and str(args[0]).startswith("database"):
            super().log_message(format, *args)


def make_server(db_path: Path, port: int, accounts, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.db_path = db_path
    server.history = History(db_path)
    server.accounts = accounts
    server.limiter = LoginLimiter()
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--accounts", type=Path, default=accounts_module.DEFAULT_DIR)
    parser.add_argument("--smtp", type=Path, default=Path(os.environ.get("CREDENTIALS_DIRECTORY", ".")) / "smtp.env")
    parser.add_argument("--port", type=int, default=8107)
    args = parser.parse_args()
    accounts = accounts_module.Accounts(args.accounts, accounts_module.Mailer(args.smtp))
    make_server(args.db, args.port, accounts).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
