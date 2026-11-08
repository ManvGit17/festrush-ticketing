# Architecture: splitting only where the measurements say so

FestRush started as a small monolith. The plan for this phase was to measure it, then split
it into services only where a split pays for itself, and to say honestly where it does not. This
page is the record: what the system looks like now, each decision with the evidence for it, and
what was not measured. Every table is printed by `python bench/summarize.py` from the raw runs in
[`bench/results/`](../bench/results/); the commands to reproduce them are at the end.

## The system now

```
 students ──▶ nginx gateway ──▶ web app (FastAPI, several processes) ─────▶ PostgreSQL 18
              rate limit,        accounts, seat claims, orders,            data, outbox,
              1 s read cache,    waiting room, JSON API, /metrics          queue, idempotency keys
              /metrics hidden            │      ▲
                                         │      │ HTTP, short timeouts, trace context
                                         ▼      │
                                  payments service (mock provider, its own process)
                                         
                         worker ── releases expired holds, delivers the outbox,
                                   settles unfinished payments, admits the queue

 Prometheus ◀── web, worker      Jaeger ◀── traces from web, worker, payments      Grafana
```

`docker compose up` starts the database, the app, the worker, the payments service and the
gateway (<http://localhost:8088>). `docker compose --profile observability up -d` adds
Prometheus, Jaeger and a provisioned Grafana dashboard (<http://localhost:3300>); set
`FESTRUSH_OTLP_ENDPOINT=http://jaeger:4318` to send traces.

What is a separate process, and why:

| Part | Separate? | Decided by |
| :-- | :-- | :-- |
| Web app | the public face; stays a modular monolith | D1, D5 |
| Gateway (nginx) | yes: the front door and the seam for later splits | D4 |
| Worker | yes, since phase 2 | D3 |
| Payment provider | yes | D2 |
| Inventory (seat claims) | **no** | D1 |

## Decisions

### D1. Seat claiming stays inside the monolith

**Hypothesis (from the research):** splitting inventory out would not help, because the limit is
the row in the database and a network call adds latency without removing it.

**Evidence.** The experiment this decision was meant to rest on, a real inventory service
compared with the in-process code, **was not built**, so there is no before-and-after for it. What
exists instead:

- Phase 2 found the four seat-claiming strategies close in speed
  ([concurrency.md](concurrency.md)), so the seat lock is not what limits throughput.
- E0 below finds the web tier is what limits it, and that it scales with processes.
- A service would have to own the idempotency key as well as the seat, because today the key, the
  seat claim and the stored answer commit in one transaction. Across a network that guarantee
  becomes a distributed one.

**Decision:** keep it in the monolith. This rests on reasoning and on indirect evidence, not on a
direct measurement, and should be revisited if the web tier stops being the limit.

### D2. The payment provider is its own service

**Hypothesis:** external by nature, so it should be a separate process; expected to help.

| Variant | Runs | Whole run (s) | Reserve p50 (s) | Order p50 (s) | Pay p50 (s) | Pay p95 (s) | Page refresh p50 (s) | Oversold |
| :-- | --: | --: | --: | --: | --: | --: | --: | --: |
| `inprocess` | 2 | 5.6 (5.5-5.6) | 2.1 (2.1-2.1) | 2.3 (2.3-2.4) | 1.0 (0.9-1.0) | 2.3 (2.3-2.3) | n/a | 0 (0-0) |
| `service` | 2 | 6.3 (6.1-6.5) | 2.3 (2.3-2.3) | 2.4 (2.3-2.5) | 1.6 (1.6-1.6) | 3.0 (2.8-3.3) | n/a | 0 (0-0) |

**What the evidence shows.** The split costs speed: with a 300 ms provider, the median pay
request went from about 1.0 s to about 1.6 s, and the whole run took about 0.7 s longer. It
bought no throughput. What it bought is a failure domain, which the chaos runs below depend on:
killing the payments service mid-run (second row of the chaos table) is something an in-process
provider cannot even be asked to do.

The split also surfaced a real bug that the in-process version hid. When the service was down,
every payment in flight was marked failed and the seat left unsold. The reconciler now sends a
charge the provider never saw again, with the same idempotency key, so it is taken at most once
(`test_a_provider_outage_during_payment_delays_the_sale_without_losing_it`), and it never sends
one for an order that has already expired.

**Decision:** keep the service, because the isolation is what makes the failure handling real.
It is configured by `FESTRUSH_PAYMENTS_URL`; leave it unset for the in-process mock.

### D3. Confirmations go through the outbox and a worker

**Hypothesis:** off the request path, so a slow email cannot slow checkout.

| Variant | Runs | Whole run (s) | Reserve p50 (s) | Order p50 (s) | Pay p50 (s) | Pay p95 (s) | Page refresh p50 (s) | Oversold |
| :-- | --: | --: | --: | --: | --: | --: | --: | --: |
| `outbox` | 2 | 6.2 (6.1-6.4) | 2.5 (2.4-2.6) | 2.8 (2.5-3.1) | 1.0 (0.8-1.1) | 2.5 (2.1-3.0) | n/a | 0 (0-0) |
| `inline` | 2 | 6.9 (6.6-7.2) | 2.3 (2.1-2.5) | 2.6 (2.2-3.0) | 2.0 (1.8-2.2) | 3.4 (3.3-3.5) | n/a | 0 (0-0) |

**What the evidence shows.** With a 300 ms message, sending it inside the pay request roughly
doubled the median pay time (about 1.0 s to about 2.0 s) and stretched the whole run. Inline also
loses the safety the outbox gives: the message is sent in the same transaction as the order, so a
crash can lose it or send it twice.

**Decision:** outbox and worker, as built in phase 2. The inline option stays in the code
(`FESTRUSH_NOTIFY_INLINE`) only so the comparison can be rerun.

### D4. A gateway in front, with rate limiting; the read cache did not earn its place

**Hypotheses:** rate limiting sheds load before it reaches Python; a one-second cache absorbs
students refreshing the page.

| Setup | Whole run (s) | Reserve attempt p50 / p95 (s) | Order p50 (s) | Pay p50 (s) | Rejected with 429 | Seats sold | Oversold |
| :-- | --: | --: | --: | --: | --: | --: | --: |
| no gateway | 44 | 24.8 / 35.1 | 22.9 | 11.1 | 0 | 200 | 0 |
| gateway, 300 claims/s | 48 | 0.0 / 20.1 | 17.0 | 22.2 | 47,831 | 200 | 0 |
| gateway, 100 claims/s | 48 | 0.0 / 0.4 | 6.9 | 15.2 | 157,141 | 200 | 0 |

| Variant | Runs | Whole run (s) | Reserve p50 (s) | Order p50 (s) | Pay p50 (s) | Pay p95 (s) | Page refresh p50 (s) | Oversold |
| :-- | --: | --: | --: | --: | --: | --: | --: | --: |
| `nocache` | 2 | 7.5 (7.4-7.5) | 2.0 (2.0-2.1) | 2.3 (2.2-2.4) | 0.9 (0.8-0.9) | 3.0 (3.0-3.1) | 0.00 (0.00-0.00) | 0 (0-0) |
| `cache` | 2 | 7.8 (7.5-8.2) | 1.9 (1.9-2.0) | 1.9 (1.8-1.9) | 1.2 (1.1-1.2) | 3.1 (3.1-3.1) | 0.00 (0.00-0.00) | 0 (0-0) |

**What the evidence shows.**

- *Rate limiting* at 100 claims a second made every accepted reserve fast (95th percentile 0.4 s
  against 35 s) and cut the median order time from 23 s to 7 s. But the whole run took the same
  time, 48 s against 44 s, because capacity is capacity: the rejected students retry. The pay step
  did not improve, and the roughly 157,000 retries cost CPU on the same machine, so this
  comparison is confounded. Rate limiting protects the checkout steps from a stampede; it does not
  make the stampede finish sooner.
- *The read cache* showed no measurable benefit here. A page refresh already took under 10 ms,
  because a read costs far less than a seat claim, and the other numbers moved within noise. The
  cache is harmless (it serves data at most a second old, and claiming a seat never trusts it) but
  this project cannot claim it helps.

**Decision:** keep the gateway for rate limiting, `/metrics` protection and login throttling.
Keep the cache configured at one second but make no claim for it.

### D5. A waiting room, which does not rescue a simultaneous arrival

**Hypothesis:** admitting students at a rate the system can serve beats letting all of them wait.

| Admission rate | Whole run (s) | Join p50 (s) | Status poll p50 (s) | Reserve p50 (s) | Status polls | Seats sold | Oversold |
| --: | --: | --: | --: | --: | --: | --: | --: |
| 100/s | 119 | 37.7 | 25.5 | 24.6 | 5,000 | 200 | 0 |
| 300/s | 119 | 32.7 | 32.0 | 25.9 | 5,000 | 200 | 0 |

**What the evidence shows, which I did not expect.** The waiting room did not help against 5,000
students arriving in the same second. Both runs ran to the two-minute cap (119 seconds). Joining the queue costs a
request just like reserving does, so the 5,000 joins were themselves the stampede: median join
time was over 30 seconds. A queue inside the application cannot protect the application from the
arrival of the queue. Real waiting rooms sit at the edge (a CDN) for this reason.

What it does do, and what the tests check: admits at a steady rate, in order (first come, first
served) or at random (lottery, fairer against scripts that join in the first millisecond), and
refuses seat claims from anyone not admitted. It should help when arrivals are spread over time
and the system is overloaded by their sum. That case was not benchmarked.

**Decision:** keep it, off by default (`FESTRUSH_WAITING_ROOM_ENABLED`), and describe it as
untested against the worst case. Moving the queue's cheap part into the gateway is the next thing
to try.

### D6. Observability is built in, not bolted on

Prometheus metrics (request latency histograms by route, seat-claim outcomes, payment results,
outbox backlog and age, queue size, active holds, lock waits) are served at `/metrics` and
scraped from the app and the worker. OpenTelemetry traces cover every request, SQL statement,
outbox event and call to the payments service, with the trace context carried across the HTTP
call, so one trace shows a checkout from the web request through `payments.charge` to the
provider's own database work (checked in Jaeger). The Grafana dashboard is provisioned from
[`observability/`](../observability/). Tracing is off unless an endpoint is set; nothing leaves the
machine.

## E0. What limits throughput?

Phase 2 left this open: all four seat strategies topped out near 150 requests per second.

| Web processes | Threads each | Students | Requests/s overall | Reserve p50 (s) |
| --: | --: | --: | --: | --: |
| 1 | 40 | 1,500 | 114 | 8.3 |
| 4 | 10 | 5,000 | 214 | 12.7 |
| 4 | 40 | 5,000 | 260 | 9.2 |
| 8 | 10 | 5,000 | 320 | 8.2 |
| 16 | 5 | 5,000 | 404 | 6.3 |

What the measurements show, and what they do not:

- **The load generator was part of the ceiling.** The same 5,000-student spike driven by k6 ran
  roughly twice as fast as with Locust, so the phase 2 throughput figures understate the system.
  (The relative comparison between strategies in phase 2 is unaffected: all used the same driver.)
- **It is not the database.** During a run the database sessions were almost all waiting for the
  application to send the next command, and the database server was far from saturated.
- **It is the Python web tier, and it scales with processes.** Requests per second rose from about
  114 with one process to about 404 with sixteen. Fewer threads per process did not help.
- **What I did not find.** Why a request costs 10 to 15 ms of web-tier time when the handler
  itself takes about 2.3 ms (profiled in-process with `bench/profile_http.py`). It is the ASGI
  stack, middleware and thread hand-offs, not any one function I could point at.

The practical lever is more web processes behind the gateway. That needs no code change.

## Chaos: break it while it is selling

300 students buy 30 seats while one thing is broken and then put back. After each run the
invariants are checked ([concurrency.md](concurrency.md)).

| Fault | What was broken | Students | Seats sold | Messages | Failed requests | Oversold | Invariants | App back after |
| :-- | :-- | --: | --: | --: | --: | --: | :-- | --: |
| kill the worker | killing background; starting background again | 300 | 30 | 30 | 0 | 0 | all hold | n/a |
| kill the payments service | killing payments; starting payments again | 300 | 30 | 30 | 0 | 0 | all hold | n/a |
| stop PostgreSQL for 5 s | stopping PostgreSQL; starting PostgreSQL again | 100 | 20 | 20 | 34 | 0 | all hold | 0.0 s |
| slow, lossy provider | 800 ms latency, 20% lost answers, 10% declines | 300 | 27 | 27 | 0 | 0 | all hold | n/a |

Every run ends with every seat sold exactly once, every confirmation delivered, and every
invariant intact. Two of these runs found real bugs first:

1. **Provider outage abandoned the sale** (described under D2). Fixed and covered by a test.
2. **The database pool did not recover after PostgreSQL restarted.** Under load the web
   processes returned about 2,500 "couldn't get a connection" errors and never came back,
   because a connection attempt to a database that is down could hang instead of failing. The
   pool now checks each connection as it hands it out, fails a connection attempt after three
   seconds, and answers 503 after five seconds of waiting, so it recovers as soon as the database
   does. The 34 failed requests in the table are the ones that arrived while the database was
   down. (The recovery column is 0.0 s because the app was already answering when the check ran;
   it does not measure the outage itself.)

A third problem was in the test harness, not the app: the first kill-the-worker run reported
violated invariants because the harness never restarted the worker. That is now fixed.

## What this does not show

- **One laptop.** The load generator, the web processes and the worker share CPUs, and PostgreSQL
  runs in a container on a virtual disk. The same configuration varied by up to a factor of two
  between runs on this machine, and the experiment tables use two repetitions each.
- **A simultaneous arrival is the worst case on purpose.** Real launches ramp up.
- **Inventory was never split (D1)**, and the waiting room was never benchmarked on a ramp (D5).
- **The mock provider and the email "delivery" are simulations**, with injected latency and failures.

## Reproducing the numbers

```bash
bench/run_matrix.sh          # the phase 2 strategy comparison (about 25 minutes)
bench/run_experiments.sh     # E2, E3, E4 (about 12 minutes)
python bench/summarize.py    # prints every table above from bench/results/
```

The other runs use `bench/run.py` directly, for example (inside Docker, so the environment is
the same for everyone):

```bash
docker compose --profile bench run --rm bench --strategy atomic_update --driver k6 \
  --users 5000 --seats 200 --gateway --claim-rate 100 --claim-burst 50     # the gateway
docker compose --profile bench run --rm bench --strategy atomic_update --driver k6 \
  --users 5000 --seats 200 --waiting-room --admit-per-second 100           # the waiting room
uv run python bench/run.py --strategy atomic_update --users 300 --seats 30 \
  --chaos restart-db --chaos-at 2 --chaos-for 5                            # chaos (needs docker compose db)
```
