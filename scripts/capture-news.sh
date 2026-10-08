#!/bin/bash
# Daily news snapshot. Saves the trailing three days of Alpaca news for the configured
# companies into a new dated version under data/raw/alpaca-news/, so later revisions of an
# article can be compared against what was served today.
#
# Installed as a launchd job by scripts/install-news-capture.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) capture-news"
exec uv run --env-file .env python -m bazaar_market.sources capture-news --days 3
