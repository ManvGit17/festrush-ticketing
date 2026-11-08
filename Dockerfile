# syntax=docker/dockerfile:1.7

# --- build: resolve dependencies from the lockfile into a virtualenv ----------------------------
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-default-groups --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-default-groups

# --- bench: the app plus the load-test tooling, for `docker compose --profile bench run bench` -----
FROM python:3.13-slim AS bench
RUN apt-get update && apt-get install -y --no-install-recommends nginx-light \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv     uv sync --frozen --no-install-project
COPY src ./src
COPY db ./db
COPY main.py ./main.py
COPY bench ./bench
COPY gateway ./gateway
# k6, a compiled load generator, to tell the application's limits from Locust's.
COPY --from=grafana/k6:2.3.0 /usr/bin/k6 /usr/local/bin/k6
RUN --mount=type=cache,target=/root/.cache/uv     uv sync --frozen
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["python", "bench/run.py"]

# --- runtime: the virtualenv, the package and the migrations ------------------------------------
FROM python:3.13-slim
RUN useradd --create-home --uid 10001 festrush
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src
COPY db /app/db
COPY main.py /app/main.py
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER festrush
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["festrush", "serve", "--host", "0.0.0.0", "--port", "8080"]

