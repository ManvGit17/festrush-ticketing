# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org/). The original
version is kept in [legacy/v1/](legacy/v1/) and is called v1.

## 2.0.0

A rewrite of v1 around one promise: never sell a seat twice, and prove it.

- **Foundation:** PostgreSQL 18 with migrations, hashed passwords, server-side sessions, CSRF
  protection, per-user checkout, strict types, Docker Compose, CI.
- **Concurrency core:** seat holds that expire; four seat-claiming strategies (one deliberately
  unsafe, as a baseline); an order and payment state machine enforced by the database; idempotency
  keys; a transactional outbox with a worker that survives being killed; invariant checks; a JSON API.
- **Services and observability:** nginx gateway, payment provider as its own service, waiting room,
  Prometheus metrics, OpenTelemetry traces, Grafana dashboard, chaos runs.
- **Experience:** live seat map over server-sent events, System Lens drawer, Playwright journey and
  axe accessibility checks in CI.
- **Launch:** API reference generated from the OpenAPI schema, documentation and community files.
- **Fixed relative to v1:** see section 8 of [docs/audit.md](docs/audit.md) for each finding.
- **Not done:** the inventory split into a separate service (see
  [docs/architecture.md](docs/architecture.md) for why), trace spans inside the System Lens, and a
  recorded replay demo.
