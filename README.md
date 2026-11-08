# FestRush

College-fest ticketing that is meant to never oversell, and to prove it under load.

This repository started as a Cloud Computing lab (PES University): a small FastAPI monolith for a
fest. It is being rebuilt in phases into a systems-engineering project. The plan is in
[docs/roadmap.md](docs/roadmap.md), the starting point is audited in [docs/audit.md](docs/audit.md),
and the approaches considered are in [docs/research.md](docs/research.md).

**Status: phase 3 (services and observability).** Done: PostgreSQL 18 with migrations, secure
accounts, seat holds and a payment state machine the database enforces, four seat-claiming
strategies, idempotency keys, an outbox and worker, the payment provider as its own service, an
nginx gateway, a waiting room, Prometheus metrics, OpenTelemetry traces, a Grafana dashboard,
chaos tests, and benchmarks. Not yet built: the new UI and the launch material. See the
[roadmap](docs/roadmap.md), [how seats are sold exactly once](docs/concurrency.md) and
[where the system was split, and where it was not](docs/architecture.md).

## Quick start

```bash
docker compose up
```

That starts PostgreSQL 18, applies the migrations, runs the background worker, loads nine demo
events and five demo accounts
(`demo01` to `demo05`; the password is `FESTRUSH_DEMO_PASSWORD`, `festrush-demo` by default) and
serves the app at <http://localhost:8080> and, through the nginx gateway, at <http://localhost:8088>. Ports and passwords are in [.env.example](.env.example).

## Development

You need [uv](https://docs.astral.sh/uv/) and Docker.

```bash
docker compose up -d db          # PostgreSQL on localhost:5440
uv sync
export FESTRUSH_SECRET_KEY=local-development-only-secret-change-me
uv run festrush migrate
uv run festrush seed
uv run festrush serve            # http://127.0.0.1:8080
```

```bash
uv run pytest                    # tests against a real PostgreSQL (creates festrush_test)
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
```

The v1 characterization suite pins what the original lab code did, bugs included, and needs the
old dependency versions it was written for:

```bash
uv run --no-project --with-requirements tests/characterization/requirements-legacy.txt \
  pytest tests/characterization --confcutdir=tests/characterization -m legacy -o addopts=""
```

## Dashboards and traces

```bash
FESTRUSH_OTLP_ENDPOINT=http://jaeger:4318 docker compose --profile observability up -d
```

Grafana at <http://localhost:3300> (dashboard provisioned, anonymous viewing), Prometheus at
:9090, Jaeger at :16686. Nothing leaves your machine.

## Concurrency tests and benchmarks

The seat rush runs as a real test: students, seats, the app, the worker and Locust, then the
invariants are checked against the database.

```bash
docker compose up -d db
uv run python bench/run.py --strategy atomic_update --users 300 --seats 30
# the same at the size in the docs, inside Docker so the environment is the same for everyone:
docker compose --profile bench run --rm bench --strategy atomic_update --users 5000 --seats 200
uv run python bench/summarize.py
```

The full comparison is `bench/run_matrix.sh`. Seats are claimed through `POST
/api/events/{id}/reserve`; the strategy is `FESTRUSH_INVENTORY_STRATEGY` (`naive`, `row_lock`,
`atomic_update`, `skip_locked`).

## Load tests for the web pages

The four Locust files in [locust/](locust/) are kept from the lab. Each simulated student now
signs up and signs in first.

```bash
uv run locust -f locust/locust/journey_locustfile.py --host http://localhost:8080
```

## Layout

| Path | Contents |
| :-- | :-- |
| `main.py` | Entry point kept from v1: `uvicorn main:app` |
| `src/festrush/` | The application: routes, services, security, templates |
| `db/migrations/` | The schema, as numbered SQL files applied once each |
| `tests/` | Tests against PostgreSQL; `tests/characterization/` pins v1 |
| `locust/` | Load tests for each page journey (kept from the lab) |
| `bench/` | The seat-rush benchmark: scenarios, runner, results and summary script |
| `legacy/v1/` | The original lab code, unchanged |
| `docs/` | Audit, research and roadmap |

## License

[MIT](LICENSE). Built by Manit V Urs.
