# Contributing

Thanks for looking. Small, focused changes are easiest to review.

## Set up

You need [uv](https://docs.astral.sh/uv/) and Docker. The steps are in the Development section of
the [README](README.md).

## Before you open a pull request

```bash
uv run ruff format src tests && uv run ruff check src tests
uv run mypy
uv run pytest                              # needs PostgreSQL: docker compose up -d db
uv run python scripts/build_docs.py --check
```

- Work on a branch; do not push to `main`.
- Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- Add a test with every behaviour change. Tests run against a real PostgreSQL, not a mock.
- Anything about concurrency must keep [the invariants](docs/concurrency.md) true; the seat rush in
  CI checks them.
- If you quote a number in the docs, it must come from a script in `bench/` and a file in
  `bench/results/`.

Good first tasks are listed in [docs/good-first-issues.md](docs/good-first-issues.md).
By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
