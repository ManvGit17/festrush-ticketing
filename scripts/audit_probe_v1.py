"""Probe a running v1 server for the concurrency findings in docs/audit.md.

Usage (against a scratch copy of v1, never the committed fest.db):

    python scripts/audit_probe_v1.py http://127.0.0.1:8090

Uses only the standard library. Prints one JSON object so the numbers can be diffed between runs.
"""

import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def call(url: str, data: dict[str, str] | None = None) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    try:
        with OPENER.open(urllib.request.Request(url, data=body), timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def burst(n: int, fn):  # type: ignore[no-untyped-def]
    results: list = [None] * n
    gate = threading.Barrier(n)

    def run(i: int) -> None:
        gate.wait()
        results[i] = fn(i)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def main(base: str) -> None:
    out: dict[str, object] = {"base": base}

    # P1: 100 simultaneous clicks on "Register" for the same event by the same user.
    user = "probe_double_click"
    codes = burst(100, lambda i: call(f"{base}/register_event/1?user={user}")[0])
    _, page = call(f"{base}/my-events?user={user}")
    out["P1_clicks"] = 100
    out["P1_status_codes"] = dict(Counter(codes))
    out["P1_rows_created_for_one_user_one_event"] = page.count("Confirmed")

    # P2: 200 simultaneous sign-ups with distinct usernames. Any "already exists" answer is false,
    # because the bare ``except:`` reports every database error (for example "database is locked")
    # as a duplicate username.
    def signup(i: int) -> str:
        status, body = call(f"{base}/register", {"username": f"probe_{i}", "password": "pw"})
        return "created" if status == 302 else body[:60]

    answers = burst(200, signup)
    out["P2_signups"] = 200
    out["P2_answers"] = dict(Counter(answers))

    # P3: checkout returns the same page whoever asks, because it never learns who that is.
    totals = {call(f"{base}/checkout")[1].count("₹") for _ in range(3)}
    out["P3_checkout_is_user_independent"] = len(totals) == 1

    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8090")
