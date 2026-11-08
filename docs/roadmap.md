# Roadmap

_Proposed 2026-11-08. Based on [audit.md](audit.md) and [research.md](research.md). Nothing here
is built yet; this is the plan to approve._

## Positioning

> **A fest ticketing system that cannot oversell, and proves it.** It starts as a small
> monolith, measures it under a launch-day rush, and splits into services only where the
> measurements say a split helps.

Three things make it more than a rewrite:

1. **Invariants, not vibes.** A checker script asserts "confirmed + held <= capacity", "no seat
   without a paid order" and "every paid order confirmed exactly once" after every load run.
2. **The same load against every design.** Inventory strategies are benchmarked head to head,
   including the naive one that oversells.
3. **A System Lens.** The UI can open the system as it works: traces, lock waits, queue depth,
   outbox lag, the way the previous project's SQL Lens opened the database.

## Target architecture

```
                         ┌──────────── observability (self-hosted) ────────────┐
                         │ OpenTelemetry Collector ─▶ Jaeger     Prometheus ─▶ Grafana │
                         └───────▲───────────────────────────▲─────────────────┘
                                 │ traces/metrics            │
  browser ──▶ nginx gateway ──▶ app (modular monolith, FastAPI)
  (React,     rate limit,        ├ identity   sessions, hashed passwords
   SSE)       waiting-room       ├ catalog    events, seat map (read-heavy)
              check              ├ inventory  holds, capacity, strategies
                                 ├ orders     state machine, idempotency keys
                                 ├ waitingroom admission tokens, position
                                 └ lens       trace/lock/queue view for the UI
                                     │                    │
                                     ▼                    ▼ HTTP, with timeouts/retries
                               PostgreSQL 18      payments-mock (own process, own failures)
                               (data, outbox,            ▲
                                queue, idempotency)      │ webhooks
                                     ▲
                                     │ SELECT ... FOR UPDATE SKIP LOCKED
                               worker (outbox relay, hold expiry, emails)
```

Phase 1 and 2 are one deployable app plus PostgreSQL. The picture above is the destination of
phase 3, and only the pieces that win their experiment ([research §7](research.md#7-splitting-a-monolith-only-where-it-pays))
are extracted.

### Stack

| Layer | Choice | Why |
| :-- | :-- | :-- |
| Language and API | Python 3.13, **FastAPI** (kept), psycopg 3 with a pool | Same language and framework as v1 and the previous project; psycopg exposes locks and transactions directly, which matters here. No ORM hiding the SQL being benchmarked. |
| Database | **PostgreSQL 18** (SQLite dropped) | Row locks, `SKIP LOCKED`, constraints, `LISTEN/NOTIFY`; the queue and idempotency store live here too. |
| Migrations | Plain numbered `.sql` files and a small runner | Same approach as the previous project. |
| Passwords and sessions | Argon2id or bcrypt hashes; server-side sessions in PostgreSQL, `HttpOnly` `SameSite=Lax` cookie; CSRF token on forms | Fixes S1, S2, S7. |
| Gateway | **nginx** | Provides `limit_req` and routing for the strangler step. |
| Web UI | **React 19 + TypeScript + Vite**, Tailwind v4 (built, not CDN), SSE for live updates | Same stack as the previous project; fixes S8. Needs your approval (decision 4). |
| Observability | OpenTelemetry, Prometheus, Grafana, Jaeger, all in Compose | Self-hosted, no keys, no data leaves the machine. |
| Load tests | Locust, with a checker script | Already used by the original project. |
| CI | GitHub Actions: ruff, mypy, pytest against a PostgreSQL 18 service, Compose smoke test, small load and chaos run, Playwright with axe, CodeQL, pip-audit and npm audit | Matches the previous project's CI, plus load. |

### A separate autoscaling project

An unrelated autoscaling project of the same author exists. It is linked from the launch material, not merged here; this
project has an nginx gateway of its own.

## Features, ranked by value and risk

Size: S under half a day, M about a day, L more than a day. Risk is the chance of the feature
slipping or having to be reworked.

| # | Feature | Value | Risk | Size | Phase |
| :-- | :-- | :-- | :-- | :-- | :-- |
| F1 | Secure foundation: hashed passwords, sessions, CSRF, no committed DB, PostgreSQL, migrations, seed | Must | Low | L | 1 |
| F2 | Seat inventory that cannot oversell, with four strategies behind one interface | Highest | Medium | L | 2 |
| F3 | Holds with expiry, then payment, as an explicit order state machine | Highest | Medium | M | 2 |
| F4 | Idempotency keys on registration and payment | High | Low | S | 2 |
| F5 | Mock payment provider with injectable latency, failure and late confirmation | High | Low | M | 2, 3 |
| F6 | Outbox and a PostgreSQL queue for confirmations | High | Medium | M | 2 |
| F7 | Load tests with an invariant checker; large documented run | Highest | Low | M | 2 |
| F8 | Measured service split (experiments and extraction of what wins) | High | Medium | L | 3 |
| F9 | Gateway with rate limiting; waiting room | High | Medium | M | 3 |
| F10 | Tracing, metrics, Grafana dashboards | High | Low | M | 3 |
| F11 | Chaos tests (kill worker, slow provider, restart database) | High | Medium | M | 3 |
| F12 | New UI: live seat map, queue position, real-time updates | High | Medium | L | 4 |
| F13 | System Lens (traces, lock waits, queue depth, outbox lag) | High | High | M | 4 |
| F14 | Replay demo on GitHub Pages (labeled as a replay) | Medium | Low | S | 4 |
| F15 | Launch material: README, docs site, branding, release | Must | Low | L | 5 |

If time runs short, cut in this order: F14, then the lottery admission mode in F9, then the
Grafana dashboards beyond one main board. F1 to F7 and F12 are the core.

## Phases and pull requests

Each phase is one branch and one pull request. CI must pass before merge, and the next phase
starts after the merge. Merges can be held for a day if you ask.

### PR 1: Foundation (`modernize/foundation`)

Goal: a secure, tested, reproducible base. Behaviour changes are deliberate and each flips a
characterization test.

- `docs/audit.md`, `docs/research.md`, `docs/roadmap.md`, the characterization suite
  (`tests/characterization/`) and `scripts/audit_probe_v1.py`.
- `pyproject.toml` with pinned, locked dependencies (uv); the app moved into a `src/` package with
  modules for identity, catalog and orders; typed settings from environment variables.
- Passwords hashed (S1); server-side sessions, CSRF protection, writes through `POST` (S2);
  input validation (S6); generic error pages (S5); `*.db` ignored so a database can't be committed (S4);
  the hardcoded student identifier removed (S3); bare `except:` replaced by specific handling (A2).
- PostgreSQL 18 schema with keys, foreign keys, uniqueness and a capacity column (D1, A4, A5, A6),
  numbered migrations, an idempotent seed script (A8) and a seeded dataset.
- The deliberate crash route (A7) moved behind a debug flag; leftover slow code and misleading
  UI text removed (A9, A15); working logout (A13); encoded links (A14).
- Locust files kept and fixed per decision 1 (A10).
- Docker Compose (PostgreSQL, app) on a non-conflicting port; Dockerfile; `.env.example`.
- CI: ruff, mypy, pytest against PostgreSQL 18, Compose smoke test, CodeQL, pip-audit; Dependabot;
  pre-commit.
- LICENSE per decision 3.

Done when: `docker compose up` serves the app; tests, types and lint pass in CI; the audit
findings S1 to S6, A1 to A5, A7 to A9 and D1 are closed or explicitly tracked.

### PR 2: The concurrency core (`modernize/concurrency`)

_Status: built; see [concurrency.md](concurrency.md)._

- Schema: events with capacity, seats, holds with `expires_at`, orders, payments, idempotency keys,
  outbox.
- Four reservation strategies behind one interface, selectable by configuration:
  `naive`, `row_lock`, `atomic_update`, `skip_locked`.
- Holds that expire (worker), orders as a state machine enforced in the database, a mock payment
  provider, and idempotent payment confirmation.
- Outbox written in the same transaction as the order; a worker relays it and sends confirmations.
- `bench/` with Locust scenarios (spike, retry storm, soak), the invariant checker and a runner
  that writes raw results (CSV and JSON) and a short report.
- CI runs a small spike (for example 300 users, 30 seats) against each strategy and asserts the
  invariants; `naive` is expected to fail its invariant check and that failure is asserted too.
- `docs/concurrency.md` with the measured comparison and the large run (for example 5,000 users and
  200 seats), the machine described, the exact command given.

Done when: zero oversells with each safe strategy at both scales, the naive strategy demonstrably
oversells, repeated requests with one key create one order, and killing the worker loses no
confirmation.

### PR 3: Services and observability (`modernize/services`)

_Status: built, with results that differ from the plan; see [architecture.md](architecture.md). Not built: the
inventory-service experiment (E1), so that split is argued, not measured._

- Experiments E1 to E4, one per candidate in [research §7](research.md#7-splitting-a-monolith-only-where-it-pays),
  each with a before and after measurement. The write-up states which splits helped and which did
  not.
- Extraction of the winners (expected: payments provider and worker; catalog caching at the
  gateway), strangler-fig style: nginx routes the new paths while old paths stay in the monolith.
- nginx gateway with `limit_req`; PostgreSQL-backed waiting room with FIFO admission, plus lottery
  admission if time allows.
- OpenTelemetry traces across app, worker and provider; Prometheus metrics (latency histograms,
  queue depth, outbox lag, lock waits, oversell counter); provisioned Grafana dashboard.
- Chaos tests: kill the worker, slow and fail the provider, restart PostgreSQL mid-run.
- `docs/architecture.md` with the decision record for each split.

Done when: the dashboard shows a live run; chaos runs end with all invariants intact; the
experiment results are committed and rerunnable.

### PR 4: Experience (`modernize/experience`)

- React and TypeScript app with an accessible design system (keyboard, WCAG AA, reduced motion,
  light and dark).
- Live seat map (free, held, yours, sold) updated over server-sent events; queue position view;
  checkout with the payment state visible; "my tickets".
- System Lens drawer on every screen: the request's trace spans, lock waits, queue depth and
  outbox lag.
- Playwright end-to-end tests and axe accessibility checks in CI.
- Replay demo on GitHub Pages, labeled as a recording of a real run.

Done when: a run through the whole journey passes the end-to-end tests, axe reports no serious
violations, and the Pages replay loads with no setup.

### PR 5: Launch (`modernize/launch`)

Everything in the brief's launch list: README as a landing page, docs site with an API reference
generated from the OpenAPI schema, logo and wordmark, screenshots and demo animation, social
preview, `CHANGELOG.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue and PR
templates, at least ten issues in `docs/good-first-issues.md`, honest drafts in `docs/launch/`
(blog post, Show HN, Reddit, X, LinkedIn), `docs/MODERNIZATION_SUMMARY.md`, then a tagged release
(`v2.0.0`; v1 is the original version). All links come from one config file so a repository rename is a
one-line change.

Done when: no placeholders or broken links, every number in the docs is traceable to a script in
the repository, and the release is published.

## Cross-cutting rules

- **Numbers.** Every figure in the README and docs comes from a script in the repository;
  `docs/` states the command and the hardware. Results that disappoint are published too.
- **Characterization tests** stay until the behaviour they pin is deliberately replaced; the
  commit that fixes a finding changes its test.
- **Lockfiles** are generated or verified in a Linux container, since you are on Windows.
- **Ports.** Local services use ports other than 8000, 8001 and 5000 (proposed: app 8080,
  gateway 8088, Grafana 3300, Jaeger 16686, PostgreSQL 5440).

## Decisions needed

These are yours to make before I start building. Defaults are my recommendation.

1. **What does the assignment brief require?** Does the grader run the four Locust files at their
   current paths and names (including the nested `locust/locust/journey_locustfile.py`)? Do they
   rely on the routes `/login`, `/register`, `/events?user=`, `/register_event/{id}?user=`,
   `/my-events?user=`, `/checkout`? Must the app stay a single (monolithic) deployable? If routes
   must work unauthenticated with `?user=`, that conflicts with fixing S2.
   _Default:_ keep every route name and the four Locust file names and paths, and update the
   Locust files to log in; the old `?user=` form is not kept. Tell me if the grader needs it.
2. **The student identifier in `main.py`.** It was unused by the code. _Resolved:_ removed from
   `main.py` and from the archived original. (It was also the username and password in the local
   `fest.db`, which is not on GitHub and was removed from the working tree.)
3. **License.** _Default:_ MIT, as in the previous project. (None exists today.)
4. **Front end.** Replace the Jinja pages with a React and TypeScript app (phase 4)? This is a
   framework change. _Default:_ yes; phase 1 keeps and secures the Jinja pages, so v1's
   server-rendered flow is still there until phase 4.
5. **SQLite to PostgreSQL** and the schema and API changes this implies (new tables, sessions,
   `POST` for registration). _Default:_ approved as in this plan.
6. **A separate autoscaling project.** _Default:_ link only, do not merge (see above).
7. **Names.** Keep the repository name `MonolithicApplication` (it carries the story). The
   product name in the UI and docs is not decided; _suggestion:_ **FestRush**.
8. **Merge timing.** Should I merge each pull request as soon as CI passes, or hold any of them
   for a particular day?
9. **Local setup.** I will install `uv` for Python tooling on your machine if it is missing
   (it is). _Default:_ yes. Everything else runs in Docker.
