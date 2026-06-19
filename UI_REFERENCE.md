# UI Reference — Volatility Console

Albert School · AI for Algo Trading

This document explains every panel, filter, table column, and metric in the terminal. Each section covers one tab in the sidebar.

---

## Table of Contents

1. [Data Overview (DATA)](#1-data-overview)
2. [Risk Analysis (RISK)](#2-risk-analysis)
3. [Strategy Execution (EXEC)](#3-strategy-execution)
4. [Backtesting (BACK)](#4-backtesting)
5. [Shock Simulator (SHOCK)](#5-shock-simulator)
6. [Orders (OMS)](#6-orders)
7. [Concepts — Greeks](#7-concepts--greeks)
8. [Concepts — Risk Metrics](#8-concepts--risk-metrics)
9. [Concepts — Options Pricing](#9-concepts--options-pricing)

---

## 1. Data Overview

**Purpose:** Real-time market snapshot for the Euro Stoxx 50 universe. Shows spot prices, implied volatility, options chain, and the calibrated vol surface for any selected constituent.

### Sidebar — Euro Stoxx 50 Watchlist

A scrollable list of all 50 index constituents. Click any row to select that ticker — all panels on the right update immediately.

| Column | What it shows |
|--------|---------------|
| Ticker / Name | Exchange ticker and company name |
| Spot | Last traded price in EUR (refreshes every 30 s) |
| ATM Vol | At-the-money implied volatility as a % — colour-coded: green < 15%, red > 25% |

The sidebar is resizable: drag the divider left or right.

### Context Header (top bar)

Displays the selected ticker, last-refresh badge ("LIVE" / "Xs ago"), reference spot S₀, risk-free rate r, and the active calibration model.

**QC badge:** green `PASS` means the SVI calibration converged cleanly; yellow `PENDING` means calibration is still running or degraded.

### KPI Strip (4 cards)

| Card | What it measures |
|------|-----------------|
| **Spot Ingestion** | Whether spot is coming from IBKR live feed (`LIVE`) or last disk-cached close (`SYNCHRONIZED`). The `Δ Xms` is the round-trip data latency. |
| **Forward Curve ID** | Which model drives the forward curve. `IBKR parity forward` = live; `SOFR-OIS + Div` = offline/fallback using ECB rate and dividend yield. |
| **Calibration RMSE** | Root-mean-square error of the SVI vol surface fit. Lower is better — typically < 0.002 (0.2 vol pt). |
| **Engine Health** | Percentage of engine capacity currently in use. Cosmetic at demo stage. |

### Vol Surface 3D

An interactive 3-D mesh where:
- **X axis** = strike price
- **Z axis** = maturity (10D, 1M, 3M, 6M, 12M)
- **Y axis** = implied volatility %

The surface is calibrated using the **SVI (Stochastic Volatility Inspired)** parametric model, which fits a smooth curve through market-observed implied vols for each maturity slice.

### 2D Smile — Expiry Slice (30D)

A 2-D line chart of implied vol vs strike for the 30-day maturity. Two lines:
- **Call IV** (blue) — implied vol extracted from call prices
- **Put IV** (green) — implied vol extracted from put prices

**CAL ARB** and **BFLY ARB** badges show whether the calibration is free of calendar arbitrage (same strike, different maturities) and butterfly arbitrage (convexity constraint across strikes). Both should be `CLEAR`.

### Portfolio Greeks Strip (5 boxes)

Aggregate sensitivities for the entire book on the selected underlying.

| Box | Meaning | See [Greeks section](#7-concepts--greeks) |
|-----|---------|------------------------------------------|
| Portfolio Δ | Net delta of all positions combined | §7.1 |
| Portfolio Γ | Net gamma | §7.2 |
| Portfolio V | Net vega in EUR (€1 per 1% vol move) | §7.3 |
| Portfolio Θ | Net theta in EUR per calendar day | §7.4 |
| Portfolio ρ | Net rho in EUR per 100 bps rate move | §7.6 |

### Options Chain

A centered-strike table showing calls on the left, the ATM strike in the middle, and puts mirrored on the right. The ATM row is highlighted in green.

#### Filters

| Filter | Effect |
|--------|--------|
| **TYPE — BOTH / CALLS / PUTS** | Show both sides, only calls, or only puts |
| **RANGE — ALL STRIKES** | Show all strikes in the chain (default: 3 strikes each side of ATM) |
| **RANGE — ATM ±10%** | Keep only strikes within 10% of the ATM strike |
| **RANGE — −30Δ · +30Δ** | Keep only strikes where call delta is between 0.20 and 0.80 (i.e. roughly ±30 delta) |
| **Time range — 1D / 1W / 1M / 3M / 1Y** | Historical lookback window sent to the backend (affects any time-series fields) |
| **LIQ ≥100** | Hide any row where both call volume and put volume are below 100 contracts |
| **EXPIRY dropdown** | Select the option expiry date (next 24 monthly expiries, always 3rd Friday) |

#### Chain Columns — Calls side (left of STRIKE)

| Column | Meaning |
|--------|---------|
| **Mid** | (Bid + Ask) / 2 — the fair-value estimate of the option price |
| **Sprd%** | Bid-ask spread as a % of mid: `(Ask − Bid) / Mid × 100`. Red when > 5% — indicates wide / illiquid market |
| **IV%** | Implied volatility solved from the mid price using the Brent IV solver (Black-Scholes inversion). Shown in %. Blue for OTM, green for ATM. |
| **Δ** | Delta — directional sensitivity (see §7.1) |
| **Γ** | Gamma — convexity (see §7.2) |
| **V** | Vega — vol sensitivity (see §7.3) |
| **Θ** | Theta — daily time decay in EUR, shown in red (see §7.4) |
| **Vol** | Trading volume in contracts today |
| **OI** | Open Interest — total outstanding contracts |
| **QC** | Data quality flag — ✓ green = solved from live IBKR bid/ask; `SYN` = Black-Scholes synthetic (no live quote) |

Puts are mirrored right-to-left: QC OI Vol Θ V Γ Δ IV% Sprd% Mid.

#### Chain Views

**TABLE** (default): full numerical table.  
**HEATMAP**: same data with colour intensity proportional to each Greek value — high absolute values are darker. Useful for quickly spotting the highest-risk strikes.

**CSV Export**: downloads the currently filtered chain as a CSV file.

### Forward Curve & Futures Prices (collapsible)

Expand to see:
- **Futures tiles** — forward price for the next 4 maturities using F = S₀ · e^((r−q)·T) where r = ECB rate and q = dividend yield.
- **Full term structure table** — shows forward vs spot difference in basis points (1 bp = 0.01%).
- **ATM Vol Term Structure chart** — how ATM implied vol changes across maturities. An upward slope (contango) is normal; inversion can signal near-term stress.

### Smile — Raw vs SVI Fitted (collapsible)

Expand to compare:
- **Raw market quotes** (dashed red) — mid prices converted to IV at each strike
- **SVI fitted curve** (solid blue) — the smooth parametric fit

The gap between raw and fitted is the calibration residual. Large gaps indicate either stale quotes or a calibration issue.

Maturity selector (10D, 1M, 3M, 6M, 12M) lets you inspect any slice.

---

## 2. Risk Analysis

**Purpose:** Portfolio-level risk dashboard. Measures how much the portfolio would gain or lose under various scenarios.

### Header Controls

**Portfolio selector** — choose which strategy book to analyse: SX5E Straddle, Dispersion Q3, or Calendar Spread. All panels reload for the selected portfolio.

**System clock** — live UTC timestamp showing the current pricing moment.

### KPI Strip (6 cards)

| Card | Metric | Sub-label |
|------|--------|-----------|
| **Portfolio Delta** | Total directional exposure in EUR | "Eq. Shrs" = equivalent shares: `|Δ| / spot` |
| **Gamma** | Total convexity in EUR | "Δ per 1% spot" = gamma / 100 |
| **$ Gamma** | Dollar gamma = ½ × Γ × S² / 100 (see §7.5) | "Per 1% Spot" = dollar gamma impact of a 1% spot move |
| **Vega** | Vol sensitivity in EUR per 1 vol pt | "1% Vol Shock" = vega / 100 |
| **Theta** | Daily time decay in EUR (negative = portfolio loses value each day) | "Weekly" = theta × 5 |
| **Rho** | Rate sensitivity in EUR per 1% rate move | "10bps Rate" = rho / 10 |

Colour coding: green = positive (portfolio benefits), red = negative (portfolio loses).

### VaR Section

**Value at Risk** measures the worst expected loss over a given horizon at a given confidence level, estimated from 252 trading days of historical returns.

| Toggle | Meaning |
|--------|---------|
| **1D 95%** | There is a 95% probability the 1-day loss will not exceed this amount |
| **1D 99%** | There is a 99% probability the 1-day loss will not exceed this amount |
| **7D 99%** | 7-day horizon at 99% confidence (often used for regulatory reporting) |

The highlighted box is the currently selected horizon. Values are always negative (they represent losses).

See [§8.1](#81-value-at-risk-var) for the full calculation method.

### Margin Utilisation (UAM)

**UAM (Utilisé / Available Margin)** shows what percentage of the account's available margin is currently consumed by open positions.

| Colour | Level | Meaning |
|--------|-------|---------|
| Green | 0–80% | Normal — plenty of buffer |
| Yellow | 80–90% | Caution — approaching limits |
| Red | 90–100% | Warning — close to margin call |

**Worst case P&L** is the single largest loss cell in the UAM shock grid (Spot × Vol scenario matrix).

### Custom Shock — Local Greeks P&L Approximation

An interactive panel to estimate P&L for arbitrary market moves using the **second-order Taylor approximation**:

```
ΔPnL ≈ Δ · dS + ½ · Γ · dS² + ν · dVol + Θ · dT
```

| Slider | Range | What it controls |
|--------|-------|-----------------|
| **Spot Move** | −15% to +15% | Percentage change in the underlying spot price |
| **Vol Shift** | −20 to +20 vol pts | Parallel shift in all implied vols |
| **Days Roll** | 0 to 30 days | Time passage (theta decay) |

The right panel breaks down the estimated P&L by Greek component, with a waterfall bar. The centre line is zero; blue bars extend right (gain), red bars extend left (loss).

### PnL Attribution Grid

Horizontal bar chart showing yesterday's actual P&L decomposed into components:

| Row | How it's calculated |
|-----|---------------------|
| **Delta** | How much of today's move came from directional exposure |
| **Gamma** | P&L from convexity (second-order spot move) |
| **Vega** | P&L from implied vol changes |
| **Theta** | Time decay earned or lost |
| **Rho** | P&L from interest rate moves |
| **TOTAL** | Sum of all components |

### Correlation Matrix

Pearson correlation of daily log-returns over a 252-day rolling window. Values range from −1 to +1:
- **1.0** = perfect positive correlation (move together)
- **0.0** = no correlation
- **−1.0** = perfect inverse correlation

Cells are colour-coded by intensity (darker blue = stronger correlation). The diagonal is always 1.00 (each asset is perfectly correlated with itself).

### Shock Simulator: Spot × Vol

A **3×3 grid** showing portfolio P&L under nine combined scenarios:
- Rows: Spot −5%, Unchanged, +5%
- Columns: Vol −30%, Unchanged, +30%

Green cells = profit, red cells = loss. The baseline (Spot Unchanged × ATM Vol) should be near zero. The most negative cell is the worst-case loss referenced by UAM.

### Risk Aggregates Table

Breaks down risk by sub-portfolio group (by underlying and by expiry).

| Column | Meaning |
|--------|---------|
| **Group** | Sub-portfolio label (underlying ticker or expiry date) |
| **Net Δ** | Sum of all position deltas in this group |
| **Net Vega** | Sum of all position vegas |
| **Net Θ** | Sum of all position thetas (daily EUR P&L from time decay) |
| **$Δ** | Dollar delta = Net Δ × reference spot (EUR notional directional exposure) |
| **$Vega** | Dollar vega = Net Vega × reference spot / 100 |
| **Mkt Val** | Total mark-to-market value of all positions in this group |
| **# Pos** | Number of individual option legs in this group |

### Positions Table

| Column | Meaning |
|--------|---------|
| **Contract** | Instrument name: `UNDERLYING YYYYMMDD STRIKE C/P` |
| **Qty** | Number of contracts (positive = long, negative = short) |
| **Mkt Value** | Current mark-to-market value in EUR |
| **Avg Cost** | Average price paid per contract across all fills |
| **Unreal. P&L** | Unrealised P&L = (Mkt Value − Avg Cost × Qty) |

### Liquidity Metrics — Widest Spreads

Shows the options in the book with the widest bid-ask spreads — a proxy for liquidity risk.

| Column | Meaning |
|--------|---------|
| **Ticker / Expiry / Strike** | Contract identifier |
| **Bid-Ask Spread** | `(Ask − Bid) / Mid × 100%` — wide spread means it is expensive to exit this position. Red + `WIDE` label when > 5%. |
| **Volume** | Contracts traded today. `LOW` label when < 100. |

### Pipeline Audit & Logs

A live audit trail of data quality control (QC) events. Each row represents one check the system ran on a data point.

| Column | Meaning |
|--------|---------|
| **Timestamp** | When the QC check ran |
| **Ticker** | Which instrument was checked |
| **Exception Type** | What kind of check: IV solver convergence, stale quote, wide spread, etc. |
| **Tenor** | Which maturity slice (e.g. "3M", "6M") |
| **Status** | OK (green), WARN (yellow), FAIL (red) |
| **Reason Code** | Human-readable explanation of why the check triggered |

**Filter Exceptions** button (top right): not yet wired — would filter to WARN/FAIL rows only.

---

## 3. Strategy Execution

**Purpose:** Manage live strategy positions, place new orders to IBKR, and act on hedge engine alerts.

### Header Bar

- **Latency** — round-trip API response time in ms. Green when live.
- **Strategy count** — how many strategy objects are loaded.
- **NEW ORDER** button — expands the order ticket panel.

### Order Ticket (collapsible)

Appears when you click NEW ORDER. Routes directly to IBKR on submit.

| Field | Options / Format | Notes |
|-------|-----------------|-------|
| **Direction** | BUY (green) / SELL (red) | Side of the trade |
| **Underlying** | SX5E, ASML, MC.PA, SAP, TTE, SIE, OR.PA | Which stock or index |
| **Instrument** | Call, Put, Future, Stock | Instrument type |
| **Strike** | Number (e.g. 4200) | Leave blank for futures/stocks |
| **Expiry** | YYYY-MM-DD | Option expiry date |
| **Quantity** | Integer | Number of contracts |
| **Order Type** | MARKET / LIMIT | LIMIT requires a price; MARKET fills immediately at best available |
| **Limit Price** | Number (e.g. 82.40) | Only active when Order Type = LIMIT |

Submit sends `POST /api/strategy/order` with destination IBKR.

### Portfolio Greeks Banner (4 KPIs)

| KPI | Meaning |
|-----|---------|
| **NET Δ (Portfolio)** | Sum of all strategy deltas — net directional exposure |
| **NET VEGA (Portfolio)** | Sum of all strategy vegas |
| **STRATEGIES ACTIVE** | Count of strategies with status = OPEN |
| **TOTAL MARGIN USED** | Sum of all allocated margin percentages — yellow when > 50% |

### Strategy Filter Bar

| Filter group | Options | Effect |
|-------------|---------|--------|
| **TYPE** | ALL, STRADDLE, DISPERSION, CALENDAR, BUTTERFLY | Show only strategies of the selected type |
| **STATUS** | ALL, OPEN, CLOSED, ROLLED, PENDING | Show only strategies in the selected lifecycle state |
| **MATURITY** | All Maturities, < 3M, 3–6M, 6–12M, > 12M | Filter by time to expiry |

The counter `X/Y shown` updates as filters change.

### Strategy Cards

Each active strategy is shown as a card.

**Card header:**
- Strategy name
- `ROLL NOW` or `Roll in Xd` badge when expiry < 90 days away (red when < 30 days)
- Strategy type badge (STRADDLE, DISPERSION, etc.)
- Leg badges (e.g. "Call 4200 DEC26 / Put 4200 DEC26")
- `LIVE_EXEC` pill (blinking green) = this strategy can place real orders

**Card fields:**

| Field | Meaning |
|-------|---------|
| **Strategy Label** | Internal strategy code (e.g. `ALPHA_CORE_V1`) |
| **Target Strike (K)** | The strike around which the strategy is centred. For dispersion baskets, this is the index anchor. |
| **Expiry** | Maturity label and exact date |
| **Open Interest** | Total open contracts across all legs |
| **Allocated Margin** | EUR margin reserved for this strategy and % of total account margin |
| **PnL (Intraday)** | Today's profit/loss in EUR — green positive, red negative |
| **Total Δ** | Net delta of all legs combined |
| **Total Vega** | Net vega of all legs combined |

**Dispersion basket sub-table**: for DISPERSION strategies, shows each constituent ticker and its target strike.

**Action buttons:**

| Button | What it does |
|--------|-------------|
| **[1-Click Roll]** | Calls `POST /api/strategy/roll` to roll all legs to the next available expiry |
| **[Auto-Hedge Δ]** | Calls `POST /api/strategy/hedge` to flatten delta to zero using futures |
| **[Close]** | Calls `POST /api/strategy/liquidate` to close all legs at market |

### L2 Order Book Feed

Real-time Level-2 order book snapshot, refreshing every 2 seconds.

| Column | Meaning |
|--------|---------|
| **Time** | Timestamp of this quote |
| **Bid Size** | Number of contracts available to buy at Bid price |
| **Bid** | Highest price a buyer will pay (green) |
| **Ask** | Lowest price a seller will accept (red) |
| **Ask Size** | Number of contracts available to sell at Ask price |
| **Spread (%)** | `(Ask − Bid) / Mid × 100%` — rows with spread > 5% are highlighted red with ⚠ |

### Hedge Suggest Engine

An algorithmic engine that monitors portfolio Greeks and emits alerts when action is needed.

Two alert types:
- **Delta Imbalance** — net delta has drifted beyond the acceptable band. Shows recommended action (e.g. "Sell 120 SX5E Futs") with an **Execute** button that calls `POST /api/strategy/execute-hedge`.
- **Vega Roll Opportunity** — a maturity has become rich/cheap relative to the model. Shows a **Review Matrix** button (navigates to Risk tab).

Each suggestion shows its **age** (how long ago it was generated) and **severity**.

---

## 4. Backtesting

**Purpose:** Simulate how a strategy would have performed historically, and run a Monte Carlo simulation to estimate future return distribution.

### Filter Ribbon

| Control | Options | Effect |
|---------|---------|--------|
| **STRAT** | VOL_CARRY_01, SX5E_STRADDLE, DISPERSION_Q3 | Which strategy logic to backtest |
| **UNDERLYING** | SX5E, SPX, NDX, DAX, FTSE100 | Which index the strategy trades on |
| **TIME — presets** | 1Y, 3Y, 5Y, MAX | Quick date range buttons (MAX goes back to 2005-01-01) |
| **Custom dates** | Date picker start → end | Override the preset with a specific window |
| **SHOCK PRESET** | 2008 Crash, 2020 Liquidity Shock, BREXIT, COVID Vol Spike | Zoom into a known stress period (overrides the date range) |

Clicking a shock preset a second time clears it and restores the last time range.

### Cumulative PnL vs Benchmark chart

A composed chart with three overlapping series:

| Series | Colour | Meaning |
|--------|--------|---------|
| **Strategy** | Blue | Cumulative P&L of the selected strategy in % |
| **Benchmark (underlying)** | Grey | Buy-and-hold return of the underlying index for comparison |
| **Drawdown** | Red shaded area | Running peak-to-trough decline (always ≤ 0) |

X axis = date (year labels), Y axis = cumulative return %. The drawdown area fills downward from zero to show the magnitude of each losing period.

### Monte Carlo Return Distribution

500 independent GBM (Geometric Brownian Motion) paths simulated forward from today. Each path produces a terminal 1-year return. The chart is a histogram of those 500 returns.

- **Blue bars** = returns above the VaR threshold (profitable or tolerable losses)
- **Red bars** = returns below the VaR threshold
- **Red dashed vertical line** = 95% VaR level (5% of paths are to the left of this line)
- **95% Expected VaR label** = the exact return at the 5th percentile

### Greeks Over Time (4 mini charts)

Four small line charts showing how each Greek evolved over the backtest period:
- **DELTA / Time** — how directional exposure changed
- **GAMMA / Time** — how convexity changed (spikes near expiry)
- **VEGA / Time** — vol sensitivity evolution
- **THETA / Time** — daily time decay evolution (typically negative; magnitude grows closer to expiry)

### Stat Grid (6 cards)

| Stat | Meaning |
|------|---------|
| **Cumulative PnL (Ann.)** | Total return annualised as a %. Badge shows outperformance vs benchmark (`+X.X v BM`). |
| **Sharpe Ratio** | Risk-adjusted return = (mean annual return − risk-free rate) / annualised vol. `rf=X%` shows the risk-free rate used. Higher is better; > 1 is generally considered good. |
| **Win Rate %** | Percentage of individual trades that closed at a profit. The mini bar fills proportionally. |
| **Max Drawdown** | Largest peak-to-trough loss during the backtest period. Always negative. |
| **Prob. of Profit** | From the Monte Carlo: what fraction of the 500 paths produced a positive terminal return. |
| **Avg Trade PnL** | Average P&L per closed trade in EUR. |

---

## 5. Shock Simulator

**Purpose:** Apply manual stress scenarios to the portfolio and immediately see the repriced P&L, without running a full backtest.

### Methodology Strip

| Control | Meaning |
|---------|---------|
| **PORTFOLIO** | Which strategy book to reprice |
| **ASSET CLASS** | Toggle which instrument types are included: Options, Futures, Stocks |
| **METHOD** | Which repricing model to use (can select multiple — the first active one is primary) |
| **VOL SURFACE Δ** | Toggle a chart showing ATM vol curve before and after the shock |
| **Reset** | Return all sliders to zero |

**Repricing methods:**

| Method | How it works |
|--------|-------------|
| **Parallel Grid Shift** | Shifts every point on the vol surface by the same vol shock amount. Fast and intuitive. |
| **Historical Copula Resampling** | Uses historical joint distributions of spot and vol moves (copula) to resample realistic scenarios. More accurate for tail events. |
| **VIX-Indexed Skew Stressing** | Amplifies the vol skew based on the current VIX level — high VIX periods steepen the skew more. |

### Manual Shock Controls

Four independent sliders:

| Slider | Range | Effect on pricing |
|--------|-------|------------------|
| **Underlying Spot Price Shock** | −20% to +20% | Shifts the spot price used in all BS calculations |
| **Global Volatility Surface Shift** | −30% to +30% | Parallel shift of all implied vols |
| **Interest Rate Parallel Shift** | −200 to +200 bps | Shifts the risk-free rate (affects rho) |
| **Correlation Shock (ρ)** | −1.0 to +1.0 | Changes pairwise correlations in dispersion/basket repricing |

Values update the scenario matrix automatically (debounced 300 ms).

Footer KPIs:
- **Agg. Shift** — combined magnitude of spot + vol shocks as one number
- **Methods** — how many repricing methods are active
- **Rate** — the current rate shock in bps
- **ρ Shock** — the correlation shock (orange when non-zero)

### Scenario Evaluation Matrix (3×3)

The core output: 9 scenarios combining 3 spot shocks and 3 vol shocks.

| | −30 ΔVol | ATM Baseline | +30 ΔVol |
|--|---------|-------------|---------|
| **Spot −5%** | cell | cell | cell |
| **Spot Unchanged** | cell | **zero** | cell |
| **Spot +5%** | cell | cell | cell |

Each cell shows:
- **P&L in EUR** (e.g. `+€1.23M`) — green positive, red negative
- **NAV bps** — P&L expressed as basis points of portfolio NAV (`P&L / NAV × 10,000`)

Colour intensity scales with the magnitude of the P&L — darker = larger absolute move.

The matrix offsets are **additive** to the manual sliders: if you set Spot Shock to +3%, the Spot −5% row represents Spot −5% + 3% = Spot −2%.

### ATM Vol Term Structure — Before/After (toggle)

Click **VOL SURFACE Δ** to reveal a line chart comparing:
- **Baseline vol curve** (grey) — current term structure
- **Shocked vol curve** (blue) — after applying the vol slider

Useful for seeing how the vol shock propagates across maturities.

### Hedging Suggestions

Appears automatically when spot shock > 3% or vol shock > 10%. Each card shows:

| Field | Meaning |
|-------|---------|
| **Urgency** | HIGH (red) = act now; MEDIUM (yellow) = monitor; LOW (grey) = informational |
| **Action** | Specific recommended trade (e.g. "Buy SX5E 4000P Dec26") |
| **Reason** | Why this hedge is suggested (e.g. "Delta exposure +42 under spot shock") |
| **Δ −X** | How much delta this hedge would remove |

### Liquidity Impact Table

Appears when spot or vol shock is large enough to stress liquidity. Shows how bid-ask spreads and trading volume change under the scenario.

| Column | Meaning |
|--------|---------|
| **Contract** | The option contract affected |
| **Pre-Shock Spread** | Normal bid-ask spread as % of mid |
| **Post-Shock Spread** | Estimated spread under stress. Red when wider. `(+Xpp)` = spread widened by this many percentage points. |
| **Vol Impact** | Estimated % change in available trading volume. Negative = less liquidity. |

---

## 6. Orders

**Purpose:** Order Management System (OMS) blotter — a complete record of all orders sent to IBKR, their fill status, and the ability to cancel live orders.

### KPI Strip

| Tile | Meaning |
|------|---------|
| **TOTAL ORDERS** | All orders in the blotter |
| **STAGED** | Orders created locally but not yet sent to IBKR |
| **WORKING** | Orders active at IBKR: `submitted` (waiting for fill) or `partial` (partially filled) |
| **FILLED** | Fully filled orders |
| **REJECTED** | Failed or user-cancelled orders |

### Filter Bar

Buttons filter the blotter to a subset:

| Filter | Shows |
|--------|-------|
| ALL | Every order |
| STAGED | Only staged orders |
| WORKING | Submitted + partial |
| FILLED | Fully filled only |
| REJECTED | Rejected + cancelled |

### Order Blotter Table

| Column | Meaning |
|--------|---------|
| **Status** | Current lifecycle state (see colour badges below) |
| **Order ID** | Last 8 characters of the IBKR order ID |
| **Side** | BUY (green) / SELL (red) |
| **Qty** | Total order quantity in contracts |
| **Instrument** | `UNDERLYING EXPIRY STRIKE C/P` (e.g. `SX5E 20261218 4000C`) |
| **Type** | `LMT @82.40` = limit order with price; `MKT` = market order |
| **Filled** | `filled_qty / total_qty` — yellow when partially filled |
| **Fill Px** | Average fill price. `—` if no fills yet. |
| **Reason** | Rejection reason or cancellation note |
| **Actions** | Cancel button — only shown for `staged` and `submitted` orders |

**Status badge colours:**

| Status | Badge | Meaning |
|--------|-------|---------|
| `staged` | Grey | Order created locally, waiting to be sent |
| `submitted` | Blue border | Sent to IBKR, waiting for fill |
| `partial` | Yellow | Part-filled — remainder still working |
| `filled` | Green border | Fully executed |
| `cancelled` | Dark grey | Cancelled before fill |
| `rejected` | Red border | IBKR rejected the order (see Reason column) |

The blotter auto-refreshes every 10 seconds. The header shows "LIVE" when data is < 3 seconds old.

---

## 7. Concepts — Greeks

Greeks measure the sensitivity of an option's price to one input, holding all others constant. They are derived from the **Black-Scholes formula**:

```
Call price C = S·N(d₁) − K·e^(−rT)·N(d₂)
Put price  P = K·e^(−rT)·N(−d₂) − S·N(−d₁)

where:
  d₁ = [ln(S/K) + (r − q + σ²/2)·T] / (σ·√T)
  d₂ = d₁ − σ·√T

  S = spot price
  K = strike price
  T = time to expiry in years
  r = risk-free rate
  q = dividend yield
  σ = implied volatility
  N(·) = cumulative standard normal distribution
```

### 7.1 Delta (Δ)

**Definition:** How much the option price changes for a €1 move in the underlying.

```
Call delta = N(d₁)       →  ranges from 0 to +1
Put delta  = N(d₁) − 1   →  ranges from −1 to 0
```

**Intuition:**
- Delta = 0.50 means the option behaves like holding 0.50 shares
- ATM options have delta ≈ ±0.50
- Deep ITM options have delta ≈ ±1.0 (moves like the stock)
- Far OTM options have delta ≈ 0 (very little sensitivity)

**In the portfolio:** Portfolio Delta = sum of all position deltas. A delta of +€4.5M means the portfolio gains €4.5 for every €1 the underlying rises.

**Equivalent shares:** `|Δ| / spot` — the number of underlying shares that would have the same directional risk.

### 7.2 Gamma (Γ)

**Definition:** How much delta changes for a €1 move in the underlying. The second derivative of option price with respect to spot.

```
Gamma = N'(d₁) / (S·σ·√T)

where N'(x) = (1/√2π)·e^(−x²/2)  (standard normal PDF)
```

**Intuition:**
- Gamma is always positive for long options (calls or puts)
- High gamma = delta changes rapidly with spot (options are sensitive near expiry or ATM)
- Gamma peaks at ATM and shrinks toward zero for deep ITM/OTM
- A **long straddle** has positive gamma — you profit from large moves in either direction

**In the portfolio:** Negative portfolio gamma means the book is short optionality (sold more options than bought) — you lose money from large spot moves.

### 7.3 Vega (ν or V)

**Definition:** How much the option price changes for a 1% point increase in implied volatility.

```
Vega = S·N'(d₁)·√T
```

**Intuition:**
- Vega is always positive for long options — you profit when vol rises
- Longer-dated options have higher vega (more time for vol to affect price)
- ATM options have the highest vega for a given maturity
- Selling options = short vega = you profit from vol declining

**In the portfolio:** Portfolio Vega of +€850K means the book gains €850K for every 1% point rise in implied vol across all positions.

### 7.4 Theta (Θ)

**Definition:** How much the option price loses per calendar day with everything else held constant. Also called "time decay."

```
Theta (call) = −[S·N'(d₁)·σ / (2√T)] − r·K·e^(−rT)·N(d₂)
```

**Intuition:**
- Theta is almost always negative for long options — options lose value each day
- Theta accelerates as expiry approaches (especially within 30 days)
- Short options have positive theta — you collect decay each day
- A **short straddle** has high positive theta but high gamma risk (unlimited loss from large moves)

**In the portfolio:** Theta of −€12,500 per day means the book loses €12,500 in value with each day that passes, assuming spot and vol are unchanged. The weekly figure (Θ × 5) gives a more intuitive scale.

### 7.5 Dollar Gamma ($Γ)

**Definition:** The actual EUR P&L impact from a 1% spot move, accounting for position size.

```
$Γ = ½ · Γ · S² · (1/100)²
```

This makes gamma comparable across different underlying prices (e.g. SX5E at 4200 vs ASML at 900).

**Intuition:** If $Γ = −€385,200, the portfolio loses ~€385,200 for every 1% move in either direction (from gamma alone, assuming delta is hedged).

### 7.6 Rho (ρ)

**Definition:** How much the option price changes for a 1% point change in the risk-free interest rate.

```
Rho (call) = K·T·e^(−rT)·N(d₂)
Rho (put)  = −K·T·e^(−rT)·N(−d₂)
```

**Intuition:**
- Calls have positive rho (higher rates increase call value — the cost of carry effect)
- Puts have negative rho
- Rho is small for short-dated options; more significant for LEAPS (> 1 year)
- **ECB rate changes** most directly affect European equity option rho

**In the portfolio:** Rho of +€45,100 per 1% means a 100 bps rate hike adds €45,100 to portfolio value. The "10bps Rate" sub-label shows the impact of a more realistic 10 bps move.

---

## 8. Concepts — Risk Metrics

### 8.1 Value at Risk (VaR)

**Method used:** Historical simulation (non-parametric).

**Steps:**
1. Collect 252 daily log-returns: `r_t = ln(S_t / S_{t-1})`
2. Apply each historical return to today's portfolio value
3. Sort the 252 simulated P&Ls from worst to best
4. **95% VaR** = the 13th worst (bottom 5% of 252 ≈ 12.6 → 13th)
5. **99% VaR** = the 3rd worst (bottom 1% of 252 ≈ 2.5 → 3rd)
6. **7D 99% VaR** = 1D 99% × √7 (square-root-of-time scaling)

**Interpretation:** "1D 95% VaR of −€198,900" means: based on the last year of market moves, there is a 5% probability of losing more than €198,900 in a single day.

**Limitations:** VaR does not capture losses beyond the confidence threshold (tail risk). Use the shock simulator for stress scenarios beyond historical experience.

### 8.2 Sharpe Ratio

```
Sharpe = (E[R] − rf) / σ(R)
```

- **E[R]** = mean annualised strategy return
- **rf** = risk-free rate (shown in the stat card, e.g. `rf=4.5%`)
- **σ(R)** = annualised standard deviation of returns

**Interpretation:** A Sharpe of 1.85 means the strategy earned 1.85× its own volatility above the risk-free rate. General guidance:
- < 0: strategy underperforms risk-free rate
- 0–1: modest risk-adjusted return
- 1–2: good
- > 2: excellent (rare in practice; check for overfitting)

### 8.3 Maximum Drawdown

The largest peak-to-trough decline in the strategy's cumulative P&L during the backtest period.

```
Max Drawdown = min over all t of (P&L_t − max_{s ≤ t} P&L_s)
```

A drawdown of −8.2% means the strategy fell 8.2% from its highest point before recovering.

### 8.4 Bid-Ask Spread

```
Spread % = (Ask − Bid) / ((Ask + Bid) / 2) × 100
```

The spread is the implicit transaction cost: buying at the ask and immediately selling at the bid costs you `Spread%` of your position. Wide spreads indicate:
- Thin markets (low volume, low open interest)
- High uncertainty or high event risk
- Illiquid deep OTM or long-dated options

**Red flag threshold:** > 5% in the chain and OMS blotter.

### 8.5 UAM (Margin Utilisation)

IBKR calculates required margin for each option position based on the SPAN methodology (Standard Portfolio Analysis of Risk), which considers worst-case losses across a set of price/vol scenarios. UAM tracks what percentage of the approved margin limit the open positions consume.

---

## 9. Concepts — Options Pricing

### 9.1 Implied Volatility (IV)

Implied volatility is the value of σ that, when plugged into the Black-Scholes formula, produces the observed market price. It is solved numerically using the **Brent root-finding algorithm**:

```
Find σ such that BS_Price(S, K, T, r, q, σ) = observed market mid
```

In the options chain, QC = ✓ (green) means IV was solved from a live IBKR bid/ask. QC = `SYN` means the system fell back to a model-generated price because no live quote was available.

### 9.2 Vol Smile and Skew

In a perfect Black-Scholes world, IV would be flat across all strikes. In reality, IV varies:
- **Skew**: lower strikes (OTM puts) tend to have higher IV than higher strikes (OTM calls) in equity markets — this is the "put skew" or "negative skew"
- **Smile**: IV is higher on both wings (deep OTM calls and puts) than at ATM — common in FX markets

The system models skew as a linear + quadratic function of log-moneyness `k = ln(K/S)`:
```
call_iv = ATM_vol + 0.025 × (−k)
put_iv  = ATM_vol + 0.040 × (−k)
```

### 9.3 SVI Calibration

SVI (Stochastic Volatility Inspired, Gatheral 2004) is a parametric model for the vol smile. For each maturity slice, it fits:

```
w(k) = a + b·(ρ·(k − m) + √((k − m)² + σ²))
```

where `w(k) = σ²_impl(k) × T` is total implied variance, and `a, b, ρ, m, σ` are the five SVI parameters fitted by least squares.

**RMSE** (Root Mean Square Error) measures how well the SVI curve fits the market-observed IV points. A good fit has RMSE < 0.002 (0.2 vol pts). Larger RMSE means the market has unusual shapes the model cannot fully capture.

### 9.4 Forward Price

```
F = S₀ · e^((r − q) · T)
```

- **S₀** = current spot
- **r** = risk-free rate (ECB deposit rate proxy ≈ 4.5%)
- **q** = continuous dividend yield (SX5E ≈ 3.2%)
- **T** = time to delivery in years

For SX5E constituents, `r > q` so the forward is above spot (carry positive). The difference in basis points from spot is shown in the forward curve table.

### 9.5 ATM Volatility

ATM vol is estimated from 21-day (1 month) rolling realised volatility:

```
ATM_vol ≈ σ_realised = std(daily log-returns over 21 days) × √252
```

When yfinance returns insufficient data, the system falls back to hardcoded reference values for each ticker (e.g. SX5E ≈ 18.5%, ADYEN ≈ 41.6%).

### 9.6 Open Interest vs Volume

| Metric | Definition | What it tells you |
|--------|-----------|-------------------|
| **Volume** | Contracts traded today | Short-term liquidity; high volume = active market |
| **Open Interest (OI)** | Total outstanding open contracts (not yet closed or delivered) | Longer-term engagement; rising OI = new money entering the position |

OI changes only when new contracts are opened or existing ones are closed. Volume can spike without changing OI if contracts are simply passed between traders.
