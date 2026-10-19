# Phase 3 — FX rates and Portfolio Pulse engine v2

Educational analytics only. Not investment advice.

## FX rates (Bank of Canada Valet)
- Series: FXUSDCAD (CAD per 1 USD). Fetched by `fetch-fx` and inside `run-daily`.
- Cached: each run fetches only dates after the latest stored rate. Re-runs add 0 rows.
  Offline: logs a warning; `run-daily` continues, `fetch-fx` exits 2.
- Only real observations are stored. Weekend/holiday handling happens when a rate is read:
  - rate on the date → `reported`
  - latest earlier rate within 5 calendar days → `carried_forward`
  - older than 5 days → `stale` (still converted, flagged)
  - no earlier rate → `missing` (CAD value NULL)
  - CAD amounts → `identity`
  - A later rate is never used (no look-ahead).
- Demo rows (source `synthetic`) use DEMO-USDCAD rates only; all other rows use BoC.

## CAD reporting views (native values are never changed)
- `v_snapshot_cad`, `v_holding_cad`, `v_activity_cad`: CAD value plus rate, rate date, gap and FX status.
- Rate lookup is an as-of join: `v_fx_rate_cad` gives each rate a "valid until" date (LEAD).
- `v_activity_cad.fx_conversion_cost_cad` = both sides of an FX_CONVERSION in CAD at BoC
  (negative = cost).

## Performance engine v2 (`performance.py`, stored by `performance_store.py`)
- **Account, native currency:**
  - Change = V(t) − V(t−1) − F
  - Return = change / (V(t−1) + F)
  - F = external flows (deposit, withdrawal, transfer in/out); flows arrive at the start of the period.
  - Dividends, interest, fees and tax are part of return. Trades and FX conversions are not
    flows, so a conversion cost is an investment loss.
- **Account, CAD:**
  - V(t) uses rate R1 (that day).
  - V(t−1) and the period's flows use R0 (previous snapshot).
  - Therefore (1 + r_CAD) = (1 + r_native) × (R1 / R0).
  - Currency effect = R1 / R0 − 1.
  - FX dollars = (V(t−1) + F) × (R1 − R0); market dollars = the rest.
- **Household:**
  - Sum account dollars, then divide (never average percentages).
  - Households: DEMO (synthetic), PAPER (paper accounts), REAL.
  - The return is blank if any account lacks a CAD return that day.
- **Own-account transfers:** a TRANSFER_OUT and TRANSFER_IN are paired when they are in
  different accounts of the same household, same currency, within $0.01 and within 7 days.
  Paired legs are excluded from household contributions. Unmatched transfers stay external.
- **TWR** = Π(1 + r) − 1.
- **Stored in** `perf_account_daily` and `perf_household_daily`, rebuilt atomically by
  `performance` and `run-daily`.

## Source precedence and quality
- `v_snapshot_best` picks one value per account-day:
  1. Broker-reported via SnapTrade (or synthetic) = precedence 1
  2. CSV import = 2
  3. Reconstructed (holdings × prices) = 3
- Reconstructions with `priced_share` < 1 are never used.
- Reported vs reconstructed differing by more than 0.5% = `mismatch` (reported wins).
- Quality label (worst wins): DEMO > MISSING > STALE > estimated > reported.
  Shown in `v_snapshot_cad`, `v_perf_account_daily` and `v_perf_household_daily`.

## FX cost estimates (`fx_cost.py`)
- Buy foreign: CAD = foreign × rate / (1 − fee).
- Sell foreign: CAD = foreign × rate × (1 − fee).
- These match Wealthsimple's published example.
- Default fee: Wealthsimple's published 1.5% (checked 2026-10-19); 2.5% only as a labelled scenario.
- Brokers apply the fee to their own corporate rate, so real all-in cost versus BoC can be higher.