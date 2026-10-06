# Data sources

`bazaar_market.sources` downloads source data and freezes it under `data/raw/<source>/<version>/`.
Fetching needs network access.
Reading a snapshot and applying the visibility rules does not, and the tests use synthetic fixtures only.

## What is fetched

- **SEC EDGAR**, `data.sec.gov`, no key.
  Filing history per company with `acceptanceDateTime` to the second, and XBRL facts with the accession number of
  the filing that reported each value.
  Amendments (`10-K/A`, `10-Q/A`, `8-K/A`) are separate filings.
- **Alpaca news**, `data.alpaca.markets/v1beta1/news`, needs `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`.
  One page file per 50 articles per ticker.
- **S&P 500 membership**, `sp500_ticker_start_end.csv` from `fja05680/sp500` at a pinned commit.
  Ticker, start date, end date.
  The file is hand-maintained from a book and Wikipedia.

Prices, corporate actions and the trading calendar are not fetched yet.

## Commands

```sh
uv run --env-file .env python -m bazaar_market.sources universe
uv run --env-file .env python -m bazaar_market.sources edgar
uv run --env-file .env python -m bazaar_market.sources news
uv run --env-file .env python -m bazaar_market.sources all
uv run --env-file .env python -m bazaar_market.sources capture-news --days 3
```

`--config` defaults to `config/demo-sources.toml`, `--root` to `data/raw`, and `--version` to the current UTC minute.
`capture-news` saves the trailing days into a new version, so a later capture can be compared with it.
`scripts/install-news-capture.sh` schedules `capture-news` daily with launchd on macOS.

Filing text is downloaded only when `SEC_USER_AGENT` contains a contact address.
`www.sec.gov` returns 403 to callers that do not declare one, and `edgar` skips documents and prints a notice.

## Snapshots

Each version directory has a `manifest.json` listing every file with its source URL, SHA-256, byte count, row count
and fetch time.
`Snapshot.write` raises `SnapshotConflict` when a file already exists with different bytes.
Writing identical bytes again is a no-op, so an interrupted `news` run resumes when rerun with the same `--version`:
pages already on disk are read back and the fetch continues from the last page token.

`data/raw/` is ignored by git.

## Reading point-in-time

`sources.read` loads a snapshot into `Filing`, `Fact` and `NewsItem` records.
A company or ticker missing from the snapshot raises `SourceError`.
It is never returned as an empty list.

`sources.visibility` filters records by a timezone-aware simulated time:

- A filing is visible from `accepted_at`.
  EDGAR's `acceptanceDateTime` is UTC.
- A fact is visible when the filing with its accession number is visible.
  If that filing is not loaded, the fact is visible from 00:00 UTC on the day after `filed`.
- A news article is visible from `updated_at`.
  Alpaca serves only the latest revision of an article, and its date filter matches on `updated_at`.
- `universe.tradable` is true from a membership's start date up to, and not including, its end date.

These functions take the trusted clock from the caller.
They do not authorize anything, and the market server still has to enforce the cutoff.

## Known limits

- No "prior-cycle" rule beyond acceptance time.
  A `10-K` or `10-Q` accepted before the simulated time always covers a finished period.
  A stricter fiscal-cycle rule is a spec decision.
- EDGAR identifies companies by CIK and returns no ticker for delisted companies, so `config/demo-sources.toml`
  maps each ticker to its CIK.
- An article revised after the fetched window ended is absent from that window's snapshot.
- Alpaca's stock history starts in 2016.
- The terms for storing and redistributing Alpaca and SEC data have not been reviewed.
  Keep committed fixtures synthetic.

## Demo config

`config/demo-sources.toml` names twelve companies and the period 2022-06-01 to 2023-06-30.
Both are placeholders.
The period contains the FB to META rename, Twitter's acquisition and SVB's failure.
