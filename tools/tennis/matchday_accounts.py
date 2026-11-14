#!/usr/bin/env python3
"""Accounts of the Matchday page: e-mail and password, the same way as on the EPL site.

A password is made here, goes only by e-mail and is kept only as a scrypt hash; nothing prints it, so
if a letter cannot be sent nothing changes. Two roles: a commentator sees the page, an admin also gives
and takes access. These accounts are this site's own: another site's cookie does not reach this address.

    python tools/tennis/matchday_accounts.py invite someone@example.com "Имя Фамилия" --role admin --smtp FILE
    python tools/tennis/matchday_accounts.py list
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows, where only the tests run
    fcntl = None

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "local-data" / "tennis" / "accounts"
SITE_URL = "https://bolshe.109-172-113-187.sslip.io/matchday/"
ROLES = ("admin", "commentator")
EMAIL_RE = re.compile(r"^[^@\s,;<>\"']{1,64}@[a-z0-9.-]{1,190}\.[a-z]{2,24}$")
NAME_LIMIT = 80
SESSION_SECONDS = 60 * 86400
PASSWORD_MIN = 8
PASSWORD_MAX = 128
PASSWORD_LENGTH = 14
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: read from a letter and typed on a phone
ACCOUNT_FAILURES = 10
ACCOUNT_WINDOW = 900
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
FROM_NAME = "Больше! Matchday"

INVITE_SUBJECT = "Доступ к Больше! Matchday"
RESET_SUBJECT = "Новый пароль к Больше! Matchday"
LETTER = """{first}, привет!

{intro}

Сайт: {site}
Логин: {email}
Пароль: {password}

Пароль можно поменять в личном кабинете, это кнопка с вашим именем вверху сайта
{admin}"""
ADMIN_LINE = "\nУ вас права администратора: в разделе «Доступ» можно дать доступ новому человеку или забрать его\n"


class AccountError(Exception):
    """code is what the page shows in Russian; status is the HTTP answer."""

    MESSAGES = {
        "wrong": ("Неверная почта или пароль", 401),
        "locked": ("Слишком много попыток. Подождите 15 минут", 429),
        "bad_email": ("Проверьте почту", 400),
        "bad_name": ("Укажите имя", 400),
        "bad_role": ("Нет такой роли", 400),
        "exists": ("Такая почта уже есть", 409),
        "unknown": ("Такой почты нет", 404),
        "self": ("Свой доступ забрать нельзя", 400),
        "short": (f"Пароль от {PASSWORD_MIN} до {PASSWORD_MAX} знаков", 400),
        "old_wrong": ("Старый пароль неверный", 400),
        "mail_off": ("Почта не настроена, письмо не отправить", 503),
        "mail_failed": ("Письмо не ушло, ничего не изменилось", 502),
    }

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.message, self.status = self.MESSAGES[code]


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        expected = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), **SCRYPT).hex()
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(expected, digest)


DUMMY_HASH = hash_password(secrets.token_hex(8))  # an unknown e-mail costs the same time as a wrong password


def new_password() -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def clean_email(value) -> str:
    email = str(value or "").strip().lower()
    if not EMAIL_RE.fullmatch(email):
        raise AccountError("bad_email")
    return email


def clean_name(value) -> str:
    name = " ".join(str(value or "").split())
    if not name or len(name) > NAME_LIMIT or any(c in name for c in '<>"\r\n'):
        raise AccountError("bad_name")
    return name


def public_user(user: dict) -> dict:
    return {key: user.get(key) for key in ("email", "name", "role", "active", "created_at")}


def read_env(path: Path) -> dict:
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Mailer:
    """SMTP over SSL from a KEY=VALUE file: SMTP_HOST, SMTP_PORT (465), SMTP_LOGIN, SMTP_PASSWORD."""

    def __init__(self, path: Path | None):
        self.config = read_env(path) if path and path.exists() else {}

    @property
    def ready(self) -> bool:
        return all(self.config.get(k) for k in ("SMTP_HOST", "SMTP_LOGIN", "SMTP_PASSWORD"))

    def send(self, to_email: str, to_name: str, subject: str, text: str) -> None:
        message = EmailMessage()
        message["From"] = formataddr((FROM_NAME, self.config["SMTP_LOGIN"]))
        message["To"] = formataddr((to_name, to_email))
        message["Subject"] = subject
        message["Message-ID"] = make_msgid(domain=self.config["SMTP_LOGIN"].split("@")[-1])
        message.set_content(text)
        port = int(self.config.get("SMTP_PORT") or 465)
        with smtplib.SMTP_SSL(self.config["SMTP_HOST"], port, context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(self.config["SMTP_LOGIN"], self.config["SMTP_PASSWORD"])
            smtp.send_message(message)


def letter(user: dict, password: str, invite: bool) -> tuple[str, str]:
    intro = "Это доступ к Больше! Matchday: коэффициенты Winline и плашки к теннисным матчам" if invite else "Новый пароль к Больше! Matchday"
    text = LETTER.format(first=user["name"].split()[0], intro=intro, site=SITE_URL, email=user["email"], password=password,
                         admin=ADMIN_LINE if user["role"] == "admin" else "")
    return (INVITE_SUBJECT if invite else RESET_SUBJECT), text


class Accounts:
    def __init__(self, directory: Path, mailer):
        self.directory, self.mailer = directory, mailer
        self.thread_lock = threading.Lock()
        self.failures: dict[str, list[float]] = {}
        directory.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def locked(self):
        """The service and a command run from the server's shell can both write here."""
        with self.thread_lock, (self.directory / ".lock").open("a") as handle:
            if hasattr(os, "geteuid") and os.geteuid() == 0:  # root's command must not leave it unwritable for the service
                owner = self.directory.stat()
                os.chown(self.directory / ".lock", owner.st_uid, owner.st_gid)
            if fcntl:
                fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def read(self, name: str) -> dict:
        try:
            return json.loads((self.directory / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def write(self, name: str, data: dict) -> None:
        temporary = self.directory / f".{name}.{os.getpid()}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        if hasattr(os, "geteuid") and os.geteuid() == 0:  # root's command must leave files the service can read
            owner = self.directory.stat()
            os.chown(temporary, owner.st_uid, owner.st_gid)
        os.replace(temporary, self.directory / name)

    def account_locked(self, email: str, now: float) -> bool:
        recent = [t for t in self.failures.get(email, []) if now - t < ACCOUNT_WINDOW]
        if recent:
            self.failures[email] = recent
        else:
            self.failures.pop(email, None)
        return len(recent) >= ACCOUNT_FAILURES

    def login(self, email_value, password_value, now: float | None = None) -> tuple[dict, str]:
        moment = now if now is not None else time.time()
        email = str(email_value or "").strip().lower()
        password = str(password_value or "")[:PASSWORD_MAX]
        with self.thread_lock:
            if self.account_locked(email, moment):
                raise AccountError("locked")
        user = self.read("users.json").get(email)
        right = check_password(password, user["password"] if user else DUMMY_HASH)
        if not user or not user.get("active") or not right:
            with self.thread_lock:
                self.failures.setdefault(email, []).append(moment)
            raise AccountError("wrong")
        with self.thread_lock:
            self.failures.pop(email, None)
        token = secrets.token_urlsafe(32)
        with self.locked():
            sessions = {k: v for k, v in self.read("sessions.json").items() if v["expires"] > moment}
            sessions[self.token_key(token)] = {"email": email, "expires": moment + SESSION_SECONDS}
            self.write("sessions.json", sessions)
        return public_user(user), token

    @staticmethod
    def token_key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def session_user(self, token: str | None, now: float | None = None) -> dict | None:
        """Read on every request, so taking access away works at once."""
        if not token:
            return None
        session = self.read("sessions.json").get(self.token_key(token))
        if not session or session["expires"] <= (now if now is not None else time.time()):
            return None
        user = self.read("users.json").get(session["email"])
        return public_user(user) if user and user.get("active") else None

    def logout(self, token: str | None) -> None:
        if token:
            with self.locked():
                sessions = self.read("sessions.json")
                if sessions.pop(self.token_key(token), None):
                    self.write("sessions.json", sessions)

    def close_sessions(self, email: str, keep: str | None = None) -> None:
        sessions = self.read("sessions.json")
        kept = {k: v for k, v in sessions.items() if v["email"] != email or k == keep}
        if len(kept) != len(sessions):
            self.write("sessions.json", kept)

    def change_password(self, email: str, old, new, token: str | None = None) -> None:
        new = str(new or "")
        if not PASSWORD_MIN <= len(new) <= PASSWORD_MAX:
            raise AccountError("short")
        with self.locked():
            users = self.read("users.json")
            if email not in users or not check_password(str(old or "")[:PASSWORD_MAX], users[email]["password"]):
                raise AccountError("old_wrong")
            users[email]["password"] = hash_password(new)
            self.write("users.json", users)
            self.close_sessions(email, self.token_key(token) if token else None)

    def list_users(self) -> list[dict]:
        users = [public_user(u) for u in self.read("users.json").values()]
        return sorted(users, key=lambda u: (u["role"] != "admin", not u["active"], u["name"].lower()))

    def send_password(self, user: dict, password: str, invite: bool) -> None:
        if not self.mailer.ready:
            raise AccountError("mail_off")
        subject, text = letter(user, password, invite)
        try:
            self.mailer.send(user["email"], user["name"], subject, text)
        except (OSError, smtplib.SMTPException) as problem:
            print(f"mail to the account failed: {type(problem).__name__}", file=sys.stderr, flush=True)
            raise AccountError("mail_failed") from problem

    def invite(self, name_value, email_value, role: str = "commentator") -> dict:
        """The account appears only after the letter went out."""
        name, email = clean_name(name_value), clean_email(email_value)
        if role not in ROLES:
            raise AccountError("bad_role")
        if email in self.read("users.json"):
            raise AccountError("exists")
        password = new_password()
        user = {"email": email, "name": name, "role": role, "active": True, "password": hash_password(password),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.send_password(user, password, invite=True)
        with self.locked():
            users = self.read("users.json")
            users[email] = user
            self.write("users.json", users)
        return public_user(user)

    def reset(self, email_value) -> None:
        email = clean_email(email_value)
        user = self.read("users.json").get(email)
        if not user:
            raise AccountError("unknown")
        password = new_password()
        self.send_password(user, password, invite=False)
        with self.locked():
            users = self.read("users.json")
            users[email]["password"] = hash_password(password)
            self.write("users.json", users)
            self.close_sessions(email)

    def set_access(self, email_value, active, by: str) -> dict:
        email = clean_email(email_value)
        if email == by and not active:
            raise AccountError("self")
        with self.locked():
            users = self.read("users.json")
            if email not in users:
                raise AccountError("unknown")
            users[email]["active"] = bool(active)
            self.write("users.json", users)
            if not active:
                self.close_sessions(email)
            return public_user(users[email])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["invite", "reset", "list"])
    parser.add_argument("email", nargs="?")
    parser.add_argument("name", nargs="?")
    parser.add_argument("--role", choices=ROLES, default="commentator")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--smtp", type=Path)
    args = parser.parse_args()
    accounts = Accounts(args.dir, Mailer(args.smtp))
    try:
        if args.command == "invite":
            user = accounts.invite(args.name, args.email, args.role)
            print(f"{user['email']} ({user['role']}): письмо с паролем отправлено")
        elif args.command == "reset":
            accounts.reset(args.email)
            print("Письмо с новым паролем отправлено")
        else:
            for user in accounts.list_users():
                print(user["email"], user["role"], "активен" if user["active"] else "без доступа", user["created_at"])
    except AccountError as error:
        print(error.message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
