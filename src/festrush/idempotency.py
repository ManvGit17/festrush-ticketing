"""Idempotency keys: a retried request gets the first request's answer, and does nothing twice.

The key row, the work and the stored response are written in ONE transaction:

* A duplicate that arrives while the first request is still running blocks on the key row's
  unique index until the first commits, then replays its stored answer.
* If the first request crashes, its transaction rolls back, the key disappears, and the retry
  runs from scratch. There is never a key without its effects, or effects without a key.
* The same key with a different request is a client bug and is refused.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from festrush.db import Conn, Pool, one

MAX_KEY_LENGTH = 255


@dataclass(frozen=True)
class Replayable:
    status: int
    body: dict[str, Any]
    replayed: bool = False
    mismatch: bool = False


def request_hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def run_idempotent(
    pool: Pool,
    user_id: int,
    key: str | None,
    fingerprint: str,
    work: Callable[[Conn], tuple[int, dict[str, Any]]],
) -> Replayable:
    """Run ``work`` once per (user, key). Without a key, just run it.

    ``work`` receives the open connection and returns ``(http_status, json_body)``. Its database
    changes commit together with the stored response.
    """
    if key is None:
        with pool.connection() as conn:
            status, body = work(conn)
        return Replayable(status, body)

    with pool.connection() as conn:
        claimed = one(
            conn,
            "INSERT INTO idempotency_keys (user_id, key, request_hash, response_status, response_body)"
            " VALUES (%s, %s, %s, 0, '{}'::jsonb) ON CONFLICT DO NOTHING RETURNING key",
            (user_id, key, fingerprint),
        )
        if claimed is not None:
            status, body = work(conn)
            conn.execute(
                "UPDATE idempotency_keys SET response_status = %s, response_body = %s::jsonb"
                " WHERE user_id = %s AND key = %s",
                (status, json.dumps(body), user_id, key),
            )
            return Replayable(status, body)

        # Somebody else's transaction owns this key and has committed (we waited for it).
        stored = one(
            conn,
            "SELECT request_hash, response_status, response_body FROM idempotency_keys"
            " WHERE user_id = %s AND key = %s",
            (user_id, key),
        )
    if stored is None:  # pruned between the conflict and the read; treat as a fresh request
        return run_idempotent(pool, user_id, key, fingerprint, work)
    if stored["request_hash"] != fingerprint:
        return Replayable(422, {"error": "idempotency_key_reused"}, mismatch=True)
    return Replayable(stored["response_status"], stored["response_body"], replayed=True)


def prune(conn: Conn, hours: int = 24) -> int:
    cur = conn.execute(
        "DELETE FROM idempotency_keys WHERE created_at < now() - make_interval(hours => %s)",
        (hours,),
    )
    return cur.rowcount
