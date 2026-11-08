# Modernization summary

v1 was one FastAPI app with SQLite, plaintext passwords and no protection against two students
taking the last seat. v2 keeps the idea and rebuilds the rest. The numbers below are copied from
[bench/results/SUMMARY.md](../bench/results/SUMMARY.md), which `bench/summarize.py` regenerates from
the raw runs in [bench/results/](../bench/results/).

| Phase | What changed | Read |
| :-- | :-- | :-- |
| 1 | PostgreSQL, hashed passwords, sessions, CSRF, tests, CI | [audit](audit.md) |
| 2 | Seat holds, four claiming strategies, payments, idempotency, outbox | [concurrency](concurrency.md) |
| 3 | Gateway, payment service, waiting room, metrics, traces, chaos runs | [architecture](architecture.md) |
| 4 | Live seat map, System Lens, Playwright and axe checks | [START_HERE](../START_HERE.md) |
| 5 | API reference, community files, release | [API](api.md) |

## What was measured

- In the 300-student, 30-seat run, `naive` sold 40 seats (range 37 to 44 over 3 runs), so 10 were
  oversold; `row_lock`, `atomic_update` and `skip_locked` sold exactly 30 with 0 oversold.
- At 5,000 students and 200 seats the same held: `naive` oversold 10 (range 4 to 11); the three safe
  strategies oversold 0.
- With every write sent twice under the same key (10,800 writes), 200 seats sold, 200 orders and 200
  charges at the provider resulted, and no duplicate was answered differently.
- The web tier, not the database, limited throughput: requests per second rose with the number of
  web processes (E0 table in the summary).

## What was not done

The inventory was not split into its own service, because the measurements did not justify it
([architecture](architecture.md)). The front end is plain JavaScript rather than React. There is no
recorded replay demo or animated walkthrough.
