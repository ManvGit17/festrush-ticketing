"""Shared helpers for the Locust files: every simulated student gets their own account.

v1 used one fictional user who never existed (audit A10). Here each Locust user signs up with a
unique name and signs in, the way a real student would, so the load is realistic and the
tables do not grow from one repeated row.

Run any file with, for example:

    locust -f locust/events_locustfile.py --host http://localhost:8080
"""

import re
import uuid

from locust import HttpUser

TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
EVENT_FORM = re.compile(r'action="/register_event/(\d+)"')
PASSWORD = "load-test-password"


def form_token(user: HttpUser, path: str = "/login") -> str:
    page = user.client.get(path, name=f"GET {path} (form)")
    match = TOKEN.search(page.text)
    if match is None:
        raise RuntimeError(f"no CSRF token on {path}: is the app running at {user.host}?")
    return match.group(1)


def sign_up_and_in(user: HttpUser) -> str:
    """Create a fresh account, sign in, and return the username."""
    username = f"load_{uuid.uuid4().hex[:12]}"
    user.client.post(
        "/register",
        data={
            "username": username,
            "password": PASSWORD,
            "csrf_token": form_token(user, "/register"),
        },
        name="POST /register",
    )
    user.client.post(
        "/login",
        data={"username": username, "password": PASSWORD, "csrf_token": form_token(user)},
        name="POST /login",
    )
    return username
