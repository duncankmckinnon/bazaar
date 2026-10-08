FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services services
COPY packages packages
# The runner loads the trading agent and evaluation packages at runtime.
RUN uv sync --frozen --no-dev --all-packages
EXPOSE 8080
CMD ["uv", "run", "--no-sync", "--package", "bazaar-web", "python", "-m", "bazaar_web"]
