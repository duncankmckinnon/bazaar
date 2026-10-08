#!/usr/bin/env bash
# Start the market (internal, 127.0.0.1) and the web app (public, $PORT) in one container.
# Exits non-zero if either process dies. Never prints secrets or the environment.
set -euo pipefail

say() { echo "entrypoint: $*" >&2; }

if [[ -z "${BAZAAR_RUNNER_TOKEN:-}" ]]; then
    say "BAZAAR_RUNNER_TOKEN is not set; refusing to start."
    exit 64
fi

seed_dir="${BAZAAR_SEED_DIR:-/app/seed}"
export BAZAAR_MARKET_DB="${BAZAAR_MARKET_DB:-/data/market.sqlite3}"
export BAZAAR_RUNS_DIR="${BAZAAR_RUNS_DIR:-/data/runs}"
market_port="${BAZAAR_MARKET_PORT:-8000}"
export BAZAAR_MARKET_URL="http://127.0.0.1:${market_port}"
startup_timeout="${BAZAAR_MARKET_STARTUP_TIMEOUT:-30}"

mkdir -p "$(dirname "$BAZAAR_MARKET_DB")" "$BAZAAR_RUNS_DIR"
if [[ ! -e "$BAZAAR_MARKET_DB" ]]; then
    if [[ ! -f "$seed_dir/market.sqlite3" ]]; then
        say "no market database at $BAZAAR_MARKET_DB and no seed at $seed_dir/market.sqlite3."
        exit 66
    fi
    cp "$seed_dir/market.sqlite3" "$BAZAAR_MARKET_DB"
    say "copied the seed market database to $BAZAAR_MARKET_DB"
fi
if [[ -z "$(ls -A "$BAZAAR_RUNS_DIR")" && -d "$seed_dir/runs" ]]; then
    cp -R "$seed_dir/runs/." "$BAZAAR_RUNS_DIR/"
    say "copied the seed runs to $BAZAAR_RUNS_DIR"
fi

stopping=0
pids=()
stop() {
    stopping=1
    kill -TERM "${pids[@]}" 2>/dev/null || true
}
trap stop TERM INT

python -m uvicorn bazaar_market.app:app --host 127.0.0.1 --port "$market_port" &
market_pid=$!
pids+=("$market_pid")

deadline=$((SECONDS + startup_timeout))
until python -c "import sys, urllib.request
urllib.request.urlopen(sys.argv[1], timeout=1)" "$BAZAAR_MARKET_URL/health" 2>/dev/null; do
    if (( stopping )); then
        wait || true
        exit 0
    fi
    if ! kill -0 "$market_pid" 2>/dev/null; then
        say "the market exited before it was ready."
        exit 70
    fi
    if (( SECONDS >= deadline )); then
        say "the market did not answer /health within ${startup_timeout}s."
        stop
        exit 70
    fi
    sleep 0.5
done
say "market ready on 127.0.0.1:${market_port}"

python -m uvicorn "${BAZAAR_WEB_APP:-bazaar_web.app:app}" --host 0.0.0.0 --port "${PORT:-8080}" &
pids+=("$!")

status=0
wait -n "${pids[@]}" || status=$?
if (( stopping )); then
    wait || true
    say "stopped."
    exit 0
fi
say "a process exited with status $status; stopping the other."
stop
wait || true
exit $(( status == 0 ? 1 : status ))
