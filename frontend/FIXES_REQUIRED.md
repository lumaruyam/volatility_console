# Dashboard Fixes Required — Professor Requirement Gap Analysis

All fixes below were identified by comparing the live screenshots and source code
(`DataOverview.tsx`, `RiskAnalysis.tsx`, `StrategyExecution.tsx`, `Backtesting.tsx`,
`ShockSimulator.tsx`) against the professor's detailed dashboard specification.

---

## TAB 1 — Data Overview (`DataOverview.tsx`)

### 🔴 Hardcoded / Wrong Values

| Location | Issue | Fix |
|---|---|---|
| `EXPIRY_OPTIONS` const (line 64–68) | Only 3 hardcoded expiry dates. | Replace with data fetched from `/api/market/expiries?ticker={selected}`. Fall back to current hardcoded list on error. |
| `RATE (r)` in header (line 332) | Rate `"3.45%"` is hardcoded. | Fetch from `EngineStatus` or a dedicated `/api/market/rate` endpoint; show the live risk-free rate. |
| `LOADING_INDEX` spot prices (lines 71–122) | Stale hardcoded snapshot prices for all 50 stocks (e.g. `AIR: 183.68` but screenshot shows `273.1`). | These are shown only while the real API loads — acceptable **if** the API response overwrites them quickly. Add a visual `LOADING` shimmer on each row instead of fake live prices to avoid confusing data. |
| Options chain `QC` column (line 615–616, 627) | Always renders a green `CheckCircle2` icon regardless of `row.call_qc` / `row.put_qc` values. | Use the actual `call_qc` / `put_qc` field from the API: green check if `"ok"`, orange warning icon if `"warn"`, red ✗ if `"fail"`. |

### 🟡 Missing Features (required by professor)

| Feature | Professor Requirement | Implementation Notes |
|---|---|---|
| **Rho (ρ) Greek** in the 4-box summary | Prof requires Δ Γ V Θ **and ρ** displayed. | Add a 5th `MetricBox` for `greeks.total_rho` (add `total_rho` to `GreeksSummary` type and `/api/market/greeks-summary` response). |
| **Forward Prices table/chart** | "Calculated forward prices for each maturity (F = S₀·e^{(r−q)T})". | Add a collapsible panel below the options chain showing a small table: maturity → forward price. Fetch from `/api/market/forward-curve?ticker={selected}`. |
| **Futures Prices** | "Prices for futures across maturities". | Add a row of `MetricBox`-style tiles for front-month and next 3 maturities inside the forward curve panel. |
| **Greeks heatmap** | "Heatmap of Delta, Gamma, Vega, Theta across strikes/maturities". | Add a toggle on the options chain panel (table / heatmap). In heatmap mode use Recharts `<Bar>` with color cells or a CSS grid with background opacity driven by Greek magnitude. |
| **Bid-Ask Spread column** | "Bid-ask spreads" in the options chain. | Add `Spread` column to the chain table between the call and put sections: `((ask-bid)/((ask+bid)/2)*100).toFixed(2) + "%"`. Color red if spread > 5%. |
| **Liquidity threshold filter** | "Filter out illiquid options (volume < 100, OI < 500)". | Add a `LIQUIDITY` toggle to the filter sub-bar (line 501–538). When active, hide rows where `call_volume < 100 && put_volume < 100`. |
| ~~Strike Range filter (-30Δ · +30Δ)~~ | ~~Gemini flagged this as missing.~~ **Gemini is incorrect — this filter already exists.** `strikeFilter === "DELTA30"` is fully implemented (lines 132, 237–238, 521–522). The filter bar renders `−30Δ · +30Δ` as a toggle button alongside `ATM ±10%` and `ALL STRIKES`. No action needed. |  — |
| **Time Range filter** | "Filter historical data by date range". | Add a date-range picker (or preset buttons: 1D, 1W, 1M, 3M, 1Y) that feeds into the `queryKey` of all historical fetches. |
| **Data export button** | "Allow exporting data to CSV". | Add an export button in the options chain panel header. On click, serialise `filteredChain` to CSV and trigger a browser download. |
| **Auto-refresh indicator** | "Auto-refresh data every 5–10 seconds". | `staleTime` is already 15 s for the chain but there is no `refetchInterval`. Add `refetchInterval: 15_000` to `chainData` and `surface` queries, and show a small "last updated Xs ago" badge. |

---

## TAB 2 — Risk Analysis (`RiskAnalysis.tsx`)

### 🔴 Hardcoded / Wrong Values

| Location | Issue | Fix |
|---|---|---|
| System time header (line 191) | `"09:41:22.015 GMT"` is **hardcoded**. | Replace with `new Date().toUTCString()` updated via a 1-second `setInterval`, or fetch from a `/api/system/time` heartbeat. |
| `Eq. Shrs` (line 149) | Hardcoded `"125,400"` shares. | Replace with `Math.round(greeks.portfolio_delta / selectedSpot).toLocaleString()` where `selectedSpot` comes from the index API. |

### 🔴 Greek KPI Sub-Label Bugs (confirmed by code inspection — flagged by Gemini review)

> Gemini's observation was **correct**. Three KPI tiles have broken secondary labels. Two duplicate the main value verbatim; one has a unit scaling issue that creates a visible mismatch with the PnL grid.

| KPI tile | Line | Current sub (broken) | What it actually shows | Correct fix |
|---|---|---|---|---|
| **Gamma** | 150 | `["Shares", fmtGreek(greeks.gamma)]` | The label says "Shares" but the value is **identical to the main KPI** (`-1.24M`). "Shares" is also the wrong unit for Gamma (which is Δ per underlying point, not a share count). | Change to `["Δ per 1% spot", fmtGreek(greeks.gamma / 100)]` — this shows how much delta changes for a 1% spot move, which IS a meaningful share-equivalent quantity. |
| **Theta** | 153 | `["1 Day Decay", fmtGreek(greeks.theta)]` | Same bug — sub value is **identical to the main KPI** (`-12.5K`). "1 Day Decay" implies a *different* breakdown number, not a repeat. | Change to `["Hourly", fmtGreek(greeks.theta / 8)]` (theta per trading hour) or `["Weekly", fmtGreek(greeks.theta * 5)]`. |
| **Vega** | 152 | `["1% Vol Shock", fmtGreek(greeks.vega / 100)]` | The sub is correctly scaled (÷100). BUT: Vega KPI shows `+850.4K`, implying `+8.5K` per 1% vol. The **PnL Attribution Grid directly below shows Vega PnL = +850** — a **1000× discrepancy**. | Investigate the `/api/risk/pnl-attribution` endpoint: `vega_pnl` is likely returned in **thousands of euros** (i.e. `850` = `€850K`) while all other values are in euros. If so, multiply `pnl.vega_pnl` by `1000` when constructing `pnlRows`, or fix the API to return a consistent unit. |

**Why these matter for the professor:** Greek KPIs are the first thing a risk professor checks. A Gamma tile showing the same number twice with a wrong label, and a Vega PnL bar 1000× smaller than the sensitivity, will immediately signal either sloppy implementation or a fundamental misunderstanding of Greeks scaling.

### 🟡 Missing Features

| Feature | Professor Requirement | Implementation Notes |
|---|---|---|
| **Rho PnL bar** in PnL Attribution | Prof requires Δ Γ V Θ **ρ** decomposition. | Add `{ l: "Rho", v: pnl?.rho_pnl ?? 1_200 }` to `pnlRows`. |
| **VaR fallback values** | VaR boxes show "—" when API is unavailable. | Add static fallback: `FB_VAR: VarData = { "1d_95": -198_900, "1d_99": -56_800_000, "7d_99": -150_300 }` and use `useQuery({ placeholderData: FB_VAR })`. |
| **UAM progress bar / gauge** | "Gauge or progress bar" for margin utilisation. | In the UAM panel footer (line 298–304), add a colour-coded progress bar: `<div style={{width: uam.uam_pct*100+"%"}} className={uam.uam_pct > 0.9 ? "bg-red-500" : uam.uam_pct > 0.8 ? "bg-yellow-400" : "bg-emerald-500"} />`. Colours: green < 80%, yellow 80–90%, red > 90%. |
| **Portfolio selector** | "Filter by portfolio (Straddle, Dispersion, Calendar)". | Add a dropdown at the top of the page that sets a `portfolio` state used in all query keys. |
| **Liquidity metrics section** | "Volume, open interest, bid-ask spreads" widget. | Add a small table panel below the correlation matrix showing top-10 options by bid-ask spread (from `/api/risk/liquidity`). |
| **Time horizon filter for VaR** | "Filter VaR by time horizon (1d, 1w, 1m)". | Add toggle buttons above the VaR strip; pass the selected horizon to the VaR query. |

---

## TAB 3 — Strategy Execution (`StrategyExecution.tsx`)

### 🔴 Hardcoded / Wrong Values

| Location | Issue | Fix |
|---|---|---|
| `latency` variable (line 144) | `"4ms"` hardcoded. | Fetch from `/api/strategy/latency` or from the `orderbook` response metadata. |
| `FB_ORDERS` timestamps (lines 67–71) | `"10:42:01"` etc. hardcoded. | Acceptable as fallback only; already overwrites on API load. Add `new Date().toLocaleTimeString()` to fallback generation to make them look live. |
| `executeMutation` (line 241) | `strategy_id: "strat_001"` **hardcoded** for all suggestion executions. | Pass the active/selected `strategy_id` from a `useState<string>` set when the user clicks a strategy card. |
| Strategy card `fields` — no Greeks | Only shows Name, Strike, Expiry, OI, Margin, PnL. Prof requires Greeks per strategy. | Add `total_delta`, `total_vega` to the `StrategyPosition` type and display them in the fields grid. Fetch augmented data from the API. |
| **Dispersion Basket `target_strike: "N/A"`** (line 58, `FB_POSITIONS`) | The fallback (and likely the live API) returns `"N/A"` for the Dispersion Basket's target strike. A real dispersion strategy tracks a basket of constituent strikes vs. the index anchor — `N/A` signals a placeholder, not live data. | Either: (a) extend `StrategyPosition` with a `constituent_strikes: {ticker: string; strike: number}[]` array and render a mini-table in the card when the strategy type is `"DISPERSION"`, or (b) at minimum display the index anchor strike (e.g. `"SX5E 4200"`) and note `"Basket: see constituents"`. The API endpoint `/api/strategy/positions` must be updated to return real constituent data. |

### 🟡 Missing Features

| Feature | Professor Requirement | Implementation Notes |
|---|---|---|
| **Manual trade ticket** | "Buttons to buy/sell/roll/close positions directly from the dashboard" — the prof also implies custom order entry: quantity, limit price, Buy/Sell direction for routing to IBKR. | Add a collapsible `ORDER TICKET` panel (or modal) triggered by a `[NEW ORDER]` button in the header. Fields: Underlying selector, Instrument (option/future/stock), Direction (BUY/SELL), Quantity (number input), Order Type (MARKET/LIMIT), Limit Price (number input, shown only for LIMIT), Destination (IBKR). On submit, call `POST /api/strategy/order`. This is required to route custom orders beyond the automated hedge/roll macros. |
| **Rolling countdown** | "Flag strategies that need rolling (e.g. 'Straddle expires in 9 months—roll now?')". | Parse `p.expiry` to compute days until expiry. If < 90 days, show a `⚠ Roll in X days` badge in amber on the strategy card header. |
| **Strategy type / status filters** | Dropdown filters: Straddle, Butterfly, Dispersion; Open/Closed/Rolled/Pending. | Add a filter bar above the strategy cards grid with `TYPE` and `STATUS` dropdowns. Filter the `positions` array client-side. |
| **Maturity filter** | "< 3m, 3-6m, 6-12m, > 12m". | Add to the filter bar above. Parse `p.expiry` string to bucket into maturity ranges. |
| **Wide spread alert threshold display** | "Highlight wide spreads (bid-ask > 5%) in red". | Already highlights rows with `o.wide` (red background), but the 5% threshold is implicit. Add a tooltip or legend note: `"Wide spread: > 5% of mid"`. |
| **Position Greeks summary banner** | "Net Delta, Gamma, Vega, Theta for the portfolio" — aggregated across all strategies. | Add a 4-cell KPI strip at the top of the Execution tab summing Greek exposure across all positions. |

---

## TAB 4 — Backtesting (`Backtesting.tsx`)

### 🔴 Hardcoded / Wrong Values

| Location | Issue | Fix |
|---|---|---|
| `start_date` in `useEffect` (line 119) | Hardcoded `"2005-01-01"`. | Replace with a reactive `startDate` state driven by a time-range selector (see below). |
| `FB_STATS.vs_benchmark_pct` (line 54) | Shows `1.2` but screenshot shows `+262.9` — mismatch between fallback and live. | Update fallback to a representative recent value, or hide the "v BM" badge when using fallback stats (add an `isLive` flag). |
| `stratList` fallback (line 87) | Only `["VOL_CARRY_01", "SX5E_STRADDLE", "DISPERSION_Q3"]` — not labelled as fallback. | Add comment and consider fetching with a longer `staleTime` so it is always fresh. |

### 🟡 Missing Features

| Feature | Professor Requirement | Implementation Notes |
|---|---|---|
| **Time range filter / date picker + custom range for shock presets** | "Filter backtest by date range (1y, 3y, 5y, custom)". The shock preset chips (`2008 Crash`, `2020 Liquidity Shock`, etc.) currently toggle a static named profile that overrides `start_date`/`end_date` server-side. There is no way to enter a custom date range. The `start_date` is also hardcoded to `"2005-01-01"` (line 119) regardless of the selected preset. | (a) Add preset buttons (1Y / 3Y / 5Y / MAX) that set reactive `startDate`/`endDate` state. (b) Add two `<input type="date">` fields for a fully custom range. (c) When a shock preset is selected, automatically set the date window to match the relevant crisis period (e.g. `2008 Crash` → 2008-01-01 to 2009-12-31) and allow the user to override it. Pass `start_date` and `end_date` from state, never hardcoded. |
| **Underlying asset filter** | "Filter by underlying (Euro Stoxx 50, S&P 500)". | Add a dropdown for underlying. Pass it in the backtest request body and in the Monte Carlo query key. |
| **Greeks over time charts** | "Delta, Gamma, Vega, Theta evolution during backtest as line charts". | Add a second `section` below the equity/MC charts with 4 small `<LineChart>` panels (one per Greek), fed by `result.greeks_over_time` (add this field to `BacktestResult` type and the API). |
| **Probability of profit** | "Probability of positive PnL (e.g. 65%)". | Compute from `mcReturns`: `(mcReturns.filter(r => r > 0).length / mcReturns.length * 100).toFixed(1) + "%"`. Add as a 5th stat card: **Prob. Profit**. |
| **Average trade PnL** | "Average PnL per trade". | Add `avg_trade_pnl_eur` to `BacktestStats` type and display as a 6th stat card. |
| **Cross-hair / hover on equity chart** | Prof implies interactive charts with date tooltips. | The `<Tooltip>` is already present. Enhance with a vertical `<ReferenceLine>` that follows mouse position (use Recharts `onMouseMove`). |

---

## TAB 5 — Shock Simulator (`ShockSimulator.tsx`)

### 🔴 Hardcoded / Wrong Values

| Location | Issue | Fix |
|---|---|---|
| `clientCellPnl` (lines 58–63) | Delta `0.32 * 500_000`, Gamma `28_000`, Vega `120_000` all hardcoded. Used when API is unavailable. | Fetch portfolio Greeks once from `/api/risk/greeks` on mount and use them in the fallback calculation. Store in a `useRef` so the debounced effect can access them. |

### 🟡 Missing Features

| Feature | Professor Requirement | Implementation Notes |
|---|---|---|
| **Correlation shock slider** | "Correlation shock" as a 4th shock parameter. | Add a 4th `ShockSlider` for correlation shock (range −1 to +1, step 0.05). Include `corr_stress` in the `RepriceRequest` type and pass to the API. |
| **Volatility surface before/after visualisation** | "Plot the vol surface before/after the shock to visualise changes". | Add a toggle `[SURFACE SHIFT VIEW]` in the panel header. When active, embed the `VolSurface3D` component twice (side by side or toggled), passing both the baseline and the shocked surface (fetch from `/api/shock/surface-before-after`). |
| **Hedging suggestions panel** | "Suggest actions to mitigate risk after the shock". | After each reprice call, display a `HedgeSuggestions` sub-panel (similar to `StrategyExecution`) below the scenario matrix. Parse the `RepriceResponse` for a `hedging_suggestions` array field. |
| **Liquidity impact section** | "Bid-ask spreads and volume changes under the shock". | Add a small table showing top-5 options most affected by the shock (widened spreads). Fetch from `/api/shock/liquidity-impact` alongside the reprice call. |
| **Portfolio selector filter** | "Filter by portfolio (Straddle, Dispersion, Calendar)". | Add a `PORTFOLIO` dropdown at the top of the page; pass `portfolio_id` in the `RepriceRequest`. |
| **Asset class filter** | "Filter by asset class (options, futures, stocks)". | Add `ASSET CLASS` toggle chips (Options / Futures / Stocks); pass in the reprice body. |

---

## Cross-Cutting / Global Issues

| Issue | Affected Files | Fix |
|---|---|---|
| **No `refetchInterval` on market data** | `DataOverview.tsx` | Add `refetchInterval: 10_000` to `chainData`, `surface`, `greeks` queries so they auto-refresh without user interaction. |
| **No error states shown to user** | All 5 tabs | Wrap all `useQuery` calls with `isError` handling: show a red `⚠ API ERROR — showing cached data` banner when `status === "error"`. |
| **No loading skeletons** | All 5 tabs | Replace empty `undefined` / fallback states with a consistent skeleton (`<div className="animate-pulse bg-zinc-800 h-4 rounded" />`) while the first fetch is in-flight (`isLoading && !data`). |
| **Hardcoded ticker in correlation matrix title** | `RiskAnalysis.tsx` line 259 | "Euro Stoxx 50 Component Correlation Matrix" is hardcoded. If a different portfolio is selected it should say the correct underlying. Make it reactive. |
| **`VolSurface3D` axis label note** | `VolSurface3D.tsx` line 399 | The header shows `z = IV% · x = STRIKE · y = MATURITY` but in 3D convention z is depth — this confuses viewers. Fix the header copy to match the actual axis mapping in the Three.js scene: `x = STRIKE · z = MATURITY · y = IV%`. |
| **`RATE (r)` mismatch** | `DataOverview.tsx` header | Header shows `3.45%` but the engine status API returns `status.calibration.rmse` only, not the rate. Wire to a real rate endpoint or move the rate into `EngineStatus`. |

---

## Priority Order for Implementation

1. **🔴 P1 — Correctness bugs** (wrong values displayed):
   - System time hardcoded in Risk tab
   - QC column always green in chain
   - `executeMutation` hardcoded `strategy_id`
   - `clientCellPnl` hardcoded Greeks

2. **🟠 P2 — Missing required KPIs** (prof will check):
   - Rho (ρ) in Data Overview Greeks summary
   - Rho in PnL attribution
   - UAM progress bar/gauge
   - Rolling countdown on strategy cards
   - Probability of profit in Monte Carlo
   - Greeks over time in Backtesting

3. **🟡 P3 — Missing filters** (interactivity):
   - Time range filter in Backtesting
   - Portfolio selector in Risk & Shock tabs
   - Liquidity threshold filter in Data Overview
   - Strategy type/status filters in Execution

4. **🟢 P4 — Nice-to-have** (polish):
   - Data export CSV
   - Error/loading states
   - Vol surface before/after in Shock Simulator
   - Greeks heatmap view in Data Overview
   - Correlation shock slider

---

## TAB 6 — Orders Tab (MISSING ENTIRELY)


> **Source of comparison:** Dhia's `vol_infra` dashboard (`dashboard/app.js` — `renderOrders()` function, lines 270–291).
> His dashboard has a dedicated **Orders tab** with a KPI strip + full blotter table.
> **Your dashboard has no equivalent tab.** `StrategyExecution.tsx` shows an order book widget embedded inside the strategy page, but it is scoped to strategy suggestions — not a standalone OMS blotter.

### 🔴 Missing — Entire Orders Tab

| What Dhia has | What you have | Gap |
|---|---|---|
| **Orders tab** as top-level navigation item | No dedicated Orders tab (only embedded orderbook inside Execution) | Add `Orders` tab to the nav (alongside Data Overview, Risk Analysis, etc.) |
| **KPI strip**: Total Orders / Staged / Working / Filled / Rejected counts | None — no order status counters anywhere | Add a KPI strip with 5 status counts |
| **Order Blotter table**: Status badge · Order ID · Side · Qty · Instrument (Expiry + Strike + Right) · Type + Limit · Filled Qty · Reason | None | Add a full-width sortable table. Color-code `status` badge: green=filled, orange=staged/submitted, red=rejected |
| **Order status lifecycle**: staged → submitted → partial → filled / cancelled / rejected | No lifecycle display | Show current status per order with badge colours matching the lifecycle |
| **Instrument formatting**: `SPX 20260717 5475C` (underlying + expiry + strike + right) | Execution tab shows `Strategy` level, not individual leg contracts | Display each order leg individually with full contract spec |

### Implementation Plan (new `Orders.tsx` component)

```
frontend/src/components/terminal/views/Orders.tsx   ← NEW FILE
```

**Structure:**
1. KPI strip (5 tiles):
   - `Orders` (total count)
   - `Staged` (status = staged)
   - `Working` (submitted + partial)
   - `Filled` (status = filled)
   - `Rejected` (rejected + cancelled)

2. Filter bar: All / Staged / Working / Filled / Rejected (toggle buttons)

3. Order Blotter table columns:
   - `Status` — badge with colour coding
   - `Order ID` — monospace, truncated to last 8 chars
   - `Side` — BUY (green) / SELL (red)
   - `Qty` — integer
   - `Instrument` — `{underlying} {expiry} {strike}{right}` e.g. `SPX 20260717 5475C`
   - `Order Type` — `LMT @118.50` or `MKT`
   - `Filled` — `{filled_qty}/{qty}`
   - `Fill Price` — avg_fill_price when filled, `—` otherwise
   - `Reason` — truncated error message for rejected orders

4. Data source: `GET /api/orders/blotter` → returns latest state per `order_id`
   - Fallback: seed with `seed_fake_portfolio.py` output (already done for Dhia's project)
   - Same Parquet schema as `vol_infra` orders table

**Badge colour map:**
```tsx
const STATUS_COLORS = {
  staged:    "bg-zinc-600 text-zinc-200",
  submitted: "bg-blue-500/20 text-blue-300 border border-blue-500/40",
  partial:   "bg-yellow-500/20 text-yellow-300",
  filled:    "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40",
  cancelled: "bg-zinc-700 text-zinc-400",
  rejected:  "bg-red-500/20 text-red-400 border border-red-500/40",
}
```

---

## Additional Gaps Found by Comparing Dhia's Dashboard

### Risk Tab — Missing from your version

| Dhia has | Your version | Fix |
|---|---|---|
| **Interactive custom shock sliders** (spot move %, vol shift pts, days roll) with live local-Greeks P&L recalculation — updates on every slider move (`oninput`) | Shock Simulator is a separate tab; Risk tab has only a static UAM grid | Add a `Custom Shock` mini-panel inside `RiskAnalysis.tsx` (above the UAM grid): 3 sliders (spot %, vol pts, days) + `EST. P&L (LOCAL APPROX)` display + bar chart of delta/gamma/vega/theta contributions. Use local Greek math: `ΔPnL ≈ Δ·dS + ½Γ·dS² + ν·dVol + Θ·dT`. This is the same formula Dhia uses in `renderShock()`. |
| **Positions table** at bottom of Risk tab: Contract / Qty / Mkt Value / Avg Cost / Unrealised P&L | No positions table in Risk tab | Add a `Positions` panel at the bottom of `RiskAnalysis.tsx` fetching from `/api/risk/positions`. Columns: Contract · Qty · Mkt Value · Avg Cost · Unreal P&L. Color Unreal P&L green/red. |
| **Risk Aggregates table** grouped by `by_underlying`, `by_expiry`, `portfolio` — showing Net Δ, Net Vega, Net Θ, $Δ, $Vega, Mkt Val, # positions | `RiskAnalysis.tsx` has Greek KPI tiles only — no breakdown table | Add a `Risk Aggregates` panel with a table. Groups: by_underlying / by_expiry / portfolio TOTAL. Matches Dhia's `agg-table` output exactly. |

### Surface Tab — Minor gap

| Dhia has | Your version | Fix |
|---|---|---|
| **Smile chart** (raw IV points vs SVI fitted curve) per expiry with a dropdown to switch expiry | `DataOverview.tsx` shows the 3D surface and chain table, but no 2D smile slice view | Add a collapsible `Smile — raw vs fitted` panel below the chain table. Show scatter (raw IV points) + line (SVI fit) for the selected expiry. Data from `/api/market/smile?expiry={date}`. |
| **ATM vol term structure** chart (ATM vol vs days to expiry as a line) | Not present in your dashboard | Add as a second chart alongside the Forward Curve panel. Fetch ATM vol per expiry from `/api/market/surface-params`. |

---

## Updated Priority Order

1. **🔴 P1 — Correctness bugs** (wrong values displayed):
   - System time hardcoded in Risk tab
   - QC column always green in chain
   - `executeMutation` hardcoded `strategy_id`
   - `clientCellPnl` hardcoded Greeks
   - **[NEW] Gamma / Theta sub-label duplicates**

2. **🟠 P2 — Missing required features** (prof will check, Dhia has these):
   - **[NEW] Orders tab — entire tab missing**
   - **[NEW] Interactive shock panel inside Risk tab** (Dhia has it)
   - **[NEW] Positions table in Risk tab**
   - **[NEW] Risk Aggregates table in Risk tab**
   - **[NEW] Smile chart (raw vs fitted) in Surface tab**
   - **[NEW] ATM vol term structure chart**
   - Rho (ρ) in Data Overview Greeks summary
   - Rho in PnL attribution
   - UAM progress bar/gauge
   - Rolling countdown on strategy cards
   - Probability of profit in Monte Carlo

3. **🟡 P3 — Missing filters** (interactivity):
   - Time range filter in Backtesting
   - Portfolio selector in Risk & Shock tabs
   - Liquidity threshold filter in Data Overview
   - Strategy type/status filters in Execution

4. **🟢 P4 — Nice-to-have** (polish):
   - Data export CSV
   - Error/loading states
   - Vol surface before/after in Shock Simulator
   - Greeks heatmap view in Data Overview
   - Correlation shock slider

---

## BACKEND GAPS — From professor's PDF requirements
> Cross-reference: `subject/1780037915_industrial_roadmap_volatility_infrastructure_v4.pdf`

### 🔴 CRITICAL — Will fail professor's acceptance tests

#### B-FIX-1: All EOD pipeline jobs raise `NotImplementedError`
**File:** `backend/src/orchestration/jobs.py`  
**PDF reference:** Part III Steps 3–12, Part XII

The following jobs all raise `NotImplementedError`:
`job_build_snapshots`, `job_build_forwards`, `job_solve_iv`, `job_fit_surfaces`,
`job_compute_greeks`, `job_risk_aggregation`, `job_run_scenarios`, `job_live_collect`,
`job_universe_refresh`, `job_eod_reconciliation`, `job_incremental_analytics`.

All individual library modules exist and work in isolation (`forwards/engine.py`, `iv/solver.py`, `surfaces/calibration.py`, etc.), but **the full EOD pipeline cannot run end-to-end**.

**Fix:** Replace each `raise NotImplementedError` with real calls to the existing library. Example:
```python
def job_build_forwards(run, reader, writer, metrics=None):
    from src.forwards.engine import estimate_forward_curve
    snapshots = reader.load_snapshots(run.trade_date)
    for snapshot in snapshots:
        result = estimate_forward_curve(snapshot, config)
        writer.write_forward_curve(run.trade_date, result, run.code_version)
    return {"status": "ok", "count": len(snapshots)}
```

---

#### B-FIX-2: `basket_variance.py` module missing entirely
**PDF reference:** Part II, Equation 23 — "Index or basket variance identity"

The PDF explicitly requires a module that accepts weights, constituent vols, and optional pairwise correlations, then returns basket variance and residual metrics. **This module does not exist anywhere in the codebase.**

**Fix — CREATE `backend/src/analytics/__init__.py` (empty) + `backend/src/analytics/basket_variance.py`:**
```python
"""Basket variance identity — PDF Part II Equation 23.
   sigma2_basket = sum_ij w_i * w_j * sigma_i * sigma_j * rho_ij
"""
from dataclasses import dataclass
import math

@dataclass(frozen=True)
class BasketVarianceResult:
    basket_variance: float
    basket_vol: float
    weighted_component_vars: list[float]
    residual_vs_atm: float   # basket_vol - index_atm_vol
    avg_corr_used: float
    n_constituents: int

def compute_basket_variance(weights, vols, corr_matrix=None, avg_corr=None, index_atm_vol=None):
    n = len(weights)
    if corr_matrix is None:
        rho = avg_corr if avg_corr is not None else 0.5
        corr_matrix = [[rho if i != j else 1.0 for j in range(n)] for i in range(n)]
        avg_corr_used = rho
    else:
        off = [corr_matrix[i][j] for i in range(n) for j in range(n) if i != j]
        avg_corr_used = sum(off) / len(off) if off else 1.0
    basket_var = sum(
        weights[i] * weights[j] * vols[i] * vols[j] * corr_matrix[i][j]
        for i in range(n) for j in range(n)
    )
    basket_vol = math.sqrt(max(0.0, basket_var))
    return BasketVarianceResult(
        basket_variance=basket_var, basket_vol=basket_vol,
        weighted_component_vars=[w * w * v * v for w, v in zip(weights, vols)],
        residual_vs_atm=basket_vol - index_atm_vol if index_atm_vol else 0.0,
        avg_corr_used=avg_corr_used, n_constituents=n,
    )
```
Also add `GET /api/risk/basket-variance` to `risk.py` router and a unit test.

---

### 🟡 MEDIUM — Missing documentation (prof reads Appendix C carefully)

#### B-FIX-3: Module READMEs missing from all `src/` packages
**PDF reference:** Appendix C — "module_READMEs/ — one README per major package explaining public APIs and failure modes"

No `README.md` exists in any `backend/src/` subdirectory. Create one per package (10–20 lines each):
`connectivity/`, `snapshots/`, `forwards/`, `iv/`, `surfaces/`, `pricing/`, `risk/`, `qc/`, `orchestration/`, `storage/`, `analytics/`

#### B-FIX-4: Regression test library is empty
**PDF reference:** Part XVI — "Create a curated library of replay days. This is one of the highest-leverage investments the team can make."

`backend/tests/regression/` has only `__init__.py`. Create:
```
tests/regression/fixtures/
  calm_day/              raw_events.jsonl + expected_surface.json
  event_heavy/           raw_events.jsonl + expected_surface.json
  sparse_liquidity/      few accepted quotes; PCHIP fallback triggered
  disconnect_recovery/   split event files for kill-and-restart test
test_calm_day_regression.py
test_sparse_regression.py
```

#### B-FIX-5: Document filenames don't match PDF spec (Appendix C)
| PDF says | Current file | Fix command |
|---|---|---|
| `known_limitations.md` | `backend/docs/limitations.md` | `mv backend/docs/limitations.md backend/docs/known_limitations.md` |
| `operating_runbooks.md` | `backend/RUNBOOKS.md` | `mv backend/RUNBOOKS.md backend/docs/operating_runbooks.md` |

---

### 🟢 LOW — Naming & polish

**B-FIX-6:** CREATE `backend/src/qc/checks.py` — thin re-export facade (PDF Part XII names this file explicitly; logic lives in `quote_filter.py` + `validation.py`):
```python
from src.qc.quote_filter import run_quote_qc, filter_chain
from src.qc.validation import run_daily_qc, DailyQCReport, build_triage_table
```

**B-FIX-7:** Extract `shock_date_range()` from `engine.py` to `backend/src/backtest/shock_presets.py`.

**B-FIX-8:** Add `data_mode: "live" | "seeded" | "synthetic"` to `GET /api/market/engine-status` response and show as a badge in the frontend header so the professor knows which data mode is active during the demo.

---

### Backend Fixes Summary

| # | Severity | Description | Effort |
|---|---|---|---|
| B-FIX-1 | 🔴 CRITICAL | Wire 11 NotImplementedError jobs in `orchestration/jobs.py` | 2–3 days |
| B-FIX-2 | 🔴 CRITICAL | CREATE `analytics/basket_variance.py` + `__init__.py` + test + API endpoint | 2 hours |
| B-FIX-3 | 🟡 MEDIUM | CREATE `README.md` in 10+ src/ packages | 1 hour |
| B-FIX-4 | 🟡 MEDIUM | CREATE `tests/regression/fixtures/` with 4 replay datasets | 3 hours |
| B-FIX-5 | 🟡 MEDIUM | Rename docs to match PDF spec exactly | 5 min |
| B-FIX-6 | 🟢 LOW | CREATE `qc/checks.py` re-export facade | 10 min |
| B-FIX-7 | 🟢 LOW | Extract `shock_date_range()` to `backtest/shock_presets.py` | 10 min |
| B-FIX-8 | 🟢 LOW | Add `data_mode` badge to engine-status API + frontend header | 30 min |
