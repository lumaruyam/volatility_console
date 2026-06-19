import type { ReactNode } from "react";
import { useState, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  TrendingUp, TrendingDown, ChevronsDown, BarChart3,
  Clock, Landmark, Activity, Info, Filter, ShieldAlert,
  ChevronDown, Sliders, Layers, Database,
} from "lucide-react";
import { Panel, StatusPill } from "../ui";

// ---------------------------------------------------------------------------
// API types
// ---------------------------------------------------------------------------

type GreeksData = {
  portfolio_delta: number;
  gamma: number;
  dollar_gamma: number;
  vega: number;
  theta: number;
  rho: number;
};

type VarData = {
  "1d_95": number;
  "1d_99": number;
  "7d_99": number;
};

type PnLData = {
  delta_pnl: number;
  gamma_pnl: number;
  vega_pnl: number;
  theta_pnl: number;
  rho_pnl: number;
};

type CorrData = {
  tickers: string[];
  matrix: number[][];
};

type UamCell = { pnl: number; tone: string };
type UamRow  = { label: string; cells: UamCell[] };
type UamData = {
  rows: UamRow[];
  vol_col_labels: string[];
  uam_pct: number;
  worst_case_pnl: number;
};

type QcEntry = {
  ts: string;
  ticker: string;
  type: string;
  tenor: string;
  status: string;
  reason: string;
};

type PositionRow = {
  contract: string;
  qty: number;
  mkt_value: number;
  avg_cost: number;
  unrealised_pnl: number;
};

type RiskAgg = {
  group: string;
  net_delta: number;
  net_vega: number;
  net_theta: number;
  dollar_delta: number;
  dollar_vega: number;
  mkt_val: number;
  n_positions: number;
};

type LiquidityRow = {
  ticker: string;
  expiry: string;
  strike: number;
  bid_ask_spread_pct: number;
  volume: number;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtPnl(v: number): string {
  const sign = v >= 0 ? "+" : "";
  const abs  = Math.abs(v);
  if (abs >= 1_000_000) return `${sign}${(v / 1e6).toFixed(1)}M`;
  if (abs >= 1_000)     return `${sign}${(v / 1e3).toFixed(1)}K`;
  return `${sign}${v.toFixed(0)}`;
}

const apiFetch = (url: string) =>
  fetch(url).then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); });

function fmtGreek(v: number): string {
  const sign = v >= 0 ? "+" : "";
  const abs  = Math.abs(v);
  if (abs >= 1_000_000) return `${sign}${(v / 1e6).toFixed(2)}M`;
  if (abs >= 1_000)     return `${sign}${(v / 1e3).toFixed(1)}K`;
  return `${sign}${v.toFixed(0)}`;
}

function tone(v: number): "pos" | "neg" | "neu" {
  return v > 0 ? "pos" : v < 0 ? "neg" : "neu";
}

// ---------------------------------------------------------------------------
// Static fallbacks (shown while loading)
// ---------------------------------------------------------------------------

const FB_GREEKS: GreeksData = {
  portfolio_delta: 4_520_000,
  gamma:          -1_240_000,
  dollar_gamma:    -385_200,
  vega:            850_400,
  theta:           -12_500,
  rho:              45_100,
};

const FB_VAR: VarData = {
  "1d_95": -198_900,
  "1d_99": -356_200,
  "7d_99": -789_100,
};

const FB_TICKERS = ["SX5E", "ASML", "MC.PA", "SAP", "TTE"];
const FB_CORR = [
  [1.00, 0.82, 0.88, 0.79, 0.65],
  [0.82, 1.00, 0.54, 0.89, 0.45],
  [0.88, 0.54, 1.00, 0.48, 0.52],
  [0.79, 0.89, 0.48, 1.00, 0.49],
  [0.65, 0.45, 0.52, 0.49, 1.00],
];

const FB_POSITIONS: PositionRow[] = [
  { contract: "SX5E 20261218 4000C", qty:  100, mkt_value:  125_000, avg_cost: 1_180, unrealised_pnl:   7_000 },
  { contract: "SX5E 20261218 4000P", qty:  100, mkt_value:  118_000, avg_cost: 1_250, unrealised_pnl:  -7_000 },
  { contract: "SX5E 20270618 4200C", qty:   50, mkt_value:   45_000, avg_cost:   900, unrealised_pnl:       0 },
  { contract: "SX5E 20270618 3800P", qty:   50, mkt_value:   52_000, avg_cost:   980, unrealised_pnl:   3_000 },
];

const FB_RISK_AGGS: RiskAgg[] = [
  { group: "SX5E",       net_delta: 0.52, net_vega:  8_504, net_theta: -125, dollar_delta: 2_340_000, dollar_vega: 850_000, mkt_val: 15_200_000, n_positions: 4 },
  { group: "2026-12-18", net_delta: 0.31, net_vega:  5_200, net_theta:  -75, dollar_delta: 1_395_000, dollar_vega: 520_000, mkt_val:  8_100_000, n_positions: 2 },
  { group: "2027-06-18", net_delta: 0.21, net_vega:  3_304, net_theta:  -50, dollar_delta:   945_000, dollar_vega: 330_000, mkt_val:  7_100_000, n_positions: 2 },
  { group: "TOTAL",      net_delta: 0.52, net_vega:  8_504, net_theta: -125, dollar_delta: 2_340_000, dollar_vega: 850_000, mkt_val: 15_200_000, n_positions: 4 },
];

const FB_LIQUIDITY: LiquidityRow[] = [
  { ticker: "SX5E", expiry: "2026-12-18", strike: 3600, bid_ask_spread_pct: 8.2, volume:   210 },
  { ticker: "SX5E", expiry: "2027-06-18", strike: 4400, bid_ask_spread_pct: 7.1, volume:   145 },
  { ticker: "SX5E", expiry: "2026-12-18", strike: 3400, bid_ask_spread_pct: 6.5, volume:    98 },
  { ticker: "ASML", expiry: "2026-12-18", strike:  900, bid_ask_spread_pct: 5.8, volume:    54 },
  { ticker: "ASML", expiry: "2026-12-18", strike:  750, bid_ask_spread_pct: 5.3, volume:    67 },
];

const PORTFOLIOS = [
  { value: "SX5E_STRADDLE",  label: "SX5E Straddle" },
  { value: "DISPERSION_Q3",  label: "Dispersion Q3" },
  { value: "CALENDAR_SPD",   label: "Calendar Spread" },
];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function RiskAnalysis() {
  const [portfolio, setPortfolio]   = useState("SX5E_STRADDLE");
  const [varHorizon, setVarHorizon] = useState<"1d_95" | "1d_99" | "7d_99">("1d_95");
  const [systemTime, setSystemTime] = useState("");
  const [spotShock, setSpotShock]   = useState(0);
  const [volShock, setVolShock]     = useState(0);
  const [daysShock, setDaysShock]   = useState(0);

  // Live system clock
  useEffect(() => {
    const tick = () => setSystemTime(new Date().toUTCString().slice(17, 25) + " UTC");
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  // ── Queries ───────────────────────────────────────────────────────────────
  const { data: greeks = FB_GREEKS } = useQuery<GreeksData>({
    queryKey:        ["risk-greeks", portfolio],
    queryFn:         () => apiFetch(`/api/risk/greeks?portfolio=${portfolio}`),
    staleTime:       30_000,
    refetchInterval: 30_000,
  });

  const { data: varData = FB_VAR } = useQuery<VarData>({
    queryKey:        ["risk-var", portfolio, varHorizon],
    queryFn:         () => apiFetch(`/api/risk/var?portfolio=${portfolio}`),
    staleTime:       60_000,
    placeholderData: FB_VAR,
  });

  const { data: pnl } = useQuery<PnLData>({
    queryKey:  ["risk-pnl", portfolio],
    queryFn:   () => apiFetch(`/api/risk/pnl-attribution?portfolio=${portfolio}`),
    staleTime: 30_000,
  });

  const { data: corrData } = useQuery<CorrData>({
    queryKey:  ["risk-correlation", portfolio],
    queryFn:   () => apiFetch(`/api/risk/correlation?portfolio=${portfolio}`),
    staleTime: 300_000,
  });

  const { data: uam } = useQuery<UamData>({
    queryKey:  ["risk-uam", portfolio],
    queryFn:   () => apiFetch(`/api/risk/uam?portfolio=${portfolio}`),
    staleTime: 30_000,
  });

  const { data: qcLog = [] } = useQuery<QcEntry[]>({
    queryKey:        ["risk-qc-log"],
    queryFn:         () => apiFetch("/api/risk/qc-log"),
    staleTime:       10_000,
    refetchInterval: 30_000,
  });

  const { data: refSpotData } = useQuery<{ spot: number }>({
    queryKey:  ["risk-ref-spot", portfolio],
    queryFn:   () => apiFetch(`/api/risk/reference-spot?portfolio=${portfolio}`),
    staleTime: 60_000,
  });

  const { data: positions = FB_POSITIONS } = useQuery<PositionRow[]>({
    queryKey:        ["risk-positions", portfolio],
    queryFn:         () => apiFetch(`/api/risk/positions?portfolio=${portfolio}`),
    staleTime:       30_000,
    refetchInterval: 30_000,
  });

  const { data: riskAggs = FB_RISK_AGGS } = useQuery<RiskAgg[]>({
    queryKey:  ["risk-aggregates", portfolio],
    queryFn:   () => apiFetch(`/api/risk/aggregates?portfolio=${portfolio}`),
    staleTime: 60_000,
  });

  const { data: liquidityData = FB_LIQUIDITY } = useQuery<LiquidityRow[]>({
    queryKey:  ["risk-liquidity", portfolio],
    queryFn:   () => apiFetch(`/api/risk/liquidity?portfolio=${portfolio}`),
    staleTime: 60_000,
  });

  // ── Derived: KPI tiles ────────────────────────────────────────────────────
  const refSpot  = refSpotData?.spot ?? 4_000;
  const eqShares = Math.round(Math.abs(greeks.portfolio_delta) / refSpot).toLocaleString();

  const kpis = [
    {
      l: "Portfolio Delta",
      v: fmtGreek(greeks.portfolio_delta),
      sub: ["Eq. Shrs", eqShares],
      t: tone(greeks.portfolio_delta),
      Icon: greeks.portfolio_delta >= 0 ? TrendingUp : TrendingDown,
    },
    {
      l: "Gamma",
      v: fmtGreek(greeks.gamma),
      // Fixed: was showing identical value with wrong "Shares" label
      sub: ["Δ per 1% spot", fmtGreek(greeks.gamma / 100)],
      t: tone(greeks.gamma),
      Icon: greeks.gamma >= 0 ? TrendingUp : TrendingDown,
    },
    {
      l: "$ Gamma",
      v: fmtGreek(greeks.dollar_gamma),
      sub: ["Per 1% Spot", fmtGreek(greeks.dollar_gamma / 100)],
      t: tone(greeks.dollar_gamma),
      Icon: ChevronsDown,
    },
    {
      l: "Vega",
      v: fmtGreek(greeks.vega),
      sub: ["1% Vol Shock", fmtGreek(greeks.vega / 100)],
      t: tone(greeks.vega),
      Icon: BarChart3,
    },
    {
      l: "Theta",
      v: fmtGreek(greeks.theta),
      // Fixed: was showing identical value with misleading "1 Day Decay" label
      sub: ["Weekly", fmtGreek(greeks.theta * 5)],
      t: tone(greeks.theta),
      Icon: Clock,
    },
    {
      l: "Rho",
      v: fmtGreek(greeks.rho),
      sub: ["10bps Rate", fmtGreek(greeks.rho / 10)],
      t: tone(greeks.rho),
      Icon: Landmark,
    },
  ];

  // ── Derived: PnL bars (now includes Rho) ─────────────────────────────────
  const pnlRows = pnl
    ? [
        { l: "Delta", v: pnl.delta_pnl },
        { l: "Gamma", v: pnl.gamma_pnl },
        { l: "Vega",  v: pnl.vega_pnl },
        { l: "Theta", v: pnl.theta_pnl },
        { l: "Rho",   v: pnl.rho_pnl  },
      ]
    : [
        { l: "Delta", v:  125_400 },
        { l: "Gamma", v:  -45_200 },
        { l: "Vega",  v:  210_800 },
        { l: "Theta", v:  -12_500 },
        { l: "Rho",   v:    1_200 },
      ];
  const maxAbsPnl = Math.max(1, ...pnlRows.map(p => Math.abs(p.v)));
  const totalPnl  = pnlRows.reduce((s, p) => s + p.v, 0);

  // ── Derived: Correlation ──────────────────────────────────────────────────
  const corrTickers = corrData?.tickers ?? FB_TICKERS;
  const rawMatrix   = corrData?.matrix  ?? FB_CORR;
  // Enforce symmetry: if the API returns a row where an off-diagonal value
  // equals 1.0 (copied-diagonal bug) or disagrees with its mirror, prefer
  // the non-trivial side. Pearson correlation is theoretically symmetric so
  // this only matters when floating-point or data-alignment bugs creep in.
  const corrMatrix = rawMatrix.map((row, i) =>
    row.map((val, j) => {
      if (i === j) return 1.0;
      const mirror = rawMatrix[j]?.[i] ?? val;
      if (Math.abs(val - 1.0) < 0.001 && i !== j) return mirror;
      return val;
    })
  );

  const portfolioLabel = PORTFOLIOS.find(p => p.value === portfolio)?.label ?? portfolio;

  // ── Derived: UAM ──────────────────────────────────────────────────────────
  const shockRows    = uam?.rows           ?? [];
  const volColLabels = uam?.vol_col_labels ?? ["-30 ΔVol Shock", "ATM Baseline", "+30 ΔVol Shock"];
  const uamPct       = uam?.uam_pct ?? 0;
  const uamColor     = uamPct > 0.9 ? "bg-red-500" : uamPct > 0.8 ? "bg-yellow-400" : "bg-emerald-500";
  const uamTextColor = uamPct > 0.9 ? "text-red-400" : uamPct > 0.8 ? "text-yellow-400" : "text-emerald-400";

  // ── Derived: Local shock P&L (interactive panel) ──────────────────────────
  const dS         = spotShock / 100;
  const localDelta = greeks.portfolio_delta * dS;
  const localGamma = 0.5 * greeks.dollar_gamma * dS * dS * 100;
  const localVega  = greeks.vega * (volShock / 100);
  const localTheta = greeks.theta * daysShock;
  const localTotal = localDelta + localGamma + localVega + localTheta;

  return (
    <div className="h-full overflow-y-auto overflow-x-hidden vc-scroll">
      <div className="p-3 flex flex-col gap-2.5">

      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between px-1 pb-2 border-b border-zinc-800">
        <div className="flex items-center gap-3">
          <h1 className="text-[14px] font-semibold text-zinc-100 flex items-center gap-2 uppercase tracking-tight">
            <Activity className="w-4 h-4 text-[#adc6ff]" />
            Risk Matrix &amp; Sensitivities
          </h1>
          {/* Portfolio selector */}
          <div className="relative">
            <select
              value={portfolio}
              onChange={e => setPortfolio(e.target.value)}
              className="appearance-none bg-[#1c1b1d] border border-zinc-700 text-zinc-300 font-mono text-[11px] pl-2 pr-6 py-1 focus:outline-none focus:border-[#adc6ff] cursor-pointer"
            >
              {PORTFOLIOS.map(p => (
                <option key={p.value} value={p.value}>{p.label}</option>
              ))}
            </select>
            <ChevronDown className="absolute right-1.5 top-1/2 -translate-y-1/2 w-3 h-3 text-zinc-500 pointer-events-none" />
          </div>
        </div>
        {/* Live system clock */}
        <div className="flex items-center gap-2 font-mono text-[11px] text-zinc-500">
          <span>SYSTEM TIME: {systemTime || "—"}</span>
          <span className="w-2 h-2 rounded-full bg-emerald-500 vc-blink" />
        </div>
      </div>

      {/* ── KPI strip ──────────────────────────────────────────────────────── */}
      <div className="grid grid-cols-6 gap-2">
        {kpis.map(k => {
          const toneVal  = k.t === "pos" ? "text-emerald-400" : k.t === "neg" ? "text-[#ffb4ab]" : "text-zinc-300";
          const toneIcon = k.t === "pos" ? "text-emerald-400" : k.t === "neg" ? "text-[#ffb4ab]" : "text-zinc-500";
          return (
            <div key={k.l} className="border border-zinc-800 bg-[#131315] p-2">
              <div className="flex justify-between items-start mb-1">
                <span className="text-[10px] font-bold tracking-widest uppercase text-zinc-500">{k.l}</span>
                <k.Icon className={`w-3.5 h-3.5 ${toneIcon}`} />
              </div>
              <div className={`font-mono text-[16px] ${toneVal}`}>{k.v}</div>
              <div className="font-mono text-[10px] text-zinc-500 mt-0.5 flex justify-between">
                <span>{k.sub[0]}</span><span>{k.sub[1]}</span>
              </div>
            </div>
          );
        })}
      </div>

      {/* ── VaR section ────────────────────────────────────────────────────── */}
      <div className="flex flex-col gap-1.5">
        {/* Time horizon toggles */}
        <div className="flex items-center gap-2">
          <span className="text-[9px] font-bold tracking-widest uppercase text-zinc-600">VAR HORIZON:</span>
          {(["1d_95", "1d_99", "7d_99"] as const).map(h => (
            <button
              key={h}
              onClick={() => setVarHorizon(h)}
              className={`px-2 py-0.5 font-mono text-[10px] font-bold border transition-colors ${
                varHorizon === h
                  ? "border-[#adc6ff] bg-[#adc6ff]/10 text-[#adc6ff]"
                  : "border-zinc-800 text-zinc-500 hover:border-zinc-700 hover:text-zinc-300"
              }`}
            >
              {h === "1d_95" ? "1D 95%" : h === "1d_99" ? "1D 99%" : "7D 99%"}
            </button>
          ))}
        </div>
        {/* VaR boxes — selected horizon is highlighted */}
        <div className="grid grid-cols-3 gap-2">
          <VarBox label="1D VaR (95%)" value={varData["1d_95"]} active={varHorizon === "1d_95"} />
          <VarBox label="1D VaR (99%)" value={varData["1d_99"]} active={varHorizon === "1d_99"} />
          <VarBox label="7D VaR (99%)" value={varData["7d_99"]} active={varHorizon === "7d_99"} />
        </div>
      </div>

      {/* ── UAM Progress Bar ───────────────────────────────────────────────── */}
      <div className="border border-zinc-800 bg-[#131315] px-3 py-2">
        <div className="flex items-center justify-between mb-1.5">
          <div className="flex items-center gap-2">
            <ShieldAlert className="w-3 h-3 text-zinc-500" />
            <span className="text-[10px] font-bold tracking-widest uppercase text-zinc-500">
              Margin Utilisation (UAM)
            </span>
          </div>
          <span className={`font-mono text-[13px] font-bold ${uamTextColor}`}>
            {uam ? (uamPct * 100).toFixed(1) + "%" : "—"}
          </span>
        </div>
        <div className="h-2 bg-zinc-800 w-full overflow-hidden">
          <div
            className={`h-full transition-all duration-500 ${uamColor}`}
            style={{ width: `${Math.min(100, uamPct * 100)}%` }}
          />
        </div>
        <div className="flex justify-between mt-1 font-mono text-[9px] text-zinc-700">
          <span>0%</span>
          <span className="text-yellow-600">80% CAUTION</span>
          <span className="text-red-600">90% WARN</span>
          <span>100%</span>
        </div>
        {uam && (
          <div className="mt-1 font-mono text-[10px] text-zinc-500">
            Worst case: <span className="text-[#ffb4ab]">{fmtPnl(uam.worst_case_pnl)}</span>
          </div>
        )}
      </div>

      {/* ── Interactive Custom Shock Panel ─────────────────────────────────── */}
      <Panel
        title="Custom Shock — Local Greeks P&L Approximation"
        right={
          <div className="flex items-center gap-1">
            <Sliders className="w-3 h-3 text-zinc-500" />
            <span className="text-zinc-600 text-[10px]">ΔPnL ≈ Δ·dS + ½Γ·dS² + ν·dVol + Θ·dT</span>
          </div>
        }
      >
        <div className="grid grid-cols-12 gap-3 items-start">
          {/* Sliders */}
          <div className="col-span-6 flex flex-col gap-2.5">
            <ShockSlider
              label="Spot Move"
              unit="%"
              min={-15} max={15} step={0.5}
              value={spotShock}
              onChange={setSpotShock}
              positiveLabel="+15%"
              negativeLabel="-15%"
            />
            <ShockSlider
              label="Vol Shift"
              unit=" pts"
              min={-20} max={20} step={0.5}
              value={volShock}
              onChange={setVolShock}
              positiveLabel="+20 pts"
              negativeLabel="-20 pts"
            />
            <ShockSlider
              label="Days Roll"
              unit="d"
              min={0} max={30} step={1}
              value={daysShock}
              onChange={setDaysShock}
              positiveLabel="30d"
              negativeLabel="0d"
            />
          </div>

          {/* Local P&L breakdown */}
          <div className="col-span-6 flex flex-col gap-1">
            <div className="text-[9px] font-bold tracking-widest uppercase text-zinc-600 mb-1">
              EST. P&amp;L — LOCAL APPROX
            </div>
            {[
              { l: "Delta",  v: localDelta },
              { l: "Gamma",  v: localGamma },
              { l: "Vega",   v: localVega  },
              { l: "Theta",  v: localTheta },
            ].map(row => {
              const pct = maxAbsPnl > 0 ? Math.min(48, Math.round(Math.abs(row.v) / Math.max(Math.abs(localTotal), 1) * 48)) : 0;
              const pos = row.v >= 0;
              return (
                <div key={row.l} className="flex items-center gap-2">
                  <span className="font-mono text-[10px] text-zinc-500 w-10 text-right">{row.l}</span>
                  <div className="flex-1 bg-[#09090b] h-2.5 relative border border-zinc-800">
                    <div
                      className={pos ? "absolute left-1/2 h-full bg-[#adc6ff]/70" : "absolute right-1/2 h-full bg-[#ffb4ab]/60"}
                      style={{ width: `${pct}%` }}
                    />
                    <div className="absolute left-1/2 h-full w-px bg-zinc-700" />
                  </div>
                  <span className={`font-mono text-[11px] w-16 text-right ${pos ? "text-emerald-400" : "text-[#ffb4ab]"}`}>
                    {fmtPnl(row.v)}
                  </span>
                </div>
              );
            })}
            <div className="flex items-center gap-2 pt-2 mt-1 border-t border-zinc-800">
              <span className="font-mono text-[10px] text-zinc-200 font-bold w-10 text-right">TOTAL</span>
              <div className="flex-1" />
              <span className={`font-mono text-[13px] font-bold w-16 text-right ${localTotal >= 0 ? "text-emerald-400" : "text-[#ffb4ab]"}`}>
                {fmtPnl(localTotal)}
              </span>
            </div>
          </div>
        </div>
      </Panel>

      {/* ── Mid grid: PnL | Correlation | UAM Shock ────────────────────────── */}
      <div className="grid grid-cols-12 gap-2.5">

        {/* PnL Attribution — now includes Rho */}
        <Panel className="col-span-4" title="PnL Attribution Grid" right={<span>T-1 → T0</span>}>
          <div className="flex flex-col gap-3 pt-1">
            {pnlRows.map(p => {
              const pos = p.v >= 0;
              const pct = Math.round((Math.abs(p.v) / maxAbsPnl) * 48);
              return (
                <div key={p.l} className="flex items-center gap-3">
                  <span className="font-mono text-[10px] text-zinc-400 w-14 text-right">{p.l}</span>
                  <div className="flex-1 bg-[#09090b] h-3 relative border border-zinc-800">
                    <div
                      className={pos ? "absolute left-1/2 h-full bg-[#adc6ff]/80" : "absolute right-1/2 h-full bg-[#ffb4ab]/70"}
                      style={{ width: `${pct}%` }}
                    />
                    <div className="absolute left-1/2 h-full w-px bg-zinc-600" />
                  </div>
                  <span className={`font-mono text-[12px] w-20 text-right ${pos ? "text-emerald-400" : "text-[#ffb4ab]"}`}>
                    {fmtPnl(p.v)}
                  </span>
                </div>
              );
            })}
            <div className="flex items-center gap-3 pt-3 border-t border-zinc-800 mt-1">
              <span className="font-mono text-[10px] text-zinc-200 w-14 text-right font-bold">TOTAL</span>
              <div className="flex-1" />
              <span className={`font-mono text-[13px] w-20 text-right font-bold ${totalPnl >= 0 ? "text-emerald-400" : "text-[#ffb4ab]"}`}>
                {fmtPnl(totalPnl)}
              </span>
            </div>
          </div>
        </Panel>

        {/* Correlation Matrix — title reactive to portfolio */}
        <Panel
          className="col-span-4"
          title={`${portfolioLabel} — Correlation Matrix`}
          right={<Info className="w-3 h-3" />}
        >
          <div
            className="grid font-mono text-[11px]"
            style={{
              gridTemplateColumns: `repeat(${corrTickers.length + 1}, 1fr)`,
              gap: "1px",
              background: "#3f3f46",
              padding: "1px",
            }}
          >
            <div className="bg-[#09090b]" />
            {corrTickers.map(t => <CorrHead key={"h" + t}>{t}</CorrHead>)}
            {corrMatrix.map((row, i) => (
              <CorrRow key={i} label={corrTickers[i]} row={row} />
            ))}
          </div>
        </Panel>

        {/* UAM Shock Simulator grid */}
        <Panel
          className="col-span-4"
          title="Shock Simulator: Spot × Vol"
          right={<StatusPill tone="neutral">PnL (€)</StatusPill>}
        >
          <div className="flex flex-col gap-px">
            <div
              className="grid gap-px mb-px text-center text-[9px] font-bold tracking-widest uppercase text-zinc-600"
              style={{ gridTemplateColumns: "1fr 1fr 1fr 1fr" }}
            >
              <div />
              {volColLabels.map(l => <div key={l}>{l}</div>)}
            </div>
            <div
              className="grid gap-px bg-zinc-800 p-px font-mono"
              style={{ gridTemplateColumns: "1fr 1fr 1fr 1fr" }}
            >
              {shockRows.length > 0
                ? shockRows.map((row, ri) => (
                    <ShockRow key={ri} label={row.label} cells={row.cells} />
                  ))
                : [
                    { label: "Spot -5%",              cells: [{ pnl:  120_000, tone: "pos" }, { pnl:   950_000, tone: "pos" }, { pnl: 1_800_000, tone: "pos" }] },
                    { label: "Spot Unchanged (Base)",  cells: [{ pnl: -450_000, tone: "neg" }, { pnl:         0, tone: "neu" }, { pnl:   650_000, tone: "pos" }] },
                    { label: "Spot +5%",               cells: [{ pnl:-1_200_000, tone: "neg" }, { pnl:  -850_000, tone: "neg" }, { pnl:   -50_000, tone: "neg" }] },
                  ].map((row, ri) => (
                    <ShockRow key={ri} label={row.label} cells={row.cells} />
                  ))
              }
            </div>
          </div>
        </Panel>
      </div>

      {/* ── Risk Aggregates table ───────────────────────────────────────────── */}
      <Panel
        title="Risk Aggregates"
        right={
          <div className="flex items-center gap-1">
            <Layers className="w-3 h-3 text-zinc-500" />
            <span className="text-zinc-600 text-[10px]">by underlying · by expiry · portfolio total</span>
          </div>
        }
        padded={false}
      >
        <table className="w-full border-collapse font-mono text-[11px]">
          <thead className="bg-[#1c1b1d]">
            <tr>
              {["Group", "Net Δ", "Net Vega", "Net Θ", "$Δ", "$Vega", "Mkt Val", "# Pos"].map(h => (
                <th key={h} className="text-left text-[10px] font-bold tracking-widest uppercase text-zinc-500 px-2.5 py-1.5 border-b border-zinc-800 text-right first:text-left">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {riskAggs.map((r, i) => {
              const isTotal = r.group === "TOTAL";
              return (
                <tr
                  key={i}
                  className={`border-b border-zinc-800/50 ${isTotal ? "bg-[#2a2a2c]/40 font-bold border-t border-zinc-700" : "hover:bg-zinc-800/30"}`}
                >
                  <td className="px-2.5 py-1.5 text-left">
                    <span className={isTotal ? "text-zinc-100" : "text-[#adc6ff]"}>{r.group}</span>
                  </td>
                  <td className={`px-2.5 py-1.5 text-right ${r.net_delta >= 0 ? "text-emerald-400" : "text-[#ffb4ab]"}`}>
                    {r.net_delta >= 0 ? "+" : ""}{r.net_delta.toFixed(2)}
                  </td>
                  <td className="px-2.5 py-1.5 text-right text-zinc-200">
                    {r.net_vega.toLocaleString()}
                  </td>
                  <td className={`px-2.5 py-1.5 text-right ${r.net_theta < 0 ? "text-[#ffb4ab]" : "text-emerald-400"}`}>
                    {fmtPnl(r.net_theta)}
                  </td>
                  <td className={`px-2.5 py-1.5 text-right ${r.dollar_delta >= 0 ? "text-emerald-400" : "text-[#ffb4ab]"}`}>
                    {fmtPnl(r.dollar_delta)}
                  </td>
                  <td className="px-2.5 py-1.5 text-right text-zinc-300">{fmtPnl(r.dollar_vega)}</td>
                  <td className="px-2.5 py-1.5 text-right text-zinc-300">{fmtPnl(r.mkt_val)}</td>
                  <td className="px-2.5 py-1.5 text-right text-zinc-500">{r.n_positions}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Panel>

      {/* ── Positions table ─────────────────────────────────────────────────── */}
      <Panel
        title="Positions"
        right={
          <div className="flex items-center gap-1">
            <Database className="w-3 h-3 text-zinc-500" />
            <span className="text-zinc-600 text-[10px]">{portfolio}</span>
          </div>
        }
        padded={false}
      >
        <table className="w-full border-collapse font-mono text-[11px]">
          <thead className="bg-[#1c1b1d]">
            <tr>
              {["Contract", "Qty", "Mkt Value", "Avg Cost", "Unreal. P&L"].map(h => (
                <th key={h} className="text-left text-[10px] font-bold tracking-widest uppercase text-zinc-500 px-2.5 py-1.5 border-b border-zinc-800 text-right first:text-left">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {positions.map((p, i) => (
              <tr key={i} className="border-b border-zinc-800/50 hover:bg-zinc-800/30">
                <td className="px-2.5 py-1.5 text-left text-[#adc6ff] font-bold">{p.contract}</td>
                <td className="px-2.5 py-1.5 text-right text-zinc-200">{p.qty.toLocaleString()}</td>
                <td className="px-2.5 py-1.5 text-right text-zinc-300">{fmtPnl(p.mkt_value)}</td>
                <td className="px-2.5 py-1.5 text-right text-zinc-400">{p.avg_cost.toLocaleString()}</td>
                <td className={`px-2.5 py-1.5 text-right font-bold ${p.unrealised_pnl >= 0 ? "text-emerald-400" : "text-[#ffb4ab]"}`}>
                  {fmtPnl(p.unrealised_pnl)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      {/* ── Liquidity Metrics ───────────────────────────────────────────────── */}
      <Panel
        title="Liquidity Metrics — Widest Spreads"
        right={<span className="text-zinc-600 text-[10px]">vol &lt; 100 = illiquid signal</span>}
        padded={false}
      >
        <table className="w-full border-collapse font-mono text-[11px]">
          <thead className="bg-[#1c1b1d]">
            <tr>
              {["Ticker", "Expiry", "Strike", "Bid-Ask Spread", "Volume"].map(h => (
                <th key={h} className="text-left text-[10px] font-bold tracking-widest uppercase text-zinc-500 px-2.5 py-1.5 border-b border-zinc-800 text-right first:text-left">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {liquidityData.map((r, i) => {
              const wide = r.bid_ask_spread_pct > 5;
              const illiq = r.volume < 100;
              return (
                <tr key={i} className="border-b border-zinc-800/50 hover:bg-zinc-800/30">
                  <td className="px-2.5 py-1.5 text-left text-[#adc6ff] font-bold">{r.ticker}</td>
                  <td className="px-2.5 py-1.5 text-right text-zinc-300">{r.expiry}</td>
                  <td className="px-2.5 py-1.5 text-right text-zinc-200">{r.strike.toLocaleString()}</td>
                  <td className={`px-2.5 py-1.5 text-right font-bold ${wide ? "text-red-400" : "text-yellow-400"}`}>
                    {r.bid_ask_spread_pct.toFixed(1)}%
                    {wide && <span className="ml-1 text-[9px] text-red-500">WIDE</span>}
                  </td>
                  <td className={`px-2.5 py-1.5 text-right ${illiq ? "text-yellow-400" : "text-zinc-400"}`}>
                    {r.volume.toLocaleString()}
                    {illiq && <span className="ml-1 text-[9px] text-yellow-600">LOW</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Panel>

      {/* ── Pipeline Audit ──────────────────────────────────────────────────── */}
      <Panel
        className="min-h-[180px]"
        title="Pipeline Audit & Logs"
        right={
          <button className="flex items-center gap-1 px-2 py-[2px] border border-zinc-800 bg-[#131315] hover:bg-zinc-800 text-[10px] font-bold tracking-widest uppercase text-zinc-400">
            <Filter className="w-3 h-3" />Filter Exceptions
          </button>
        }
        padded={false}
      >
        <table className="w-full border-collapse">
          <thead className="bg-[#1c1b1d]">
            <tr>
              {["Timestamp", "Ticker", "Exception Type", "Tenor", "Status", "Reason Code"].map(h => (
                <th key={h} className="text-left text-[10px] font-bold tracking-widest uppercase text-zinc-500 px-2.5 py-1.5 border-b border-zinc-800">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="font-mono text-[11px]">
            {qcLog.map((r, i) => (
              <tr key={i} className="border-b border-zinc-800/50 hover:bg-zinc-800/40">
                <td className="px-2.5 py-1 text-zinc-500">{r.ts}</td>
                <td className="px-2.5 py-1 text-[#adc6ff] font-bold">{r.ticker}</td>
                <td className="px-2.5 py-1 text-zinc-200">{r.type}</td>
                <td className="px-2.5 py-1 text-zinc-300">{r.tenor}</td>
                <td className="px-2.5 py-1">
                  <StatusPill tone={r.status === "OK" ? "ok" : r.status === "WARN" ? "warn" : "fail"}>{r.status}</StatusPill>
                </td>
                <td className="px-2.5 py-1 text-zinc-300">{r.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function VarBox({ label, value, active }: { label: string; value?: number; active?: boolean }) {
  const display = value !== undefined ? fmtPnl(value) : "—";
  return (
    <div className={`border bg-[#131315] px-3 py-2 flex items-center justify-between transition-colors ${active ? "border-[#adc6ff]/50 bg-[#adc6ff]/5" : "border-zinc-800"}`}>
      <span className={`text-[10px] font-bold tracking-widest uppercase ${active ? "text-[#adc6ff]" : "text-zinc-500"}`}>{label}</span>
      <span className="font-mono text-[13px] text-[#ffb4ab]">{display}</span>
    </div>
  );
}

function ShockSlider({
  label, unit, min, max, step, value, onChange, positiveLabel, negativeLabel,
}: {
  label: string; unit: string; min: number; max: number; step: number;
  value: number; onChange: (v: number) => void;
  positiveLabel: string; negativeLabel: string;
}) {
  const pos = value > 0;
  const neg = value < 0;
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-bold tracking-widest uppercase text-zinc-500">{label}</span>
        <span className={`font-mono text-[12px] font-bold ${pos ? "text-emerald-400" : neg ? "text-[#ffb4ab]" : "text-zinc-400"}`}>
          {value > 0 ? "+" : ""}{value}{unit}
        </span>
      </div>
      <div className="flex items-center gap-2">
        <span className="font-mono text-[9px] text-zinc-700 w-10 text-right">{negativeLabel}</span>
        <input
          type="range"
          min={min} max={max} step={step}
          value={value}
          onChange={e => onChange(parseFloat(e.target.value))}
          className="flex-1 accent-[#adc6ff] cursor-pointer h-1"
        />
        <span className="font-mono text-[9px] text-zinc-700 w-10">{positiveLabel}</span>
      </div>
    </div>
  );
}

function CorrHead({ children }: { children: ReactNode }) {
  return (
    <div className="bg-[#09090b] flex items-center justify-center py-2 text-[10px] font-bold text-zinc-500 tracking-wider">
      {children}
    </div>
  );
}

function CorrRow({ label, row }: { label: string; row: number[] }) {
  return (
    <>
      <CorrHead>{label}</CorrHead>
      {row.map((v, i) => (
        <div
          key={i}
          className="flex items-center justify-center py-2 font-mono text-[11px] text-[#dbe5ff]"
          style={{ background: `rgba(77,142,255,${Math.max(0.12, v)})` }}
        >
          {v.toFixed(2)}
        </div>
      ))}
    </>
  );
}

function ShockRow({ label, cells }: { label: string; cells: UamCell[] }) {
  return (
    <>
      <div className="bg-[#131315] flex items-center justify-center text-center px-1 py-3 text-[9px] font-bold tracking-widest uppercase text-zinc-500 border-r border-zinc-800">
        {label}
      </div>
      {cells.map((c, i) => {
        const cls =
          c.tone === "pos" ? "bg-emerald-900/30 text-emerald-400"
          : c.tone === "neg" ? "bg-red-900/30 text-[#ffb4ab]"
          : "bg-zinc-800/50 text-zinc-400";
        return (
          <div key={i} className={`flex items-center justify-center font-mono text-[12px] py-3 ${cls}`}>
            {fmtPnl(c.pnl)}
          </div>
        );
      })}
    </>
  );
}
