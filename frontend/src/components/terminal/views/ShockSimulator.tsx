import { useState, useEffect, useRef } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import {
  Zap, AlertTriangle, TrendingDown, Activity, Layers,
} from "lucide-react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine, Legend,
} from "recharts";
import { Panel, Chip } from "../ui";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type Method = "Parallel Grid Shift" | "Historical Copula Resampling" | "VIX-Indexed Skew Stressing";

type RepriceRequest = {
  spot_stress: number;
  vol_stress: number;
  rate_stress_bps: number;
  corr_stress?: number;
  methodology: string;
  active_methods: number;
  portfolio_id?: string;
  asset_classes?: string[];
};

type MatrixCell = { spot_pct: number; vol_pct: number; pnl_eur: number; nav_bps: number };

type HedgeSuggestion = {
  action: string;
  reason: string;
  urgency: "HIGH" | "MEDIUM" | "LOW";
  estimated_delta_reduction?: number;
};

type RepriceResponse = {
  scenario_matrix: MatrixCell[][];
  spot_row_labels: string[];
  vol_col_labels: string[];
  base_portfolio_value: number;
  nav_total: number;
  aggregate_shift_pct: number;
  active_methods: number;
  rate_bps: number;
  hedging_suggestions?: HedgeSuggestion[];
};

type LiquidityImpactRow = {
  contract: string;
  pre_spread_pct: number;
  post_spread_pct: number;
  volume_impact_pct: number;
};

type GreeksSnapshot = {
  portfolio_delta: number;
  dollar_gamma: number;
  vega: number;
  theta: number;
  portfolio_value?: number;
};

type SurfaceShiftData = {
  maturities: string[];
  atm_vol_before: number[];
  atm_vol_after: number[];
};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const METHODS: Method[] = [
  "Parallel Grid Shift",
  "Historical Copula Resampling",
  "VIX-Indexed Skew Stressing",
];
const METHOD_SLUGS: Record<Method, string> = {
  "Parallel Grid Shift":          "parallel_grid_shift",
  "Historical Copula Resampling": "historical_copula",
  "VIX-Indexed Skew Stressing":   "vix_indexed_skew",
};

const SPOT_ROWS  = [-5, 0, 5];
const VOL_COLS   = [-30, 0, 30];
const ROW_LABELS = ["Spot −5%", "Spot Unchanged (Base)", "Spot +5%"];
const COL_LABELS = ["−30 ΔVol Shock", "ATM Baseline", "+30 ΔVol Shock"];

const PORTFOLIOS    = ["SX5E_STRADDLE", "DISPERSION_Q3", "CALENDAR_SPD"] as const;
const ASSET_CLASSES = ["Options", "Futures", "Stocks"] as const;

// Fallback surface (base ATM term structure — "after" shifts with volShock at render time)
const FB_SURFACE_BASE = [
  { maturity: "1M",  vol: 18.2 },
  { maturity: "3M",  vol: 19.4 },
  { maturity: "6M",  vol: 20.1 },
  { maturity: "12M", vol: 20.8 },
  { maturity: "18M", vol: 21.2 },
  { maturity: "24M", vol: 21.5 },
];

const FB_HEDGE_SUGGESTIONS: HedgeSuggestion[] = [
  { action: "Buy SX5E 4000P Dec26", reason: "Delta exposure +42 under spot shock", urgency: "HIGH",   estimated_delta_reduction: 38 },
  { action: "Sell 3M variance swap",  reason: "Vega exposure exceeds 5% NAV",        urgency: "MEDIUM", estimated_delta_reduction: 0  },
];

const FB_LIQUIDITY: LiquidityImpactRow[] = [
  { contract: "SX5E 4000P DEC26", pre_spread_pct: 2.1,  post_spread_pct: 8.4,  volume_impact_pct: -42 },
  { contract: "ASML 900C SEP26",  pre_spread_pct: 5.9,  post_spread_pct: 14.2, volume_impact_pct: -61 },
  { contract: "SX5E 4400C DEC26", pre_spread_pct: 1.8,  post_spread_pct: 6.3,  volume_impact_pct: -35 },
  { contract: "MC.PA 500P SEP26", pre_spread_pct: 8.2,  post_spread_pct: 19.7, volume_impact_pct: -74 },
  { contract: "SX5E 3800P MAR27", pre_spread_pct: 3.4,  post_spread_pct: 9.8,  volume_impact_pct: -48 },
];

// ---------------------------------------------------------------------------
// Client-side fallback — uses live portfolio Greeks when available
// ---------------------------------------------------------------------------

function clientCellPnl(
  sPct: number,
  vPct: number,
  rate: number,
  greeks: GreeksSnapshot | null,
): number {
  const pv           = greeks?.portfolio_value ?? 1_000_000;
  const deltaNotl    = greeks != null ? greeks.portfolio_delta * pv : 0.32 * 500_000;
  const gamma        = greeks?.dollar_gamma ?? 28_000;
  const vega         = greeks?.vega         ?? 120_000;
  return deltaNotl * (sPct / 100) + 0.5 * gamma * (sPct / 100) ** 2
    + vega * (vPct / 100) - 18_000 * (rate / 100) * 0.01;
}

const apiFetch = (url: string) =>
  fetch(url).then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); });

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ShockSimulator() {
  const [methodActive, setMethodActive] = useState<Record<Method, boolean>>({
    "Parallel Grid Shift": true,
    "Historical Copula Resampling": false,
    "VIX-Indexed Skew Stressing": false,
  });
  const [spotShock, setSpotShock]     = useState(0);
  const [volShock,  setVolShock]      = useState(0);
  const [rateShock, setRateShock]     = useState(0);
  const [corrShock, setCorrShock]     = useState(0);
  const [portfolio, setPortfolio]     = useState<string>("SX5E_STRADDLE");
  const [assetClasses, setAssetCls]   = useState<string[]>(["Options", "Futures", "Stocks"]);
  const [showSurface, setShowSurface] = useState(false);

  // ── Queries ───────────────────────────────────────────────────────────────
  const { data: greeksLive } = useQuery<GreeksSnapshot>({
    queryKey:  ["shock-greeks", portfolio],
    queryFn:   () => apiFetch(`/api/risk/greeks?portfolio=${portfolio}`),
    staleTime: 60_000,
  });

  const { data: surfaceShift } = useQuery<SurfaceShiftData>({
    queryKey:  ["shock-surface", spotShock, volShock, portfolio],
    queryFn:   () =>
      apiFetch(`/api/shock/surface-before-after?spot=${spotShock}&vol=${volShock}&portfolio=${portfolio}`),
    enabled:   showSurface,
    staleTime: 30_000,
  });

  // ── Mutations ─────────────────────────────────────────────────────────────
  const repriceMutation = useMutation<RepriceResponse, Error, RepriceRequest>({
    mutationFn: (body) =>
      fetch("/api/shock/reprice", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(body),
      }).then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }),
  });

  const liquidityMutation = useMutation<LiquidityImpactRow[], Error, { spot: number; vol: number; portfolio: string }>({
    mutationFn: (body) =>
      fetch("/api/shock/liquidity-impact", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(body),
      }).then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }),
  });

  // ── Debounced reprice (fires on any shock/method/portfolio change) ─────────
  const mutateRef          = useRef(repriceMutation.mutate);
  const liquidityMutateRef = useRef(liquidityMutation.mutate);
  mutateRef.current          = repriceMutation.mutate;
  liquidityMutateRef.current = liquidityMutation.mutate;
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const activeCount   = METHODS.filter(m => methodActive[m]).length;
    const primaryMethod = METHODS.find(m => methodActive[m]) ?? METHODS[0];

    debounceRef.current = setTimeout(() => {
      mutateRef.current({
        spot_stress:     spotShock / 100,
        vol_stress:      volShock  / 100,
        rate_stress_bps: rateShock,
        corr_stress:     corrShock,
        methodology:     METHOD_SLUGS[primaryMethod],
        active_methods:  Math.max(1, activeCount),
        portfolio_id:    portfolio,
        asset_classes:   assetClasses,
      });
      liquidityMutateRef.current({ spot: spotShock, vol: volShock, portfolio });
    }, 300);

    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [spotShock, volShock, rateShock, corrShock, methodActive, portfolio, assetClasses]);

  // ── Derived ───────────────────────────────────────────────────────────────
  const apiMatrix  = repriceMutation.data?.scenario_matrix;
  const maxAbsPnl  = apiMatrix
    ? Math.max(1, ...apiMatrix.flat().map(c => Math.abs(c.pnl_eur)))
    : 2_000_000;

  const greeks = greeksLive ?? null;

  function getCell(ri: number, ci: number): { pnl: number; nav_bps: number } {
    if (apiMatrix) {
      const c = apiMatrix[ri][ci];
      return { pnl: c.pnl_eur, nav_bps: c.nav_bps };
    }
    const sTot = SPOT_ROWS[ri] + spotShock;
    const vTot = VOL_COLS[ci]  + volShock;
    const pnl  = clientCellPnl(sTot, vTot, rateShock, greeks);
    return { pnl, nav_bps: pnl / 250_000 };
  }

  const apiStats       = repriceMutation.data;
  const aggregateShift = apiStats != null
    ? apiStats.aggregate_shift_pct
    : parseFloat(Math.sqrt(spotShock ** 2 + (volShock * 0.4) ** 2).toFixed(2));
  const activeMethods  = apiStats?.active_methods ?? METHODS.filter(m => methodActive[m]).length;
  const rateBpsDisplay = apiStats?.rate_bps ?? rateShock;

  // Vol surface chart data
  const surfaceChartData = surfaceShift
    ? surfaceShift.maturities.map((m, i) => ({
        maturity: m,
        before:   +(surfaceShift.atm_vol_before[i] * 100).toFixed(2),
        after:    +(surfaceShift.atm_vol_after[i]  * 100).toFixed(2),
      }))
    : FB_SURFACE_BASE.map(p => ({
        maturity: p.maturity,
        before:   p.vol,
        after:    +(p.vol + volShock * 0.1).toFixed(2),
      }));

  const hedgeSuggestions = apiStats?.hedging_suggestions
    ?? (Math.abs(spotShock) > 3 || Math.abs(volShock) > 10 ? FB_HEDGE_SUGGESTIONS : []);

  const liquidityData = liquidityMutation.data
    ?? (Math.abs(spotShock) > 2 || Math.abs(volShock) > 5 ? FB_LIQUIDITY : []);

  const toggleAssetClass = (cls: string) => {
    setAssetCls(prev =>
      prev.includes(cls)
        ? prev.length > 1 ? prev.filter(c => c !== cls) : prev  // keep at least one
        : [...prev, cls],
    );
  };

  const handleReset = () => {
    setSpotShock(0); setVolShock(0); setRateShock(0); setCorrShock(0);
  };

  return (
    <div className="h-full min-h-0 overflow-x-hidden overflow-y-auto vc-scroll flex flex-col gap-3 p-1">

      {/* ── Filter + methodology strip ──────────────────────────────────── */}
      <div className="flex flex-col gap-2 p-2.5 border border-zinc-800 bg-[#131315]">
        {/* Row 1: portfolio + asset class */}
        <div className="flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-bold tracking-[0.18em] uppercase text-zinc-500">PORTFOLIO:</span>
            <select
              value={portfolio}
              onChange={e => setPortfolio(e.target.value)}
              className="bg-[#0e0e10] border border-zinc-800 text-zinc-200 font-mono text-[11px] py-1 px-2 focus:outline-none focus:border-[#adc6ff] uppercase"
            >
              {PORTFOLIOS.map(p => <option key={p}>{p}</option>)}
            </select>
          </div>
          <div className="w-px h-4 bg-zinc-800" />
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-bold tracking-[0.18em] uppercase text-zinc-500">ASSET CLASS:</span>
            {ASSET_CLASSES.map(cls => (
              <Chip
                key={cls}
                active={assetClasses.includes(cls)}
                onClick={() => toggleAssetClass(cls)}
              >
                {cls}
              </Chip>
            ))}
          </div>
          {greeksLive && (
            <span className="ml-auto font-mono text-[9px] text-emerald-400/70">
              ✓ live Greeks loaded
            </span>
          )}
        </div>

        {/* Row 2: simulation methodology + actions */}
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[10px] font-bold tracking-[0.18em] uppercase text-zinc-500">METHOD:</span>
          {METHODS.map(m => (
            <Chip
              key={m}
              active={methodActive[m]}
              onClick={() => setMethodActive(s => ({ ...s, [m]: !s[m] }))}
            >
              {methodActive[m] ? "▣ " : "▢ "}{m}
            </Chip>
          ))}
          <button
            onClick={() => setShowSurface(v => !v)}
            className={`ml-auto flex items-center gap-1.5 px-2.5 py-1 font-bold tracking-widest uppercase text-[10px] border transition-colors ${
              showSurface
                ? "border-[#adc6ff] bg-[#adc6ff]/10 text-[#adc6ff]"
                : "border-zinc-700 text-zinc-400 hover:border-zinc-600"
            }`}
          >
            <Layers className="w-3 h-3" />VOL SURFACE Δ
          </button>
          <button
            onClick={handleReset}
            className="px-3 py-1 border border-zinc-700 bg-[#09090b] text-zinc-300 text-[11px] font-bold tracking-wider uppercase hover:bg-zinc-800"
          >
            Reset
          </button>
          {repriceMutation.isPending && (
            <span className="font-mono text-[10px] text-[#adc6ff] animate-pulse flex items-center gap-1">
              <Zap className="w-3 h-3" />REPRICING…
            </span>
          )}
        </div>
      </div>

      {/* ── Main: shock controls + scenario matrix ────────────────────────── */}
      <div className="grid grid-cols-12 gap-3">
        <Panel title="Manual Shock Controls" className="col-span-12 xl:col-span-5">
          <div className="space-y-3">
            <ShockSlider
              label="Underlying Spot Price Shock"
              min={-20} max={20} step={0.25} value={spotShock} onChange={setSpotShock}
              format={v => `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`}
              baseline="0.00%"
            />
            <ShockSlider
              label="Global Volatility Surface Shift"
              min={-30} max={30} step={0.5} value={volShock} onChange={setVolShock}
              format={v => `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`}
              baseline="0.00%"
            />
            <ShockSlider
              label="Interest Rate (r) Parallel Shift"
              min={-200} max={200} step={5} value={rateShock} onChange={setRateShock}
              format={v => `${v >= 0 ? "+" : ""}${v.toFixed(0)} bps`}
              baseline="0 bps"
            />
            <ShockSlider
              label="Correlation Shock (ρ)"
              min={-1} max={1} step={0.05} value={corrShock} onChange={setCorrShock}
              format={v => `${v >= 0 ? "+" : ""}${v.toFixed(2)}`}
              baseline="0.00"
              accentColor="#fb923c"
            />
          </div>

          {/* Footer KPIs */}
          <div className="mt-4 pt-3 border-t border-zinc-800 grid grid-cols-4 gap-2 text-[10px] font-mono">
            <Kpi k="Agg. Shift"    v={`${aggregateShift.toFixed ? aggregateShift.toFixed(2) : aggregateShift}%`} />
            <Kpi k="Methods"       v={`${activeMethods}/3`} />
            <Kpi k="Rate"          v={`${rateBpsDisplay >= 0 ? "+" : ""}${rateBpsDisplay} bps`} />
            <Kpi k="ρ Shock"       v={`${corrShock >= 0 ? "+" : ""}${corrShock.toFixed(2)}`}
              valueClass={corrShock !== 0 ? "text-[#fb923c]" : "text-zinc-200"} />
          </div>
        </Panel>

        <Panel title="Scenario Evaluation Matrix" className="col-span-12 xl:col-span-7">
          <table className="w-full border-collapse text-[11px] font-mono table-fixed">
            <colgroup>
              <col style={{ width: "22%" }} />
              <col style={{ width: "26%" }} />
              <col style={{ width: "26%" }} />
              <col style={{ width: "26%" }} />
            </colgroup>
            <thead>
              <tr className="bg-[#1c1b1d]">
                <th className="px-2 py-2 text-left text-[10px] uppercase tracking-[0.18em] text-zinc-500 border-b border-zinc-700">
                  Spot \ Vol
                </th>
                {COL_LABELS.map(c => (
                  <th key={c} className="px-2 py-2 text-center text-[11px] font-bold uppercase tracking-wider text-[#adc6ff] border-b border-zinc-700 border-l border-zinc-800">
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {ROW_LABELS.map((rLabel, ri) => (
                <tr key={rLabel}>
                  <th className="px-3 py-3 text-left text-[10px] uppercase tracking-wider text-zinc-400 border-r border-zinc-800">
                    {rLabel}
                  </th>
                  {VOL_COLS.map((_, ci) => {
                    const { pnl, nav_bps } = getCell(ri, ci);
                    const pos       = pnl >= 0;
                    const intensity = Math.min(1, Math.abs(pnl) / maxAbsPnl);
                    const alpha     = (0.15 + intensity * 0.6).toFixed(2);
                    const bg        = pos
                      ? `rgba(34,197,94,${alpha})`
                      : `rgba(244,63,94,${alpha})`;
                    return (
                      <td
                        key={ci}
                        className="px-3 py-3 text-center border border-zinc-900"
                        style={{ background: bg }}
                      >
                        <div className={`text-base font-semibold ${pos ? "text-emerald-100" : "text-rose-100"}`}>
                          {pos ? "+" : ""}€{(pnl / 1e6).toFixed(2)}M
                        </div>
                        <div className="text-[10px] text-zinc-300/70 mt-0.5">
                          {nav_bps >= 0 ? "+" : ""}{nav_bps.toFixed(1)} bps NAV
                        </div>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>

          {/* Legend */}
          <div className="mt-3 flex items-center justify-between text-[10px] font-mono text-zinc-500">
            <span>Loss intensity</span>
            <div className="flex items-center gap-1">
              {["0.75", "0.40", "0"].map((a, i) => (
                <span key={i} className="w-5 h-3" style={{ background: i === 2 ? "#27272a" : `rgba(244,63,94,${a})` }} />
              ))}
              {["0.40", "0.75"].map((a, i) => (
                <span key={i} className="w-5 h-3" style={{ background: `rgba(34,197,94,${a})` }} />
              ))}
            </div>
            <span>Gain intensity</span>
          </div>
        </Panel>
      </div>

      {/* ── Vol surface before/after ─────────────────────────────────────── */}
      {showSurface && (
        <Panel
          title="ATM Vol Term Structure — Baseline vs Shocked"
          icon={<Activity className="w-3.5 h-3.5 text-[#adc6ff]" />}
          right={
            <div className="flex items-center gap-3 text-[10px] font-mono text-zinc-500">
              <span className="flex items-center gap-1.5">
                <span className="w-5 h-[2px] bg-zinc-600" />Baseline
              </span>
              <span className="flex items-center gap-1.5">
                <span className="w-5 h-[2px] bg-[#adc6ff]" />Shocked
              </span>
            </div>
          }
          className="h-[220px]"
        >
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={surfaceChartData} margin={{ top: 4, right: 16, bottom: 8, left: 8 }}>
              <CartesianGrid strokeDasharray="2 4" stroke="#27272a" />
              <XAxis
                dataKey="maturity"
                tick={{ fill: "#71717a", fontSize: 9, fontFamily: "monospace" }}
                axisLine={{ stroke: "#3f3f46" }}
                tickLine={false}
              />
              <YAxis
                tickFormatter={v => `${v}%`}
                tick={{ fill: "#71717a", fontSize: 9, fontFamily: "monospace" }}
                axisLine={false}
                tickLine={false}
                width={36}
                domain={["auto", "auto"]}
              />
              <Tooltip
                contentStyle={{ background: "#09090b", border: "1px solid #3f3f46", borderRadius: 0 }}
                labelStyle={{ color: "#71717a", fontSize: 10 }}
                formatter={(v: number, name: string) => [`${v.toFixed(2)}%`, name === "before" ? "Baseline" : "Shocked"]}
              />
              {volShock !== 0 && (
                <Legend
                  wrapperStyle={{ fontSize: 9, fontFamily: "monospace", color: "#71717a" }}
                  formatter={v => v === "before" ? "Baseline" : `Shocked (Δvol ${volShock >= 0 ? "+" : ""}${volShock}%)`}
                />
              )}
              <Line type="monotone" dataKey="before" stroke="#52525b" strokeWidth={1.5} dot={false} />
              <Line type="monotone" dataKey="after"  stroke="#adc6ff" strokeWidth={2}   dot={false} strokeDasharray={volShock === 0 ? "4 2" : undefined} />
              {volShock !== 0 && (
                <ReferenceLine
                  y={surfaceChartData[0]?.before}
                  stroke="#52525b" strokeDasharray="2 4" strokeWidth={1}
                />
              )}
            </LineChart>
          </ResponsiveContainer>
        </Panel>
      )}

      {/* ── Hedging suggestions ──────────────────────────────────────────── */}
      {hedgeSuggestions.length > 0 && (
        <Panel
          title="Hedging Suggestions"
          icon={<Zap className="w-3.5 h-3.5 text-[#adc6ff]" />}
          right={
            <span className="font-mono text-[9px] text-zinc-600">
              {apiStats?.hedging_suggestions ? "LIVE" : "FALLBACK"}
            </span>
          }
        >
          <div className="grid grid-cols-2 gap-2">
            {hedgeSuggestions.map((s, i) => (
              <div
                key={i}
                className={`border p-2.5 flex flex-col gap-1.5 ${
                  s.urgency === "HIGH"   ? "border-[#ffb4ab]/40 bg-red-900/10"    :
                  s.urgency === "MEDIUM" ? "border-yellow-500/30 bg-yellow-900/10" :
                                           "border-zinc-700 bg-[#1c1b1d]"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className={`text-[9px] font-bold tracking-widest uppercase px-1.5 py-[1px] border ${
                    s.urgency === "HIGH"   ? "border-[#ffb4ab]/50 text-[#ffb4ab]"  :
                    s.urgency === "MEDIUM" ? "border-yellow-400/50 text-yellow-400" :
                                             "border-zinc-700 text-zinc-400"
                  }`}>
                    {s.urgency}
                  </span>
                  {s.estimated_delta_reduction != null && s.estimated_delta_reduction > 0 && (
                    <span className="font-mono text-[9px] text-emerald-400">
                      Δ −{s.estimated_delta_reduction}
                    </span>
                  )}
                </div>
                <div className="font-mono text-[12px] text-zinc-200 font-semibold">{s.action}</div>
                <div className="text-[10px] text-zinc-500">{s.reason}</div>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {/* ── Liquidity impact ─────────────────────────────────────────────── */}
      {liquidityData.length > 0 && (
        <Panel
          title="Liquidity Impact — Top Affected Options"
          icon={<TrendingDown className="w-3.5 h-3.5 text-[#ffb4ab]" />}
          right={
            <span className="font-mono text-[9px] text-zinc-600">
              {liquidityMutation.data ? "LIVE" : "FALLBACK"} · bid-ask widening under shock
            </span>
          }
          padded={false}
        >
          <table className="w-full border-collapse font-mono text-[11px]">
            <thead className="bg-[#1c1b1d]">
              <tr>
                {["Contract", "Pre-Shock Spread", "Post-Shock Spread", "Vol Impact"].map(h => (
                  <th key={h} className="px-2.5 py-1.5 text-left text-[10px] font-bold uppercase tracking-widest text-zinc-500 border-b border-zinc-800">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {liquidityData.map((row, i) => {
                const worsened = row.post_spread_pct > row.pre_spread_pct;
                const spreadDelta = row.post_spread_pct - row.pre_spread_pct;
                return (
                  <tr key={i} className="border-b border-zinc-800/50 hover:bg-zinc-800/30">
                    <td className="px-2.5 py-1.5 text-zinc-200">{row.contract}</td>
                    <td className="px-2.5 py-1.5 text-zinc-400">{row.pre_spread_pct.toFixed(2)}%</td>
                    <td className={`px-2.5 py-1.5 font-semibold ${worsened ? "text-[#ffb4ab]" : "text-emerald-400"}`}>
                      {row.post_spread_pct.toFixed(2)}%
                      <span className="ml-1 text-[9px]">
                        ({worsened ? "+" : ""}{spreadDelta.toFixed(2)}pp)
                      </span>
                    </td>
                    <td className={`px-2.5 py-1.5 ${row.volume_impact_pct < 0 ? "text-[#ffb4ab]" : "text-zinc-400"}`}>
                      {row.volume_impact_pct >= 0 ? "+" : ""}{row.volume_impact_pct}%
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="px-2.5 py-1 text-[9px] font-mono text-zinc-700 border-t border-zinc-800">
            Wide spread threshold: &gt;5% of mid — post-shock values flagged in red
          </div>
        </Panel>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function ShockSlider({
  label, min, max, step, value, onChange, format, baseline, accentColor,
}: {
  label: string; min: number; max: number; step: number; value: number;
  onChange: (v: number) => void; format: (v: number) => string; baseline: string;
  accentColor?: string;
}) {
  return (
    <div className="border border-zinc-800 bg-[#09090b] p-3">
      <div className="flex items-baseline justify-between mb-2">
        <span className="text-[10px] font-bold tracking-[0.18em] uppercase text-zinc-400">{label}</span>
        <span className={`font-mono text-sm ${value === 0 ? "text-zinc-300" : value > 0 ? "text-emerald-300" : "text-rose-300"}`}>
          {format(value)}
        </span>
      </div>
      <input
        type="range"
        className="vc-slider w-full"
        style={accentColor ? { accentColor } : undefined}
        min={min} max={max} step={step}
        value={value}
        onChange={e => onChange(Number(e.target.value))}
      />
      <div className="flex justify-between text-[10px] font-mono text-zinc-600 mt-1.5">
        <span>{min}</span>
        <span>Baseline {baseline}</span>
        <span>+{max}</span>
      </div>
    </div>
  );
}

function Kpi({ k, v, valueClass }: { k: string; v: string; valueClass?: string }) {
  return (
    <div className="border border-zinc-800 bg-[#09090b] px-2 py-1.5">
      <div className="text-zinc-500 text-[9px] uppercase tracking-wider">{k}</div>
      <div className={`font-mono ${valueClass ?? "text-zinc-200"}`}>{v}</div>
    </div>
  );
}
