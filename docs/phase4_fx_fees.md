# Phase 4 — FX Fee Converter

Educational analytics only, not advice. The Bank of Canada (BoC) rate is an **indicative
reference rate** published once per business day; nobody can trade at it. Platform costs are
estimates built only from fees and quotes you imported with a source URL and a check date.

## What it answers
"If I convert this amount, how much do I lose versus the BoC reference rate, in CAD and basis points?"

    python -m portfolio_pulse fx-cost --amount 1000 --from CAD --to USD --platform wealthsimple [--date YYYY-MM-DD]

## Data entry (never from memory)
| File (git-ignored) | Kind | Table |
|---|---|---|
| `data/fees/fee_schedule.csv` | `fee` — a platform's percentage FX fee | `fee_schedule` |
| `data/fees/fx_posted_rate.csv` | `quote` — a bank's posted USD/CAD buy/sell rate for one date | `fx_posted_rate` (migration 006) |

Templates with DEMO rows: `examples/fees/*.example.csv`.

    python -m portfolio_pulse fees-import data/fees/fee_schedule.csv --kind fee --dry-run
    python -m portfolio_pulse fees-import data/fees/fee_schedule.csv --kind fee

Validation: https source URL required; `date_checked` not in the future; demo rows must use
example.com and real rows must not; FX fees need `applies_to` like `CAD<->USD` and `rate_bps`
(150 = 1.50%); quotes need buy ≥ sell, CAD per 1 USD between 0.9 and 2.0 (catches inverted entry).
Bad rows go to `data/rejects/<file>_rejects.csv` with line and reason. Re-importing adds 0 rows;
re-importing the same fee with a newer `date_checked` refreshes the date.

## Two cost models
- **percent** (e.g. Wealthsimple): platform rate = BoC / (1 − f) buying USD, BoC × (1 − f) selling.
  Label `estimated`: the real fee is applied to the platform's own corporate rate, not BoC.
- **posted_rate** (e.g. a bank): you buy USD at `customer_buy_rate`, sell at `customer_sell_rate`.
  Label `reported`. A quote is used only on its own `rate_date`, never carried to another day.

## Definitions ("I convert this amount" mode; amount is in the FROM currency)
- `cost_cad` = CAD value at the BoC rate of what you'd get − CAD value of what you actually get
- `cost_bps` = cost_cad / CAD notional at BoC × 10,000 → a 1.5% fee = 150 bps in both directions
- Negative cost = better than the reference rate.
- The older `estimate_buy_foreign` answers "I need exactly X USD" (1.5% → 152.28 bps). Don't mix them.

## Hand-worked examples (tests use an invented BoC rate of 1.3800)
| Case | Received at BoC | Received via platform | Cost CAD | bps |
|---|---|---|---|---|
| 150 bps, 1,000 CAD→USD | 724.64 USD | 713.77 USD | 15.00 | 150.00 |
| 150 bps, 500 USD→CAD | 690.00 CAD | 679.65 CAD | 10.35 | 150.00 |
| posted buy 1.44, 1,000 CAD→USD | 724.64 USD | 694.44 USD | 41.67 | 416.67 |
| posted sell 1.36, 500 USD→CAD | 690.00 CAD | 680.00 CAD | 10.00 | 144.93 |

## Rates, dates and quality labels
BoC via `fx_rates.rate_for` (Phase 3 rule): same-day → `reported`; prior rate ≤ 5 days → used and
labelled **STALE**; older or none → error ("run fetch-fx"). Worst label wins:
DEMO > STALE > estimated > reported. Missing rate, fee or quote side → a clear error, never 0.
A fee last checked more than 90 days earlier adds a re-check note.

## Power BI (export contract 1.2)
`dim_fee_schedule`, `dim_fx_posted_rate`, `fact_fx_cost` (institution × date × direction × amount
100 / 1,000 / 10,000). Every row comes from the same `convert()` as the CLI. Percent fees appear
from `effective_from` (or `date_checked`) onward; quotes only on their `rate_date`. Rows that can't
be computed are skipped and counted in the log. Average `cost_bps` weighted by `notional_cad`.

## Actual conversions (implied spread) — gated
Not built. As of 2026-10-20 the ledger holds only 2 DEMO `FX_CONVERSION` rows; no real executed
rates are verified (SnapTrade `fx_rate` direction unverified; Alpaca Paper is USD-only; CSV import
doesn't write `activity`). When real rows exist, apply the posted-rate formula to the executed rate.

## Known limits
- CAD↔USD only. Wealthsimple USD-account tiered conversion fees not modelled.
- `fees-import` is insert-only: correcting a wrong row needs a manual DELETE (no supersede path).
- The CLI will apply a percent fee to any date; only the export restricts dates to `date_checked` onward.
- Bank posted rates change during the day; one quote per date and channel.
- Demo FX rates aren't `boc_valet`, so a demo-only database exports `fact_fx_cost` header-only.