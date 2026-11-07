# Research

_Researched 2026-11-08. GitHub numbers come from the GitHub API on that day (`gh api repos/<owner>/<repo>`). Documentation links were checked to resolve on the same day._

The question this document answers: **what should a rebuilt fest-ticketing app do about the
rush for scarce seats, which approaches exist, and which are worth building and measuring?**

Short answer: the interesting part is not the web app, it is the few hundred seats that thousands
of students want at the same moment. That is a contention problem inside the database, so the
project should (1) pin down correctness with invariants a script can check, (2) compare the
standard inventory strategies under the same load, and (3) split the monolith only where a
measurement shows a split helps.

## 1. Similar open-source projects

| Project | What it is | License | Stars | Last push |
| :-- | :-- | :-- | --: | :-- |
| [pretix](https://github.com/pretix/pretix) | Ticket shop for conferences, festivals, concerts | see repo (`NOASSERTION`) | 2,562 | 2026-11-06 |
| [Hi.Events](https://github.com/HiEventsDev/Hi.Events) | Event management and ticket selling (PHP) | see repo (`NOASSERTION`) | 4,147 | 2026-11-07 |
| [alf.io](https://github.com/alfio-event/alf.io) | Ticket reservation system for conferences and meetups (Java) | GPL-3.0 | 1,629 | 2026-11-07 |
| [Open Event Server](https://github.com/fossasia/open-event-server) | Organizer server behind eventyay | GPL-3.0 | 3,002 | 2026-04-02 |
| [Attendize](https://github.com/Attendize/Attendize) | Laravel ticket selling platform | see repo (`NOASSERTION`) | 4,283 | 2024-08-20 |

What this tells us:

- **These are complete products for organizers**: ticket types, taxes, check-in, payouts, plug-in
  payment providers. Rebuilding that is not the goal and would not be a credible portfolio piece.
- **They are orientation, not a benchmark.** We did not audit their inventory code, so nothing in
  this document claims they oversell or don't. What they confirm is that "reservation with a time
  limit" (alf.io describes itself as a *reservation* system) is the standard shape of the problem.
- **The gap this project can fill is evidence**: a small, readable system that shows its
  invariants holding under a load you can rerun, and shows where each design choice costs or saves
  latency. A fest is a good scale for that: hundreds of seats, thousands of users.

## 2. Selling a seat exactly once

The invariant: for every event, `confirmed + held <= capacity`, always, even with thousands of
simultaneous requests and crashed workers.

| Strategy | How it works | Strength | Weakness | Plan |
| :-- | :-- | :-- | :-- | :-- |
| **Naive read-check-write** | `SELECT` count, compare, `INSERT` | Simple | Two requests read the same count, both insert: oversells (v1's shape, see [audit](audit.md) A4, A6) | **Baseline**: shipped as a strategy so the oversell can be demonstrated, not just claimed |
| **Pessimistic row lock** | `SELECT ... FOR UPDATE` on the event row, then count and insert in one transaction | Obviously correct | Every buyer of the same event queues on one row; throughput is bounded by lock hold time ([PostgreSQL explicit locking](https://www.postgresql.org/docs/current/explicit-locking.html)) | Benchmark |
| **Atomic conditional update** | `UPDATE events SET sold = sold + 1 WHERE id = $1 AND sold < capacity`; zero rows updated means sold out | One statement, shortest lock; no read-modify-write window | Still a single hot row; says nothing about *which* seat | Benchmark; expected default for general admission |
| **Per-seat rows with `SKIP LOCKED`** | `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` over free seat rows | Concurrent buyers take different rows instead of waiting on one ([SELECT, locking clause](https://www.postgresql.org/docs/current/sql-select.html); [2ndQuadrant on SKIP LOCKED](https://www.2ndquadrant.com/en/blog/what-is-select-skip-locked-for-in-postgresql-9-5/)) | A shopper cannot choose a specific seat; the view of "what's free" is intentionally incomplete while locks are held | Benchmark; the natural fit for an "any seat" button |
| **Chosen seat with `NOWAIT` or `SKIP LOCKED` on that row** | Lock the clicked seat's row; if locked, answer "someone else is holding it" | Real seat maps | Many clicks on a few popular seats | Used by the seat map |
| **Optimistic version column** | `UPDATE ... WHERE version = $v` and retry | No locks held | Retry storms under heavy contention | Documented, not built |
| **Serializable isolation with retry** | Let the database detect conflicts and abort one transaction ([transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html)) | Correct by construction for read-then-write code | Applications must retry serialization failures; abort rate rises with contention | Benchmark as one row, to see the cost |
| **Redis counter or Lua script** | Decrement an in-memory counter | Very fast | A second source of truth that can drift from the database; extra infrastructure | Rejected for the core path; may be revisited for the waiting room |

Decision to test: for general admission the atomic conditional update should be fastest and
simplest; `SKIP LOCKED` should win for seat selection because buyers do not queue on one row. The
benchmark exists to confirm or refute that. Results will be reported even if the simple option wins.

## 3. Retries must be safe: idempotency keys

Browsers double-submit, mobile networks retry, and our own client will retry on a timeout. The
standard answer for write endpoints is an `Idempotency-Key` header:

- [Stripe's documentation](https://docs.stripe.com/idempotency) describes the model: the server
  saves the status and body of the first request made with a key and returns the same result for
  retries; reusing a key with different parameters is an error; keys can be pruned after 24
  hours; only `POST` takes them, because `GET` and `DELETE` are idempotent by definition.
- The [IETF HTTP API working group draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/)
  specifies the same header; it is a draft, not a standard.
- [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html) defines which HTTP methods are
  idempotent. v1 registers through `GET`, which both breaks the semantics and makes prefetching
  or a stray link a write ([audit](audit.md) S2).

Design: a table `(key, user, request_hash, status, response)` with a unique constraint on
`(user, key)`. A repeat with the same hash replays the stored response. The same key with a
different hash gets `422`. A request that is still in flight gets `409` with `Retry-After`.
Insert-first (`INSERT ... ON CONFLICT DO NOTHING`) makes the claim itself atomic.

Retries from our own clients and load generator use exponential backoff with jitter, per the
[AWS Builders' Library](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/),
so a retry storm does not become the outage.

## 4. Payments without real payments

There will be no real money and no paid API. A **mock payment provider** is a small separate
service that behaves like a real one in the ways that matter for correctness: it is slow, it
fails sometimes, it times out, and it can confirm a payment *after* the caller gave up.

The order moves through an explicit state machine, enforced by a `CHECK` constraint and a
transition table, so an illegal move cannot be written:

```
held ──▶ payment_pending ──▶ paid ──▶ confirmed
  │            │                │
  ▼            ▼                ▼
expired   payment_failed     refunded (compensation)
```

The hard cases to test are the ones that happen in real systems: the hold expires while the
payment is in flight; the provider's webhook arrives twice; the provider confirms after the
reservation was released. Each becomes a named test with an invariant ("no seat is confirmed
without a paid order; no paid order loses its seat without a refund").

## 5. Confirmations and emails: outbox and a queue

Writing the order and publishing "order confirmed" are two separate systems; if the process dies
between them, one happened and the other didn't (the dual-write problem). The
[transactional outbox](https://microservices.io/patterns/data/transactional-outbox) fixes this:
the event is inserted into an `outbox` table in the same transaction as the order, and a relay
publishes it afterwards. The pattern's own description notes the relay can publish a message more
than once, so consumers must be idempotent, which ties back to section 3.

Where the queue lives:

| Option | For | Against |
| :-- | :-- | :-- |
| **PostgreSQL as the queue** (`FOR UPDATE SKIP LOCKED` workers; or [pgmq](https://github.com/pgmq/pgmq), PostgreSQL license, 5,390 stars; or [Procrastinate](https://github.com/procrastinate-org/procrastinate), MIT, 1,445 stars) | One system to run; the outbox row and the job can be the same row, committed atomically; no extra failure mode | Not meant for very high throughput; polling latency |
| **Redis Streams** ([docs](https://redis.io/docs/latest/develop/data-types/streams/)) | Consumer groups, fast | Another system; a durable outbox is still needed to avoid dual writes |

Recommendation: use PostgreSQL, hand-written with `SKIP LOCKED`. The workload is hundreds of
messages a second at most, and the code is short enough to read and test. A hand-written worker
also makes the crash tests simple: kill it mid-job and see the job run again.

## 6. Launch spikes: waiting room and rate limiting

Two different controls solve two different problems:

- **Rate limiting** protects the service from any single client hammering it. nginx's
  [`limit_req`](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html) does this at the
  gateway with a leaky bucket.
- **A virtual waiting room** protects the *checkout* from too many simultaneous legitimate users.
  Commercial products admit visitors first-in-first-out, at random or by lottery once a threshold
  is crossed ([Cloudflare Waiting Room](https://developers.cloudflare.com/waiting-room/about)).
  Admission order is the main design choice: FIFO rewards fast connections and bots; a lottery
  within a short opening window is fairer.

Both exist to keep the system inside the range where it still works. The
[SRE book on overload](https://sre.google/sre-book/handling-overload/) and
[cascading failures](https://sre.google/sre-book/addressing-cascading-failures/) make the same
point: serve fewer people well instead of everyone badly.

Plan: a PostgreSQL-backed waiting room (admission tokens, a position endpoint, a
server-sent-events stream) with FIFO and lottery admission, so the difference can be compared in
a test. No Redis until a measurement shows PostgreSQL can't do it.

## 7. Splitting a monolith: only where it pays

- Martin Fowler's [Strangler Fig](https://www.martinfowler.com/bliki/StranglerFigApplication.html)
  and Microsoft's [pattern page](https://learn.microsoft.com/azure/architecture/patterns/strangler-fig)
  describe replacing a system piece by piece behind a routing layer, not a rewrite.
- Sam Newman's *Building Microservices*, chapter 5, starts from "seams": places where code can be
  separated, and advocates splitting gradually where it gives the most benefit.
- Shopify chose a [modular monolith](https://shopify.engineering/shopify-monolith) over
  microservices to get boundaries without the cost of a distributed system. That is one
  company's choice, not a rule.

This repository is named after the monolith, so it should tell the honest story. The hypothesis
the benchmarks will test:

| Candidate to extract | Expected to help? | Why |
| :-- | :-- | :-- |
| **Inventory service** | **No** | The bottleneck is the row lock in the database. Moving the code behind a network call adds latency and does not remove the contention. This is the split that looks natural and probably isn't worth it. |
| **Mock payment provider** | **Yes** | It is external by nature. Making it a separate process with its own latency and failures is what forces timeouts, retries and idempotency to be real. |
| **Notification worker** | **Yes** | Off the request path; a slow or failing email must not slow checkout. Measurable as p95 latency with the worker slowed down. |
| **Catalog and seat-map reads** | **Maybe** | Read-heavy and cacheable; the gateway or a read replica may remove the load without a new service. |

Each row becomes an experiment with a before and after (see [roadmap](roadmap.md), phase 3). If a
split makes things worse, the write-up says so and the code stays in the monolith.

## 8. Observing it

- **OpenTelemetry** ([Python docs](https://opentelemetry.io/docs/languages/python/)) for traces
  and metrics, exported to a self-hosted collector. No SaaS, no telemetry leaving the machine.
- **Prometheus** for metrics (67,374 stars, Apache-2.0). Latency as histograms, so p95 comes from
  buckets ([guidance](https://prometheus.io/docs/practices/histograms/)), not averages.
- **Grafana** for dashboards (AGPL-3.0; run unmodified as a container, not redistributed in this
  repository) and **Jaeger** for trace viewing (Apache-2.0, 23,386 stars).
- The app's own **System Lens** reads the same data for the UI (traces, lock waits, queue depth),
  mirroring the idea of the SQL Lens in the previous project, so the observability is a feature
  you can see in the product and not only a dashboard on another port.

## 9. Load and chaos testing

[Locust](https://docs.locust.io/en/stable/) (MIT, 28,268 stars) is already the course's tool and
fits: plain Python user classes, a headless mode for CI, and CSV output. Scenarios:

1. **Spike**: N users, M seats, all start within one second. Assert zero oversells.
2. **Retry storm**: every request is sent twice with the same idempotency key. Assert one order.
3. **Soak**: a steady rate for ten minutes. Watch for queue growth and memory.
4. **Chaos**: kill the worker, slow or fail the payment provider, restart Postgres mid-run.

After every run a checker script queries the database for the invariants (section 2 and 4) and
fails the run if any is violated. CI runs a small version (for example 300 users, 30 seats); the
README quotes a large run (for example 5,000 users, 200 seats) with the hardware and the exact
command, and only numbers a script produced.

## 10. A demo that needs no installation

The previous project ran the real database in the browser with PGlite. That does not carry over
here, and it is worth saying why: PGlite runs PostgreSQL in single-user mode, so by default it
accepts one connection at a time (see the [PGlite socket docs](https://pglite.dev/docs/pglite-socket),
with optional multiplexing added in v0.4). Row-lock contention between concurrent buyers is the
thing this project exists to show, and a single-connection database cannot show it.

Honest options:

1. **One command**: `docker compose up` runs the whole system locally (PostgreSQL, app, worker,
   gateway, mock provider, dashboards). This is the real thing.
2. **A recorded replay on GitHub Pages**: the UI plays back a trace captured from a real run
   (the seat map filling, the queue draining, the System Lens). It must be labeled as a replay,
   not a live system. Another repository of yours, CC_Lab-6, already ships a `replay.json`
   dashboard of this kind.
3. **An in-browser simulator** that reimplements the strategies in JavaScript would be dishonest
   as a demonstration of PostgreSQL locking, so it is not proposed.

## 11. Risks

| Risk | Mitigation |
| :-- | :-- |
| Benchmark numbers vary by machine | Commit the raw output with the hardware description; state ranges and the exact command; CI asserts invariants, not speeds |
| A real-time UI (SSE, many open connections) can overload a single worker | Measure it as part of the soak test; cap connections at the gateway |
| Scope: this list is bigger than a three-day build | The [roadmap](roadmap.md) ranks features and cuts to a core that is finished first |
| Windows lockfile problems for any Node tooling | Generate and verify lockfiles in a Linux container before pushing |
| The AGPL license on Grafana | Run it unmodified from its official image; do not vendor it |
