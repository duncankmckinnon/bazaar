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
  A late-labelling filer (AAPL, AMZN, JPM or META) that files after about 19:30 Eastern on the evening before a
  weekday holiday would trip this check, so `load_filings` fails closed for that company; none of the 6,430 guarded
  10-K, 10-Q and 8-K filings (with amendments) for the thirteen demo companies, from 1994 to 2026, tripped it
  (checked 2026-10-06).
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
