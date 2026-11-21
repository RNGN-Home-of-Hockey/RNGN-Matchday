"""The Matchday server: nothing opens without an account, accounts work by e-mail. Mail is a fake here."""
import json
import re
import smtplib
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "tennis"))
sys.path.insert(0, str(ROOT / "tests"))

import matchday_accounts as ma  # noqa: E402
import matchday_server as md  # noqa: E402
import winline_snapshot as ws  # noqa: E402
from test_matchday import EVENT, WINNER, make_db  # noqa: E402

ADMIN = "boss@example.com"
PERSON = "person@example.com"


class FakeMail:
    ready = True

    def __init__(self):
        self.sent = []
        self.broken = False

    def send(self, to_email, to_name, subject, text):
        if self.broken:
            raise smtplib.SMTPException("no")
        self.sent.append((to_email, subject, text))

    def password_for(self, email):
        text = [m for m in self.sent if m[0] == email][-1][2]
        return re.search(r"Пароль: (\S+)", text).group(1)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.mail = FakeMail()
        self.accounts = ma.Accounts(Path(self.dir.name) / "accounts", self.mail)


class Accounts(Base):
    def tearDown(self):
        self.dir.cleanup()

    def test_invite_sends_a_working_password_that_is_stored_only_as_a_hash(self):
        user = self.accounts.invite("Анна Альфа", ADMIN.upper(), "admin")
        self.assertEqual((user["email"], user["role"], user["active"]), (ADMIN, "admin", True))
        self.assertNotIn("password", user)
        password = self.mail.password_for(ADMIN)
        self.assertEqual(len(password), ma.PASSWORD_LENGTH)
        self.assertNotIn(password, (Path(self.dir.name) / "accounts" / "users.json").read_text(encoding="utf-8"))
        self.assertEqual(self.accounts.login(ADMIN, password)[0]["email"], ADMIN)

    def test_letter_says_where_and_names_the_admin_part(self):
        self.accounts.invite("Анна Альфа", ADMIN, "admin")
        self.accounts.invite("Бея Бета", PERSON)
        admin_text, person_text = self.mail.sent[0][2], self.mail.sent[1][2]
        self.assertIn(ma.SITE_URL, admin_text)
        self.assertTrue(admin_text.startswith("Анна, привет!"))
        self.assertIn("администратора", admin_text)
        self.assertNotIn("администратора", person_text)

    def test_a_letter_that_did_not_go_changes_nothing(self):
        self.mail.broken = True
        with self.assertRaises(ma.AccountError) as caught:
            self.accounts.invite("Анна Альфа", ADMIN)
        self.assertEqual(caught.exception.code, "mail_failed")
        self.assertEqual(self.accounts.list_users(), [])
        self.mail.broken = False
        self.accounts.invite("Анна Альфа", ADMIN)
        old = self.mail.password_for(ADMIN)
        self.mail.broken = True
        with self.assertRaises(ma.AccountError):
            self.accounts.reset(ADMIN)
        self.assertTrue(self.accounts.login(ADMIN, old))

    def test_no_mail_configured_is_said_plainly(self):
        accounts = ma.Accounts(Path(self.dir.name) / "other", ma.Mailer(None))
        with self.assertRaises(ma.AccountError) as caught:
            accounts.invite("Анна Альфа", ADMIN)
        self.assertEqual(caught.exception.code, "mail_off")

    def test_bad_input(self):
        for name, email, role in (("Анна", "not-an-email", "admin"), ("", ADMIN, "admin"), ("<b>", ADMIN, "admin"), ("Анна", ADMIN, "owner")):
            with self.assertRaises(ma.AccountError):
                self.accounts.invite(name, email, role)
        self.accounts.invite("Анна", ADMIN)
        with self.assertRaises(ma.AccountError) as caught:
            self.accounts.invite("Другая", ADMIN)
        self.assertEqual(caught.exception.code, "exists")

    def test_wrong_and_unknown_look_the_same_and_lock_after_too_many(self):
        self.accounts.invite("Анна", ADMIN)
        right = self.mail.password_for(ADMIN)
        for email in (ADMIN, "nobody@example.com"):
            with self.assertRaises(ma.AccountError) as caught:
                self.accounts.login(email, "wrong-one")
            self.assertEqual(caught.exception.code, "wrong")
        for _ in range(ma.ACCOUNT_FAILURES):
            with self.assertRaises(ma.AccountError):
                self.accounts.login(ADMIN, "wrong-one")
        with self.assertRaises(ma.AccountError) as caught:
            self.accounts.login(ADMIN, right)
        self.assertEqual(caught.exception.code, "locked")

    def test_changing_a_password_keeps_this_session_and_ends_the_others(self):
        self.accounts.invite("Анна", ADMIN)
        old = self.mail.password_for(ADMIN)
        _, here = self.accounts.login(ADMIN, old)
        _, there = self.accounts.login(ADMIN, old)
        with self.assertRaises(ma.AccountError):
            self.accounts.change_password(ADMIN, "wrong-old", "new-password-1", here)
        with self.assertRaises(ma.AccountError):
            self.accounts.change_password(ADMIN, old, "short", here)
        self.accounts.change_password(ADMIN, old, "new-password-1", here)
        self.assertIsNotNone(self.accounts.session_user(here))
        self.assertIsNone(self.accounts.session_user(there))
        self.assertTrue(self.accounts.login(ADMIN, "new-password-1"))

    def test_taking_access_away_works_at_once_and_not_on_oneself(self):
        self.accounts.invite("Анна", ADMIN, "admin")
        self.accounts.invite("Бея", PERSON)
        _, token = self.accounts.login(PERSON, self.mail.password_for(PERSON))
        with self.assertRaises(ma.AccountError) as caught:
            self.accounts.set_access(ADMIN, False, ADMIN)
        self.assertEqual(caught.exception.code, "self")
        self.accounts.set_access(PERSON, False, ADMIN)
        self.assertIsNone(self.accounts.session_user(token))
        with self.assertRaises(ma.AccountError):
            self.accounts.login(PERSON, self.mail.password_for(PERSON))
        self.accounts.set_access(PERSON, True, ADMIN)
        self.assertTrue(self.accounts.login(PERSON, self.mail.password_for(PERSON)))

    def test_reset_sends_a_new_one_and_ends_sessions(self):
        self.accounts.invite("Анна", ADMIN)
        old = self.mail.password_for(ADMIN)
        _, token = self.accounts.login(ADMIN, old)
        self.accounts.reset(ADMIN)
        self.assertNotEqual(self.mail.password_for(ADMIN), old)
        self.assertIsNone(self.accounts.session_user(token))
        self.assertTrue(self.accounts.login(ADMIN, self.mail.password_for(ADMIN)))

    def test_expired_session(self):
        self.accounts.invite("Анна", ADMIN)
        _, token = self.accounts.login(ADMIN, self.mail.password_for(ADMIN), now=1000)
        self.assertIsNone(self.accounts.session_user(token, now=1000 + ma.SESSION_SECONDS + 1))
        self.assertIsNone(self.accounts.session_user("made-up"))
        self.assertIsNone(self.accounts.session_user(None))


class Http(Base):
    def setUp(self):
        super().setUp()
        path = Path(self.dir.name) / "t.sqlite"
        raw = make_db(path)
        ws.store(raw, [EVENT], [WINNER], "2026-11-04T10:00:00+00:00")
        raw.close()
        self.server = md.make_server(path, 0, self.accounts)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.accounts.invite("Анна Альфа", ADMIN, "admin")
        self.accounts.invite("Бея Бета", PERSON)
        self.admin = self.log_in(ADMIN)
        self.person = self.log_in(PERSON)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def send(self, path, cookie=None, data=None, kind="application/json"):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        headers = {"Cookie": cookie} if cookie else {}
        if data is not None:
            headers["Content-Type"] = kind
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def post(self, path, body, cookie=None):
        status, headers, raw = self.send(f"/matchday/api/{path}", cookie, json.dumps(body).encode("utf-8"))
        return status, headers, json.loads(raw)

    def log_in(self, email):
        status, headers, _ = self.post("login", {"email": email, "password": self.mail.password_for(email)})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";")[0]

    def test_nothing_opens_without_a_login(self):
        for path in ("/matchday/", "/matchday/app.js", "/matchday/index.html"):
            status, headers, _ = self.send(path)
            self.assertEqual((status, headers["Location"]), (302, "/matchday/login"), path)
        for path in ("/matchday/api/matches", "/matchday/api/matches/100", "/matchday/api/health", "/matchday/api/me", "/matchday/api/users"):
            status, _, body = self.send(path)
            self.assertEqual(status, 401, path)
            self.assertNotIn("Альфа", body.decode("utf-8"))

    def test_login_page_and_what_it_needs_are_open(self):
        for path in ("/matchday/login", "/matchday/styles.css", "/matchday/login.js"):
            self.assertEqual(self.send(path)[0], 200, path)

    def test_made_up_sessions_are_refused(self):
        for cookie in (f"{md.COOKIE}=abc", "other=1", f"{md.COOKIE}=", "broken;;=="):
            self.assertEqual(self.send("/matchday/api/matches", cookie)[0], 401, cookie)

    def test_session_cookie_is_safe(self):
        _, headers, answer = self.post("login", {"email": ADMIN, "password": self.mail.password_for(ADMIN)})
        for part in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/matchday"):
            self.assertIn(part, headers["Set-Cookie"])
        self.assertEqual(answer["user"]["role"], "admin")
        self.assertNotIn("password", answer["user"])

    def test_wrong_password_says_nothing_about_the_address(self):
        wrong = self.post("login", {"email": ADMIN, "password": "wrong"})
        unknown = self.post("login", {"email": "nobody@example.com", "password": "wrong"})
        self.assertEqual((wrong[0], wrong[2]), (unknown[0], unknown[2]))
        self.assertEqual(wrong[0], 401)

    def test_one_address_is_limited_across_many_accounts(self):
        for n in range(md.MAX_TRIES):
            self.post("login", {"email": f"n{n}@example.com", "password": "wrong"})
        status = self.post("login", {"email": ADMIN, "password": self.mail.password_for(ADMIN)})[0]
        self.assertEqual(status, 429)

    def test_a_form_from_another_site_cannot_post(self):
        data = json.dumps({"email": ADMIN, "password": self.mail.password_for(ADMIN)}).encode()
        self.assertEqual(self.send("/matchday/api/login", data=data, kind="application/x-www-form-urlencoded")[0], 400)
        self.assertEqual(self.send("/matchday/api/users/access", self.admin, data, "text/plain")[0], 400)

    def test_too_long_or_broken_bodies_are_refused(self):
        self.assertEqual(self.send("/matchday/api/login", data=b"x" * (md.MAX_BODY + 1))[0], 400)
        self.assertEqual(self.send("/matchday/api/login", data=b"not json")[0], 400)
        self.assertEqual(self.send("/matchday/api/login", data=b"[1]")[0], 400)

    def test_logout_ends_the_session_on_the_server_too(self):
        status, headers, _ = self.post("logout", {}, self.person)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.send("/matchday/api/me", self.person)[0], 401)

    def test_me(self):
        self.assertEqual(json.loads(self.send("/matchday/api/me", self.person)[2])["user"]["email"], PERSON)

    def test_a_commentator_sees_the_page_but_not_the_people(self):
        self.assertEqual(self.send("/matchday/api/matches", self.person)[0], 200)
        self.assertEqual(self.send("/matchday/api/users", self.person)[0], 403)
        for path, body in (("users/add", {"name": "Новая", "email": "x@example.com"}), ("users/access", {"email": ADMIN, "active": False}),
                           ("users/reset", {"email": ADMIN})):
            self.assertEqual(self.post(path, body, self.person)[0], 403, path)
        self.assertEqual(len(self.accounts.list_users()), 2)

    def test_an_admin_manages_people(self):
        listed = json.loads(self.send("/matchday/api/users", self.admin)[2])["users"]
        self.assertEqual({u["email"] for u in listed}, {ADMIN, PERSON})
        self.assertNotIn("password", json.dumps(listed))
        status, _, answer = self.post("users/add", {"name": "Гая Гамма", "email": "gaya@example.com"}, self.admin)
        self.assertEqual((status, answer["user"]["role"]), (200, "commentator"))
        self.assertEqual(self.mail.sent[-1][0], "gaya@example.com")
        self.assertEqual(self.post("users/access", {"email": PERSON, "active": False}, self.admin)[0], 200)
        self.assertEqual(self.send("/matchday/api/me", self.person)[0], 401)
        self.assertEqual(self.post("users/access", {"email": ADMIN, "active": False}, self.admin)[0], 400)
        self.assertEqual(self.post("users/reset", {"email": "nobody@example.com"}, self.admin)[0], 404)

    def test_a_failed_letter_is_reported_and_adds_nobody(self):
        self.mail.broken = True
        status, _, answer = self.post("users/add", {"name": "Гая", "email": "gaya@example.com"}, self.admin)
        self.assertEqual(status, 502)
        self.assertIn("Письмо не ушло", answer["error"])
        self.assertEqual(len(self.accounts.list_users()), 2)

    def test_change_password_through_the_page(self):
        old = self.mail.password_for(PERSON)
        self.assertEqual(self.post("password", {"old": "wrong", "new": "brand-new-pass"}, self.person)[0], 400)
        self.assertEqual(self.post("password", {"old": old, "new": "brand-new-pass"}, self.person)[0], 200)
        self.assertEqual(self.post("login", {"email": PERSON, "password": "brand-new-pass"})[0], 200)

    def test_the_address_without_a_slash_goes_to_the_page(self):
        status, headers, _ = self.send("/matchday")
        self.assertEqual((status, headers["Location"]), (302, "/matchday/"))

    def test_page_and_its_files_after_login(self):
        for path, kind in (("/matchday/", "text/html"), ("/matchday/app.js", "text/javascript"), ("/matchday/styles.css", "text/css")):
            status, headers, body = self.send(path, self.person)
            self.assertEqual(status, 200)
            self.assertIn(kind, headers["Content-Type"])
            self.assertTrue(body)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_data(self):
        status, _, body = self.send("/matchday/api/matches", self.person)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["matches"][0]["id"], "100")
        status, headers, body = self.send("/matchday/api/matches/100", self.person)
        self.assertEqual((status, headers["Cache-Control"]), (200, "no-store"))
        self.assertEqual(json.loads(body)["team1"], "Альфа А.")
        self.assertEqual(self.send("/matchday/api/matches/999", self.person)[0], 404)
        self.assertTrue(json.loads(self.send("/matchday/api/health", self.person)[2])["ok"])

    def test_nothing_else_is_served(self):
        for path in ("/", "/matchday/../schema.sql", "/matchday/matchday_server.py", "/matchday/api/matches/1%20OR%201=1",
                     "/matchday/api/matches/abc", "/matchday/%2e%2e/%2e%2e/secret", "/other"):
            self.assertEqual(self.send(path, self.admin)[0], 404, path)
        self.assertEqual(self.post("matches", {}, self.admin)[0], 404)

    def test_pages_build_from_text_only(self):
        for name in ("app.js", "login.js"):
            code = (md.SITE / name).read_text(encoding="utf-8")
            self.assertNotIn("innerHTML", code)
            self.assertNotIn("insertAdjacentHTML", code)


if __name__ == "__main__":
    unittest.main()
