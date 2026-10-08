# Conference deploy

One container runs the market on `127.0.0.1:8000` (internal only) and the web app on `0.0.0.0:$PORT`.
It is meant for one Cloud Run service with min and max 1 instance and CPU always on.

## Stage the data (PM)

The image takes its market data from `deploy/seed/`, which git ignores. Real data is never committed.

```text
deploy/seed/market.sqlite3   a copy of the market DB with bars, news and filings imported
deploy/seed/runs/            the real seed runs shown on the board
```

Copy them in before building, for example with `sqlite3 data/market.sqlite3 ".backup deploy/seed/market.sqlite3"`.
That is safe while a server has the database open. `.dockerignore` keeps `data/`, every `.env*` file, every other
`*.sqlite3*` file and the rig notes out of the build context.

## Build and run

```sh
docker build -f deploy/Dockerfile -t bazaar-conf .
docker run --rm -p 8080:8080 \
  -e BAZAAR_RUNNER_TOKEN -e PYDANTIC_AI_GATEWAY_API_KEY -e LOGFIRE_TOKEN -e PUBLIC_URL \
  bazaar-conf
```

`-e NAME` with no value passes the variable from your shell, so secrets stay out of the command line and history.
On Cloud Run, set them as secrets or environment variables on the service.

## Environment

| Variable | Required | Meaning |
| --- | --- | --- |
| `BAZAAR_RUNNER_TOKEN` | yes | Token for the market's control routes. The container refuses to start without it. |
| `PYDANTIC_AI_GATEWAY_API_KEY` | yes | Model access for submitted strategies. |
| `LOGFIRE_TOKEN` | no | Sends traces to Logfire when set. |
| `PUBLIC_URL` | yes | Public base URL, used for the QR code to `/submit`. |
| `PORT` | no | Web port. Cloud Run sets it. Default 8080. |
| `BAZAAR_MAX_QUEUE` | no | Default 30. |
| `BAZAAR_MAX_SUBMISSIONS_PER_DAY` | no | Default 150. |
| `BAZAAR_MAX_PER_IP_PER_HOUR` | no | Default 5. |
| `BAZAAR_MARKET_DB` | no | Writable market DB. Default `/data/market.sqlite3`, copied from the seed on first start. |
| `BAZAAR_RUNS_DIR` | no | Writable runs folder. Default `/data/runs`, filled from the seed when empty. |
| `BAZAAR_WEB_APP` | no | Web app to serve. Default `bazaar_web.app:app`. |

The container sets `BAZAAR_MARKET_URL=http://127.0.0.1:8000` itself. `/data` is the container's own disk, so on
Cloud Run a new instance starts again from the seed. The container exits non-zero if the market or the web app dies,
and Cloud Run restarts it.
