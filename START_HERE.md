# Start here

FestRush is college-fest ticketing that is built never to sell a seat twice, with the tests and
benchmarks to show it. It began as a small project covering a few cloud-computing topics: one
FastAPI app where students sign up, browse events and register. This is a whole new version of it,
with much more behind the same idea.

This page is the front door. It says how to run the project, where to read, and, further down,
keeps the running log of what has changed. **New updates are added to the top of the Updates
section.**

## Run it

```bash
docker compose up
```

Then open <http://localhost:8080> (the app) or <http://localhost:8088> (the same app behind the
gateway). Demo accounts are `demo01` to `demo05`; the password is in [.env.example](.env.example).
For dashboards and traces:

```bash
FESTRUSH_OTLP_ENDPOINT=http://jaeger:4318 docker compose --profile observability up -d
```

Grafana is at <http://localhost:3300>, Jaeger at :16686, Prometheus at :9090.

## Where to read

| If you want to know | Read |
| :-- | :-- |
| What the project is and how to develop on it | [README.md](README.md) |
| What was wrong with the original, with evidence | [docs/audit.md](docs/audit.md) |
| Which approaches were considered, and why | [docs/research.md](docs/research.md) |
| The plan, the phases and what each one contains | [docs/roadmap.md](docs/roadmap.md) |
| How a seat is sold exactly once, and the measurements | [docs/concurrency.md](docs/concurrency.md) |
| What was split into services, what was not, and the evidence | [docs/architecture.md](docs/architecture.md) |
| The raw benchmark runs behind every number | [bench/results/](bench/results/) |

## Where things are

| Path | Contents |
| :-- | :-- |
| `src/festrush/` | The application: routes, seat claiming, orders, payments, worker, metrics |
| `db/migrations/` | The schema, as numbered SQL files |
| `gateway/` | The nginx front door |
| `observability/` | Prometheus and Grafana configuration and the dashboard |
| `bench/` | Load, chaos and experiment scripts, and their results |
| `tests/` | Tests against PostgreSQL; `tests/characterization/` pins the original behaviour |
| `legacy/v1/` | The original version, kept for reference |

## Updates

### 2026-11-08: the hardcoded student identifier is removed

- A student registration number was a constant in `main.py` that nothing read. It is removed from
  `main.py` and from the archived original in `legacy/v1/`, and a test now fails if one comes back.
- Earlier commits in the history still contain it; history is not rewritten.

### 2026-11-08: phase 3, services and observability (merged)

- **Built:** an nginx gateway (rate limiting, a one-second read cache, `/metrics` hidden), the
  payment provider as its own service, a waiting room, Prometheus metrics, OpenTelemetry traces, a
  Grafana dashboard, and chaos runs that break the worker, the payments service and the database
  mid-sale and then check every invariant.
- **Found and fixed:** a provider outage used to abandon every sale in flight; the database pool did
  not recover after PostgreSQL restarted.
- **Measured, with results that differ from the plan:** the web tier limits throughput, not the
  database; splitting out the provider costs speed but makes outage handling real; confirmations
  through the outbox beat sending them inline; the read cache and the in-app waiting room did not
  help against a simultaneous arrival. The inventory split was not built, and the write-up says so.
- Details: [docs/architecture.md](docs/architecture.md).

### 2026-11-08: the repository is renamed and public

- Now `festrush-ticketing`. CodeQL runs on every pull request and reports no open alerts.

### 2026-11-08: phase 2, the concurrency core (merged)

- **Built:** seat holds that expire; four ways to claim a seat (one deliberately unsafe, as the
  baseline); an order and payment state machine enforced by the database; idempotency keys; a mock
  payment provider; a transactional outbox and a worker that survives being killed; eight
  invariant checks; a JSON API; a benchmark runner.
- **Measured:** the three safe strategies never oversold and the unsafe one always did; retried
  writes took effect once; the strategies were close in speed.
- Details: [docs/concurrency.md](docs/concurrency.md).

### 2026-11-08: phase 1, the foundation (merged)

- **Built:** PostgreSQL 18 with migrations, hashed passwords, server-side sessions and CSRF
  protection, per-user checkout, tests, linting, strict types, Docker Compose and CI.
- **Kept:** the four original load-test files, at their original paths, now signing students in.
- Details: [docs/audit.md](docs/audit.md) (section 8 tracks each finding).

## What is next

1. **Phase 4, the experience:** a new front end with a live seat map, queue position, real-time
   updates, and a "System Lens" view that shows traces, lock waits and queue depth.
2. **Phase 5, the launch:** a landing-page README, a documentation site with an API reference,
   branding, screenshots, a changelog and community files, launch drafts, and a tagged release.

## Open items for the maintainer

- **Required checks:** turning on required status checks for `main` is a repository setting.
- **Repository description and topics:** set these in the repository settings.
