#!/usr/bin/env bash
# cron / launchd entry point.  Example crontab line (weekdays 6:30 pm local time):
# 30 18 * * 1-5 /path/to/portfolio-pulse/scripts/run_daily.sh >> /path/to/portfolio-pulse/logs/cron.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
python -m portfolio_pulse run-daily
