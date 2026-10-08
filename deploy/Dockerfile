# One container for the conference: the market on 127.0.0.1:8000 and the web app on $PORT.
# Build from the repository root: docker build -f deploy/Dockerfile .
# The market data ships only from deploy/seed/, which the PM stages and git ignores.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY services services
RUN uv sync --frozen --no-dev --all-packages

COPY deploy/entrypoint.sh /app/deploy/entrypoint.sh
COPY deploy/seed/market.sqlite3 /app/seed/market.sqlite3
COPY deploy/seed/runs /app/seed/runs

RUN useradd --create-home --uid 1000 bazaar \
    && mkdir -p /data \
    && chown bazaar:bazaar /data
USER bazaar

ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
