FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services services
RUN uv sync --frozen --no-dev --package bazaar-agent
CMD ["uv", "run", "--no-sync", "--package", "bazaar-agent", "python", "-m", "bazaar_agent"]
