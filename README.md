# Portfolio Pulse

An educational, local-first portfolio analytics project: daily portfolio changes that
separate **contributions** from **investment performance**, a calendar of the biggest up
and down days, and a watchlist news feed with links to original articles.

> Educational analytics only. Not investment advice, not a trading system. Demo data is
> synthetic and labelled as such everywhere it appears.

## What's inside

```
portfolio-pulse/
├── sql/schema.sql                  tables + v_daily_performance view
├── src/portfolio_pulse/
│   ├── db.py                       connection, schema, sources, batches, stable IDs
│   ├── synthetic.py                reproducible demo portfolio, cash flows, news
│   ├── csv_import.py               mapping-driven CSV importer with validation
│   ├── news.py                     Finnhub API / Google News RSS / demo fallback
│   ├── export.py                   CSVs for Power BI + data-quality table
│   └── cli.py                      python -m portfolio_pulse <command>
├── config/                         column-mapping examples (copy and edit)
├── sample_inputs/                  small CSVs with deliberate errors for testing
├── scripts/run_daily.bat|.sh       scheduler entry points
├── tests/                          pytest suite (20 tests)
└── data/                           inbox/ (your exports), powerbi/ (outputs) - git-ignored
```

## Setup

Requires Python 3.12+.

**Windows (PowerShell)**
```powershell
cd portfolio-pulse
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
copy .env.example .env
```

**macOS / Linux**
```bash
cd portfolio-pulse
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env
```

## Run it

```bash
python -m pytest -q                         # 20 tests should pass
python -m portfolio_pulse demo              # load synthetic data (re-run: 0 new rows)
python -m portfolio_pulse export            # writes data/powerbi/*.csv
```

Then open Power BI Desktop → Get data → Folder (or Text/CSV) → `data/powerbi/`.

### Your own data (optional)

1. Download an activity export from your broker's website yourself.
2. Put it in `data/inbox/`.
3. Copy `config/column_map.example.json` to `config/column_map.json` and replace the
   example header names with your file's actual headers. Map each transaction-type code
   you see to DEPOSIT / WITHDRAWAL / TRANSFER_IN / TRANSFER_OUT / DIVIDEND / INTEREST / FEE,
   or list it under `ignore_types` (buys and sells).
4. Validate first: `python -m portfolio_pulse import-csv data/inbox/<file>.csv --map config/column_map.json --dry-run`
5. Fix the mapping until rejects make sense, then run without `--dry-run`.

Never put brokerage usernames, passwords or tokens in this project.

### News

* No setup: demo headlines (all start with `[DEMO]` and link to example.com).
* Finnhub: add a free key to `.env` as `FINNHUB_API_KEY`, then `python -m portfolio_pulse fetch-news`.
* Google News RSS (personal, non-commercial): `python -m portfolio_pulse fetch-news --source rss`.

### Schedule it

* Windows: Task Scheduler → Create Basic Task → Daily → Action "Start a program" →
  `scripts\run_daily.bat`. Tick "Run whether user is logged on or not" only if you need it.
* macOS/Linux: `crontab -e` and add the line shown at the top of `scripts/run_daily.sh`.
* Manual run any time: `python -m portfolio_pulse run-daily`.

Logs go to `logs/portfolio_pulse.log`.
