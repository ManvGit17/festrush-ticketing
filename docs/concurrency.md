# Selling a seat exactly once

How FestRush stops a rush of students from overselling an event, how that was tested, and what
the measurements say. Every number on this page is printed by `bench/summarize.py` from the raw
run files in [`bench/results/`](../bench/results/), and you can produce your own (see
[Reproducing the numbers](#reproducing-the-numbers)).

## The problem

A few hundred seats, thousands of students, one moment. Three things go wrong in a naive system:

1. **Two requests take the last seat.** Each reads "one seat left", both insert.
2. **A retry does the work twice.** A double click, a flaky network or an eager client sends the
   same request again, and the student holds two seats or pays twice.
3. **Half-finished work.** A payment succeeds but the seat is gone, or the seat is confirmed and
   the process dies before anyone is told.

The audit found v1 had all three ([audit.md](audit.md): A4, A6, D1): no capacity, no uniqueness,
and 100 simultaneous clicks created 100 registrations.

## The model

```
 hold a seat ──▶ order ──▶ pay ──▶ confirmed ──▶ message sent
   (expires)    (pending)  (provider)  (outbox)     (worker)
```

| Step | What is stored | Rule enforced by the database |
| :-- | :-- | :-- |
| Hold | `reservations` row, `held`, with `expires_at` | One active (held or confirmed) reservation per student per event: a partial unique index |
| Order | `orders` row `pending`, one `payments` row, the holds attached | One payment per order; `provider_key` unique |
| Pay | Provider is called with **no transaction open**; the answer is applied in one transaction | Order and reservation status moves are checked by triggers, so an illegal move cannot be written |
| Confirm | Order `confirmed`, reservations `confirmed`, an `outbox` row, all in that transaction | The message is queued if and only if the order is confirmed |
| Message | The worker writes a `mailbox` row (no real email exists) | Unique `(order, kind)`, so a repeated delivery sends nothing twice |

Order states: `pending → paid → confirmed`, `pending → payment_failed`, `pending | payment_failed →
expired`, `expired → refunded`. Nothing else is accepted by the trigger.

## Four ways to claim a seat

All four insert the same reservation row. They differ in how they stop two requests from taking
the last seat ([inventory.py](../src/festrush/inventory.py)). Pick one with
`FESTRUSH_INVENTORY_STRATEGY`.

| Strategy | The guard | Why it can or cannot be wrong |
| :-- | :-- | :-- |
| `naive` | Count the active reservations, compare with capacity, then insert | **Can oversell.** Two requests read the same count. The baseline, kept so the failure can be shown |
| `row_lock` | `SELECT ... FROM events WHERE id = $1 FOR UPDATE`, then count and insert | Claims for one event run one at a time, so the count cannot change under you |
| `atomic_update` | `UPDATE events SET claimed = claimed + 1 WHERE id = $1 AND claimed < capacity`; no row updated means sold out | The database decides in one statement. `claimed` also has `CHECK (claimed <= capacity)` |
| `skip_locked` | `SELECT id FROM seats WHERE event_id = $1 AND reservation_id IS NULL ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED` | Each request takes a different free seat row. A request that finds none checks whether free-but-locked seats exist before saying "sold out" |

The derived state each one uses (`events.claimed`, `seats.reservation_id`) is rebuilt from the
reservations by `SELECT rebuild_inventory()` (or `festrush rebuild-inventory`) when you switch
strategy.

Two details worth knowing:

- Every safe strategy inserts the reservation first where it can, so a duplicate request is
  refused by the unique index before it touches the shared seat state.
- `skip_locked` has a real weakness: while seats are locked by in-flight claims that may yet roll
  back, a request can find none free. The code retries briefly and then answers `busy` instead of
  claiming "sold out" wrongly. A busy answer is rare, and the student can simply try again.

## Retries are safe

Writes accept an `Idempotency-Key` header ([idempotency.py](../src/festrush/idempotency.py)), the
same idea as Stripe's. The key row, the work and the stored response commit in **one**
transaction:

- A duplicate that arrives while the first request is still running waits on the key's unique
  index, then returns the first request's answer (with `Idempotent-Replayed: true`).
- If the first request crashes, its transaction rolls back, the key disappears, and the retry
  runs fresh. There is never a key without its effects, or effects without a key.
- The same key with a different request is refused (422).

The web form for checkout carries such a key in a hidden field, so a double click pays once.
`POST /api/orders/{id}/pay` needs no key: only one caller can flip the payment from `initiated` to
`processing`, so only one reaches the provider.

## Payments without real payments

[`mockpay.py`](../src/festrush/mockpay.py) is a stand-in for a payment provider with its own
ledger table. It is idempotent by key, can be slow, can decline, and can lose the response after
taking the money. The hard cases are tests, not hopes:

| Case | What happens | Test |
| :-- | :-- | :-- |
| The provider declines | Order `payment_failed`; the holds stay and the student can try again | `test_a_declined_payment_keeps_the_hold_and_allows_a_second_try` |
| The answer is lost (timeout) | Payment `unknown`; the worker asks the provider later and confirms the order | `test_a_lost_response_is_settled_by_the_worker` |
| The web process dies before calling the provider | The provider has no record, so the payment is `failed`, not charged | `test_a_charge_the_provider_never_saw_is_failed_not_taken` |
| The hold expires while payment is in flight, then the money arrives | The order is `expired`, so the money is **refunded**; nobody is left paid without a seat | `test_late_money_after_expiry_is_refunded` |
| Ten simultaneous pay calls | One provider call | `test_ten_simultaneous_payments_reach_the_provider_once` |

A hold with a payment in flight gets a grace period past its expiry (`payment_grace_seconds`)
before it is released.

## The worker survives being killed

[`worker.py`](../src/festrush/worker.py) releases expired holds, relays the outbox and reconciles
payments. It claims outbox rows with `FOR UPDATE SKIP LOCKED` and writes the message and the
"processed" mark in one transaction. A worker killed part-way leaves its rows pending and the next
one picks them up; the message insert is idempotent as a second line of defence.
`test_killing_the_worker_mid_batch_loses_no_confirmation` starts a real worker process, kills it
while it holds part of the outbox, starts another, and checks every order has exactly one message.

## Invariants

After every load run, [`invariants.py`](../src/festrush/invariants.py) queries the database for
these properties. They read reservations, orders and payments directly, never the counters a
strategy keeps, so a strategy cannot pass by agreeing with its own bookkeeping.

| Invariant | Meaning |
| :-- | :-- |
| `no_oversell` | Active reservations never exceed an event's capacity |
| `one_seat_per_user_per_event` | No student holds two seats at one event |
| `confirmed_seats_are_paid` | Every confirmed reservation belongs to a confirmed order with a succeeded payment |
| `paid_orders_have_seats` | A confirmed order's seats are all confirmed |
| `no_double_charge` | Money the provider holds and has not refunded belongs to a succeeded payment |
| `refunds_match_lost_seats` | A succeeded payment is never attached to an order that did not get its seats |
| `one_confirmation_per_order` | Once the outbox is drained, each confirmed order has exactly one event and one message |
| `seats_match_reservations` | No seat row points at a reservation that is no longer active |

`festrush check` runs them against any database, and every one of them has a test that plants the
damage and checks it is noticed (`tests/test_invariants.py`).

## Results

Raw runs: [`bench/results/`](../bench/results/) (one JSON file per run). Tables below are the
output of `python bench/summarize.py`.

**Host and setup.** An Intel Core Ultra 7 155H laptop (Windows 11, 15.6 GB RAM) running Docker
Desktop with WSL2 (22 logical CPUs and 7.5 GB visible to containers). The load generator (Locust),
four web processes and the worker ran in one container and PostgreSQL 18 in another, so the load
generator shares CPUs with the system under test. Every student arrives at once (Locust
spawn rate equal to the user count) and each does one journey: reserve, order, pay. The naive
runs use no artificial delay. Runs were interleaved (rep 1 of every strategy, then rep 2, then
rep 3) so slow drift on the machine affects every strategy alike.

#### Environment

- Machine: x86_64 (22 logical CPUs visible), Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.41
- PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2), Python 3.13.18
- 4 web processes, 10 database connections each, one worker process
- Commit(s): 94d7802; 27 runs

#### Spike: 300 students, 30 seats

| Strategy | Runs | Seats sold (confirmed) | Oversold seats | Reserve p50 (s) | Reserve p95 (s) | Reserve req/s | Failed requests |
| :-- | --: | --: | --: | --: | --: | --: | --: |
| `naive` | 3 | 40 (37-44) | 10 (7-14) | 0.8 (0.8-1.3) | 1.3 (1.3-2.2) | 157 (94-163) | 0 (0-0) |
| `row_lock` | 3 | 30 (30-30) | 0 (0-0) | 1.1 (1.0-2.0) | 1.6 (1.6-3.4) | 147 (73-157) | 0 (0-0) |
| `atomic_update` | 3 | 30 (30-30) | 0 (0-0) | 0.8 (0.8-1.3) | 1.5 (1.4-2.0) | 158 (104-187) | 0 (0-0) |
| `skip_locked` | 3 | 30 (30-30) | 0 (0-0) | 0.9 (0.9-1.5) | 1.7 (1.4-2.6) | 148 (92-166) | 0 (0-0) |

#### Spike: 5,000 students, 200 seats

| Strategy | Runs | Seats sold (confirmed) | Oversold seats | Reserve p50 (s) | Reserve p95 (s) | Reserve req/s | Failed requests |
| :-- | --: | --: | --: | --: | --: | --: | --: |
| `naive` | 3 | 210 (204-211) | 10 (4-11) | 22.0 (22.0-31.0) | 34.0 (33.0-42.0) | 131 (107-138) | 0 (0-0) |
| `row_lock` | 3 | 200 (200-200) | 0 (0-0) | 19.0 (17.0-22.0) | 28.0 (24.0-31.0) | 152 (136-176) | 0 (0-0) |
| `atomic_update` | 3 | 200 (200-200) | 0 (0-0) | 21.0 (20.0-22.0) | 31.0 (31.0-32.0) | 133 (133-138) | 0 (0-0) |
| `skip_locked` | 3 | 200 (200-200) | 0 (0-0) | 25.0 (24.0-25.0) | 36.0 (36.0-36.0) | 125 (122-127) | 0 (0-0) |

#### Retry storm: every write sent twice with the same key

| Strategy | Users | Seats | Writes sent | Seats sold | Oversold | Orders | Charges at provider | Duplicate answered differently | Failed requests |
| :-- | --: | --: | --: | --: | --: | --: | --: | --: | --: |
| `atomic_update` | 5,000 | 200 | 10,800 | 200 | 0 | 200 | 200 | 0 | 0 |
| `skip_locked` | 5,000 | 200 | 10,800 | 200 | 0 | 200 | 200 | 0 | 0 |

#### Soak: slow, declining and forgetful payment provider, short holds

| Strategy | Duration (s) | Requests | Failed | Reservations | Orders | Payments | Messages | Oversold | Violated invariants |
| :-- | --: | --: | --: | :-- | :-- | :-- | --: | --: | :-- |
| `atomic_update` | 60 | 14,245 | 0 | {'confirmed': 440, 'held': 10} | {'confirmed': 422, 'payment_failed': 28} | {'failed': 28, 'succeeded': 422} | 422 | 0 | none |

Median, with the lowest to highest of the repeated runs in brackets.

## What the numbers say

**1. The safe strategies are safe, and the naive one is not.** In all 3 small and all 3 large
runs of each of `row_lock`, `atomic_update` and `skip_locked`, the number of confirmed seats equals
the seats available, there are zero oversold seats and no invariant is violated. In all 6 runs
of `naive` it oversold, by 4 to 14 seats, with no delay added: the race is real at these sizes,
not a contrivance. (CI uses a 5 ms delay for the naive strategy so its failure is deterministic
on a small runner; the table above does not.)

**2. Retries are safe.** In the retry storm every write was sent twice at the same instant with
the same key (10,800 writes for 5,000 students). The result is exactly 200 seats sold, 200 orders
and 200 charges at the provider, and no duplicate was answered differently from its twin.

**3. The soak run found nothing wrong, and exercised the awkward paths.** With a provider that
was slow, declined some payments and lost the response to some others, and with one-minute
holds, 14,245 requests produced no failed request and no violated invariant. Every payment whose
response was lost was settled later by the worker.

**4. The strategies were close in speed, which was not what I expected.** At 300 students the
requests-per-second ranges of all four overlap, and `atomic_update` had the highest median. At
5,000 students `row_lock` was fastest (136 to 176), `skip_locked` slowest (122 to 127) and
`atomic_update` in between (133 to 138): a spread of roughly 20 to 30 percent from three runs
each, in the opposite order from the one I predicted. My hypothesis going in
([research.md](research.md), section 7) was that `skip_locked` would pull ahead because buyers take
different rows instead of queueing on one. These runs do not show that. Across every run, all
four sit between about 70 and 190 reserve requests per second, so the single hot row is not what
limits this system here.

I have not pinned down what does limit it, so I make no claim about the cause. Candidates are
the per-request work (a session lookup, the idempotency key and several round trips), commit
latency on this machine's Docker volume, and the Python web processes; the load generator also
shares the CPUs. Phase 3 profiles this before deciding anything is worth splitting out.

**5. When 5,000 students arrive in the same second, nobody gets a fast answer.** The median
reserve took about 19 to 25 seconds and the 95th percentile 28 to 36 seconds, with every strategy,
because requests queue far faster than they are served. That is a throughput ceiling, not a
seat-locking problem, and it is the case for the waiting room and rate limiting planned for
phase 3: admit students at the rate the system can serve instead of letting all of them wait.

**Decision.** The default stays `atomic_update`: it is correct, it is a single statement, and
nothing here shows a safe alternative beating it. `row_lock` is the simplest to explain and
equally safe. `skip_locked` is the only one that can answer `busy`, and the numbers give no
reason to pay for that. The point of having four is that the comparison is now a command, not an
opinion.

## What this does not show

- **One machine, one run of each configuration three times.** The ranges are real: the same
  configuration varied by up to a factor of two between runs (for example `row_lock` at 300
  students: 73 to 157 requests per second). Differences smaller than that should be read as
  noise.
- **One hot event.** All students want the same event. Spread across many events, contention
  would be lower and the strategies could separate; the soak run uses three events but is a
  correctness test, not a comparison.
- **A single database server and a local disk.** Nothing here says how a replicated or managed
  PostgreSQL would behave.
- **Simulated payments.** The provider is a mock with injected latency and failures.
- **Latency is for a simultaneous arrival.** Real launches ramp up; the 5,000-in-one-second case
  is the worst case on purpose.

## Reproducing the numbers

You need Docker. From a clone:

```bash
bench/run_matrix.sh        # the full matrix: about 25 minutes (REPS=1 for a quick look)
python bench/summarize.py  # prints the tables above from bench/results/
```

One configuration at a time, inside Docker:

```bash
docker compose --profile bench run --rm bench --strategy skip_locked --users 5000 --seats 200
```

Or natively (needs `uv` and a PostgreSQL on port 5440, which `docker compose up -d db` provides):

```bash
uv run python bench/run.py --strategy atomic_update --users 300 --seats 30
```

Each run exits non-zero if a safe strategy oversells, an invariant is violated, a request fails
or the run does not finish in two minutes. `--expect-oversell` inverts the first check for the
naive baseline. CI runs a small version of the same thing on every pull request
(`.github/workflows/ci.yml`, job "Seat rush").
