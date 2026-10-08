FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services services
COPY packages packages
COPY config config
RUN uv sync --frozen --no-dev --package bazaar-market
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "--package", "bazaar-market", "uvicorn", "bazaar_market.app:app", "--host", "0.0.0.0", "--port", "8000"]
