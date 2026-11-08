"""Load scenarios for the seat rush. Run through ``bench/run.py``, which sets everything up.

Scenarios (``BENCH_SCENARIO``):

* ``spike``        every simulated student arrives at once and tries to buy one seat:
                   reserve, then order, then pay. Each user does this exactly once.
* ``retry-storm``  the same, but every write is sent twice at the same moment with the same
                   Idempotency-Key, like a double click or an over-eager retry. The system must
                   treat each pair as one request.
* ``soak``         users keep browsing and buying across several events for a fixed time, with
                   short holds so expiry runs, and a provider that is slow, declines and loses
                   responses now and then.

Students are real rows with pre-made session tokens (``bench-token-<i>``), so the numbers measure
seat claiming and payment, not password hashing.
"""

import itertools
import json
import os
import random

import gevent

from locust import HttpUser, between, constant, events, task

SCENARIO = os.environ.get("BENCH_SCENARIO", "spike")
TOTAL = int(os.environ.get("BENCH_USERS", "100"))
EVENT_IDS = [int(x) for x in os.environ.get("BENCH_EVENT_IDS", "1").split(",")]

_next_index = itertools.count()
_finished = 0


class Buyer(HttpUser):
    wait_time = between(0.5, 1.5) if SCENARIO == "soak" else constant(0)

    def on_start(self) -> None:
        self.index = next(_next_index)
        self.headers = {"Authorization": f"Bearer bench-token-{self.index}"}

    # -- helpers -----------------------------------------------------------------------------

    def _send(self, name: str, method: str, path: str, key: str | None = None):  # type: ignore[no-untyped-def]
        headers = dict(self.headers)
        if key:
            headers["Idempotency-Key"] = key
        # 409 is an expected answer (sold out, already holding); only 5xx, 4xx and timeouts count
        # as failures.
        with self.client.request(method, path, headers=headers, name=name, catch_response=True) as r:
            if r.status_code in (200, 201, 409):
                r.success()
            else:
                r.failure(f"HTTP {r.status_code}")
            return r

    def _twice(self, name: str, method: str, path: str, key: str | None):  # type: ignore[no-untyped-def]
        """Send the same request twice at once and check both got the same answer."""
        first = gevent.spawn(self._send, name, method, path, key)
        second = gevent.spawn(self._send, name, method, path, key)
        gevent.joinall([first, second])
        a, b = first.value, second.value
        # With a key the two answers must be identical (compare parsed JSON: the stored copy
        # comes back from a jsonb column, which orders keys differently). Without a key (pay),
        # one call does the work and the other reports the current state, so only the status
        # code has to agree.
        same = a.status_code == b.status_code and (key is None or a.json() == b.json())
        if not same:
            events.request.fire(
                request_type="CHECK",
                name="duplicate request got a different answer",
                response_time=0,
                response_length=0,
                exception=Exception(f"{a.status_code} {a.text} != {b.status_code} {b.text}"),
            )
        return a

    def _write(self, name: str, method: str, path: str, key: str | None = None):  # type: ignore[no-untyped-def]
        if SCENARIO == "retry-storm":
            return self._twice(name, method, path, key)
        return self._send(name, method, path, key)

    def _buy(self, event_id: int, tag: str) -> None:
        reserved = self._write(
            "POST /api/events/[id]/reserve", "POST", f"/api/events/{event_id}/reserve", f"r-{tag}"
        )
        if reserved.status_code != 201:
            return
        order = self._write("POST /api/orders", "POST", "/api/orders", f"o-{tag}")
        if order.status_code != 201:
            return
        # Paying is safe to repeat without a key: only one call ever reaches the provider.
        self._write("POST /api/orders/[id]/pay", "POST", f"/api/orders/{order.json()['id']}/pay")

    # -- the scenarios -----------------------------------------------------------------------

    @task
    def scenario(self) -> None:
        global _finished
        if SCENARIO == "soak":
            event_id = random.choice(EVENT_IDS)
            self._send("GET /api/events/[id]", "GET", f"/api/events/{event_id}")
            self._buy(event_id, f"{self.index}-{event_id}-{random.random():.6f}")
            return
        self._buy(EVENT_IDS[0], str(self.index))
        _finished += 1
        self.stop()
        if _finished >= TOTAL:
            self.environment.runner.quit()  # type: ignore[union-attr]


@events.quitting.add_listener
def dump_statistics(environment, **_kwargs):  # type: ignore[no-untyped-def]
    """Write the final numbers straight from Locust's in-memory statistics.

    (Locust's CSV files are written on a timer, so a short run can miss its last second.)
    """
    path = os.environ.get("BENCH_STATS_OUT")
    if not path:
        return

    def row(entry):  # type: ignore[no-untyped-def]
        return {
            "requests": entry.num_requests,
            "failures": entry.num_failures,
            "avg_ms": round(entry.avg_response_time, 1),
            "p50_ms": entry.get_response_time_percentile(0.50),
            "p95_ms": entry.get_response_time_percentile(0.95),
            "p99_ms": entry.get_response_time_percentile(0.99),
            "max_ms": round(entry.max_response_time, 1),
            "rps": round(entry.total_rps, 1),
        }

    out = {name: row(e) for (name, _method), e in environment.stats.entries.items()}
    out["Aggregated"] = row(environment.stats.total)
    out["_errors"] = {str(k): v.occurrences for k, v in environment.stats.errors.items()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f)
