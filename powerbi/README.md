# Portfolio Pulse: Power BI project (PBIP)

Text-based Power BI project: the model (TMDL) and the report (PBIR) are plain files, so every
change shows up in a git diff. **No data is stored in these files.** Data is loaded from the
CSV folder named by the `DataFolder` parameter when you click Refresh.

## Open it
1. Double-click `PortfolioPulse.pbip` (Power BI Desktop, October 2026 or later).
2. Click **Refresh** on the Home tab. The pages are empty until you do.
3. Demo folder by default: `C:\Users\rishi\Websites\portfolio-pulse\data\powerbi_demo\`.
   To change it: Home > Transform data > Edit parameters > `DataFolder` (keep the trailing backslash).

## What is inside
- `PortfolioPulse.SemanticModel/`: 18 contract tables, `check_totals`, the what-if table, 18 relationships,
  61 measures in 8 display folders.
- `PortfolioPulse.Report/`: 9 pages: Overview, Calendar, Day detail, Cash flows, FX & fees, News,
  Data quality, How this works, Reconciliation.

## Reconciliation page
```powershell
python scripts\reconcile_powerbi.py --db data\demo\pulse_demo.db --out data\powerbi_demo
```
Then Refresh. Every check should show PASS.

## Sharing rule
Only share a `.pbix` saved from this project while `DataFolder` points at `data\powerbi_demo\`
and `python -m portfolio_pulse.demo_gate data\powerbi_demo` exits 0.
`.pbi/cache.abf` holds loaded data and is git-ignored here.
