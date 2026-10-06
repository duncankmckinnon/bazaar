# Market service: demo runbook

Run every command from the repository root. The market keeps prices, experiment clocks and
accounts in one SQLite file, `data/market.sqlite3` by default. Import prices into the same file
the server reads.

## 1. Load prices

Pick one dataset. The `data_version` you import under is the one you pass on the experiment's
first `PUT /experiments/{experiment_id}/cutoff` (see section 3).

### Real prices: Alpaca daily bars

Fetch unadjusted daily bars for the 13 companies in `config/demo-sources.toml`, then import them:

```sh
uv run --env-file .env python -m bazaar_market.sources bars --version bars-2026-10-06
uv run python -m bazaar_market.sources import-bars \
    --snapshot data/raw/alpaca-bars/bars-2026-10-06 --db data/market.sqlite3
```

- `bars` reads `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. It prints one line per ticker. A count
  of 0 means Alpaca has no data for that ticker in the period.
- `import-bars` reads no environment variables. It prints each ticker's bar count and date
  range. If any ticker is missing data, it stores nothing and says which ticker and dates.
- The snapshot under `data/raw/` is gitignored. Do not commit it.

The data version is **`alpaca-bars-v1`**.

**If `bars` is refused for the `sip` feed** (403 or 422), fetch from IEX into a new folder and
import it under its own data version:

```sh
uv run --env-file .env python -m bazaar_market.sources bars --feed iex --version bars-2026-10-06-iex
uv run python -m bazaar_market.sources import-bars \
    --snapshot data/raw/alpaca-bars/bars-2026-10-06-iex --db data/market.sqlite3 \
    --data-version alpaca-bars-v1-iex
```

The data version is then **`alpaca-bars-v1-iex`**.

**If one ticker has no bars** (FI is the likely one) and blocks the import, import only the demo
run's tickers. The same coverage checks apply to them. The output and the `data_imports` table
both list the tickers that were left out:

```sh
uv run python -m bazaar_market.sources import-bars \
    --snapshot data/raw/alpaca-bars/bars-2026-10-06 --db data/market.sqlite3 \
    --symbols AAPL,MSFT,KO
```

To refetch into a new folder and import it again under the same data version, rerun both commands
with a new `--version`. Identical bars import cleanly.

### Synthetic prices: the fallback

Invented prices for the same 13 companies, every weekday from 2025-07-01 to 2026-09-30. Needs no
keys or network.

```sh
uv run python -m bazaar_market.prices synthetic --out data/bars-synthetic-v1.csv
uv run python -m bazaar_market.prices import data/bars-synthetic-v1.csv --db data/market.sqlite3
```

The data version is **`synthetic-v1`**.

### Check what is loaded

```sh
sqlite3 data/market.sqlite3 \
  "SELECT data_version, symbol, COUNT(*), MIN(observed_at), MAX(observed_at)
   FROM data_bars GROUP BY 1, 2;"
sqlite3 data/market.sqlite3 "SELECT * FROM data_imports;"
```

## 2. Start the server

TODO(builder-ledger): the start command and the approval header, from `app.py`.

## 3. Choose the dataset for an experiment

An experiment reads one data version, fixed by its first cutoff. The first
`PUT /experiments/{experiment_id}/cutoff` must name both versions:

| Dataset | `data_version` | `execution_rule_version` |
| --- | --- | --- |
| Alpaca daily bars | `alpaca-bars-v1` | `exec-v1` |
| Alpaca daily bars, IEX fallback | `alpaca-bars-v1-iex` | `exec-v1` |
| Synthetic | `synthetic-v1` | `exec-v1` |

Later cutoffs may repeat the versions or leave them out, but cannot change them. A daily close
becomes visible at 16:00 New York time. A cutoff during a session sees the previous close.

## Environment variables

| Variable | Read by | Meaning |
| --- | --- | --- |
| `BAZAAR_MARKET_DB` | server | SQLite file. Default `data/market.sqlite3`. Must match `--db` above. |
| `BAZAAR_DEV_APPROVAL_IDS` | server | DEV ONLY. Comma-separated approval UUIDs to allow. Unset means every approval is denied. |
| `LOGFIRE_TOKEN` | server | Logfire write token. Traces are sent only when it is set. |
| `BAZAAR_ENVIRONMENT` | server | Logfire environment label. Default `development`. |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | `bars` | Alpaca keys, from `.env`. Not needed to import or serve. |
