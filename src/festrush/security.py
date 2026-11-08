"""Password hashing, session tokens and CSRF tokens. Closes audit findings S1, S2 and S7."""

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

SESSION_COOKIE = "festrush_session"
CSRF_COOKIE = "festrush_csrf"


def make_hasher(*, insecure_fast: bool = False) -> PasswordHasher:
    if insecure_fast:
        return PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    return PasswordHasher()  # Argon2id with the library's current recommended parameters


class Passwords:
    def __init__(self, *, insecure_fast: bool = False) -> None:
        self._hasher = make_hasher(insecure_fast=insecure_fast)
        # Verified when the username does not exist, so "no such user" and "wrong password"
        # take about the same time.
        self._dummy = self._hasher.hash(secrets.token_urlsafe(16))

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, stored_hash: str | None, password: str) -> bool:
        try:
            return self._hasher.verify(stored_hash or self._dummy, password) and bool(stored_hash)
        except (VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, stored_hash: str) -> bool:
        return self._hasher.check_needs_rehash(stored_hash)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


class Csrf:
    """Signed double-submit tokens.

    A random value goes in a cookie and, signed, into every form. A cross-site page can make the
    browser send the cookie but cannot read it, so it cannot produce a matching form value.
    """

    def __init__(self, secret: str) -> None:
        self._key = secret.encode()

    def new_cookie_value(self) -> str:
        return secrets.token_urlsafe(24)

    def form_token(self, cookie_value: str) -> str:
        return f"{cookie_value}.{self._sign(cookie_value)}"

    def valid(self, cookie_value: str | None, form_token: str | None) -> bool:
        if not cookie_value or not form_token:
            return False
        expected = self.form_token(cookie_value)
        return hmac.compare_digest(expected.encode(), form_token.encode())

    def _sign(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()
