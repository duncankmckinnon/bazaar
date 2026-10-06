# Data sources

`bazaar_market.sources` downloads source data and freezes it under `data/raw/<source>/<version>/`.
Fetching needs network access.
Reading a snapshot and applying the visibility rules does not, and the tests use synthetic fixtures only.

## What is fetched

- **SEC EDGAR**, `data.sec.gov`, no key.
  Filing history per company with an acceptance time to the second, and XBRL facts with the accession number of
  the filing that reported each value.
  Amendments (`10-K/A`, `10-Q/A`, `8-K/A`) are separate filings.
- **Alpaca news**, `data.alpaca.markets/v1beta1/news`, needs `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`.
  One page file per 50 articles per ticker.
- **S&P 500 membership**, `sp500_ticker_start_end.csv` from `fja05680/sp500` at a pinned commit.
  Ticker, start date, end date.
  The file is hand-maintained from a book and Wikipedia.

- **Alpaca daily bars**, `data.alpaca.markets/v2/stocks/bars`, needs `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`.
  Requested with `adjustment=raw`: split- or dividend-adjusted history rewrites old prices with corporate actions that
  happened later, which is lookahead.
  The manifest records the adjustment and feed with each ticker's window.
  Each ticker is requested with `asof=-` over the days it traded under that name, so Fiserv is fetched as FI until
  2025-11-10 and as FISV from 2025-11-11.

Corporate actions and the trading calendar are not fetched.

## Prices

`bazaar_market.prices` stores daily bars in the market database's `data_bars` table, keyed by data version, symbol
and observation time.
A bar's close is observed and available at its session's 16:00 Eastern close, stored in UTC, so a read cut off
during a session gets the previous close.
`SqliteMarketData.price_at` returns the latest close available at the cutoff and raises `MissingData` when there is
none.
A day with no bar for any symbol has no session. Early closes and holidays are not modelled.

Bars are imported from a CSV with the header `symbol,date,open,high,low,close,volume`: the ticker in use that day,
the session date as `YYYY-MM-DD`, decimal prices and an integer volume.
Re-importing the same bars is a no-op, and a changed bar under the same data version raises `BarConflict`.

```sh
uv run python -m bazaar_market.prices synthetic --out data/bars-synthetic-v1.csv
uv run python -m bazaar_market.prices import data/bars-synthetic-v1.csv --db data/market.sqlite3
```

Real bars are fetched into a snapshot and then imported as data version `alpaca-bars-v1`:

```sh
uv run --env-file .env python -m bazaar_market.sources bars --version <version>
uv run python -m bazaar_market.sources import-bars --snapshot data/raw/alpaca-bars/<version> --db data/market.sqlite3
```

`bars` takes `--feed sip` (the default) or `--feed iex`.
Alpaca answers 200 with no bars where it has no data, so `import-bars` treats missing data as an error and stores
nothing.
It fails when a ticker has no bars, when a ticker lacks a session other tickers traded between its first and last bar,
or when AAPL, MSFT or KO lacks a weekday from 2026-01-30 to 2026-02-13, the runner's demo run.

`synthetic` writes data version `synthetic-v1`: a seeded random walk on every weekday from 2025-07-01 to 2026-09-30
for the thirteen demo companies.
It follows the demo's ticker history (FI until 2025-11-10, then FISV; K ends 2025-12-10; EA ends 2026-08-04).
The prices are invented.

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
Its window ends at the time of the fetch, not at the end of the day.
`scripts/install-news-capture.sh` schedules `capture-news` daily with launchd on macOS.

The EDGAR User-Agent is `SEC_USER_AGENT` from the environment, or else `edgar.user_agent` in the config.
The demo config names anthony@pydantic.dev.
Filing text is downloaded only when the User-Agent contains a contact address.
`www.sec.gov` returns 403 to callers that do not declare one, and `edgar` skips documents and prints a notice.

## Snapshots

Each version directory has a `manifest.json` listing every file with its source URL, SHA-256, byte count, row count
and fetch time.
A file is frozen once the manifest lists it.
`Snapshot.write` raises `SnapshotConflict` when a frozen file would get different bytes or is missing from disk, and
`UnsafePath` when a name resolves outside the version directory.
Names are normalized first, so `a/./b.json` and `a/b.json` are one file.
Files are written through a temporary file and renamed, and a file left without a manifest entry by a run that died
is written again.
One process writes a version at a time. There is no lock.

The manifest records the window each symbol's news was fetched for, and `sources.read.news_coverage` returns it.
Outside that window, an empty result means the news was not fetched, not that there was none.

An interrupted `news` run resumes when rerun with the same `--version`: frozen pages are read back and the fetch
continues from the last page token.
Fetching a different window into that version raises `SnapshotConflict`.
Because a `capture-news` window ends at the fetch time, a failed capture is rerun into a new version.
Resume is meant for an immediate retry.
Alpaca serves only the latest revision of each article, so resuming much later can freeze pages that reflect two
different moments: an article revised in between can be kept in its older form or drop out of the window.
Fetch into a new version instead.

EDGAR has no resume.
Rerunning `edgar` into an existing version raises `SnapshotConflict` as soon as EDGAR has added a filing for any
company, because that company's submissions file has changed.
Fetch into a new version instead.

Requests are tried up to three times for dropped connections and for 429, 502, 503 and 504.
Redirects are never followed, so the Alpaca key headers are sent to `data.alpaca.markets` only.

`data/raw/` is ignored by git.

## Research archives: news and filings

The market serves archived news and company filings to agents at
`GET /experiments/{id}/news/{symbol}` and `GET /experiments/{id}/filings/{symbol}`, as `NewsPage` and `FilingPage`.
An experiment's data bundle (for example `demo-bundle-v1`) names the news and filings versions it reads.
Every page and item carries the experiment's `data_version`, and `source` names the archive version,
for example `alpaca-news/alpaca-news-v1`.
A window the archive does not cover is 404, never an empty page.

```sh
uv run python -m bazaar_market.sources import-news --snapshot data/raw/alpaca-news/<version> --db data/market.sqlite3
uv run python -m bazaar_market.sources import-filings --snapshot data/raw/edgar/<version> --db data/market.sqlite3
```

News (`alpaca-news-v1`):

- An article is published at `created_at`, and its revision on file is available at `updated_at`, or at
  `created_at` if that is later.
  It is served only once that revision is available, so an article revised after the cutoff is left out.
- The fetch selects by revision time, so an article revised after the fetched window ended is absent.
  A request is covered only when the window and the cutoff are both inside the fetched window.
- Text is the content's HTML as plain text, or the summary when there is no content, or empty when there is neither.
  It is cut at 100,000 characters with the marker `[truncated at 100000 characters]`.

Filings (`edgar-filings-v1`):

- Only 10-K and 10-Q filings and their amendments are served.
  8-K filings have no fiscal period and are not served.
- A filing's fiscal period comes from its own XBRL facts: the duration that ends on its report date and lasts
  350 to 380 days for a 10-K or 84 to 98 days for a 10-Q.
  A 10-K also reports prior-year and quarterly durations, and a 10-Q reports year-to-date ones, so the first duration
  found is not used.
  A filing with no such duration, or more than one, is left out, and `import-filings` lists it with the reason.
- A filing is published, revised and available at its acceptance time.
- The archive covers acceptances from `edgar.documents_since` (2024-07-01) to the end of the period, because only those
  filings' documents were fetched.
  A qualifying filing in that window without its document stops the import.
- Text is the primary document as plain text, without inline XBRL headers.
  It is cut at 200,000 characters with the marker `[truncated at 200000 characters]`; most annual reports are cut.

## Reading point-in-time

`sources.read` loads a snapshot into `Filing`, `Fact` and `NewsItem` records.
`SourceError` is raised for a company or ticker missing from the snapshot, for a filing history whose older files
were not all frozen, and for news whose fetch stopped before the last page.
None of these is returned as a shorter list.
The readers read only files the manifest lists, and raise `SourceError` for a file on disk that the manifest
does not list or whose SHA-256 no longer matches.

`sources.visibility` filters records by a timezone-aware simulated time:

- A filing is visible from `accepted_at`, which is EDGAR's `acceptanceDateTime` read as labelled.
  See the first known limit.
- A fact is visible when the filing with its accession number is visible.
  If that filing is not loaded, the fact is visible from midnight Eastern on the day after `filed`.
  This relies on XBRL forms being under EDGAR's 17:30 Eastern filing-date cutoff.
  Forms that keep a same-day date until 22:00 Eastern, such as Section 16 forms, carry no XBRL facts.
- A news article is visible from `updated_at`, or from `created_at` if that is later.
  Alpaca serves only the latest revision of an article, and its date filter matches on `updated_at`.
- `universe.in_universe` is true from a membership's start date up to, and not including, its end date.
  It is index membership, not trading status.

These functions take the trusted clock from the caller.
They do not authorize anything, and the market server still has to enforce the cutoff.

## Known limits

- EDGAR labels `acceptanceDateTime` as UTC, and for some filers it is not.
  In the demo snapshot the value is UTC for nine of the thirteen companies.
  For AAPL, AMZN, JPM and META it is 4 or 5 hours later than the real acceptance time: 1,501 of 5,312 10-K, 10-Q
  and 8-K filings since 2005 match EDGAR's 17:30 Eastern filing-date rule only after subtracting the Eastern offset.
  Read as labelled, no filing is dated before its acceptance, so these filings become visible late and never early.
  Correcting the value needs the acceptance time on the filing index page, which is on `www.sec.gov`.
  `load_filings` checks every 10-K, 10-Q and 8-K, and their amendments, against the 17:30 Eastern rule.
  It raises `SourceError` when a filing is labelled on a weekday before 17:30 Eastern but dated a later day, because
  that label is earlier than the real acceptance and would show the filing early.
  This catches only early labels that cross a filing-date boundary.
  An early label that stays on the filing date, such as 16:30 Eastern stamped as 16:30 UTC, is still undetectable
  without the index page.
- Membership is keyed by the ticker in use at the time.
  `in_universe` is false for FISV from 2023-06-07 to 2025-11-10, when Fiserv traded as FI.
  Mapping a company across a rename is left to the importer.
  The membership file also ends a spell after the last trade: K on 2025-12-11, with a last price on 2025-12-10.
- `news_symbols` has no dates.
  The demo config fetches FI for the whole period, and 3 of its 91 articles are dated after the rename.
- No "prior-cycle" rule beyond acceptance time.
  A `10-K` or `10-Q` is accepted after its period ends, so acceptance time excludes current-period numbers.
  EDGAR's `reportDate` is not a reliable period end for old filings: 8 of 1,616 periodic reports in the demo snapshot,
  all from 2012 or earlier, carry the filing date there.
  A stricter fiscal-cycle rule is a spec decision.
- EDGAR identifies companies by CIK and returns no ticker for delisted companies, so `config/demo-sources.toml`
  maps each ticker to its CIK.
- An article revised after the fetched window ended is absent from that window's snapshot.
- Alpaca's stock history starts in 2016.
- The terms for storing and redistributing Alpaca and SEC data have not been reviewed.
  Keep committed fixtures synthetic.

## Demo config

`config/demo-sources.toml` names thirteen companies and the period 2025-07-01 to 2026-09-30.
Both are placeholders.
The period is recent so that its last months are after the training cutoff of current models.
It contains the FI to FISV rename (2025-11-11) and two cash acquisitions: Kellanova (2025-12-11) and Electronic Arts
(2026-08-04).
