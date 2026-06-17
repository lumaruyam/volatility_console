import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  TrendingUp, TrendingDown, ChevronsDown, BarChart3,
  Clock, Landmark, Activity, Info, Filter, ShieldAlert,
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
  gamma: -1_240_000,
  dollar_gamma: -385_200,
  vega: 850_400,
  theta: -12_500,
  rho: 45_100,
};

const FB_TICKERS = ["SX5E", "ASML", "MC.PA", "SAP", "TTE"];
const FB_CORR = [
  [1.00, 0.82, 0.88, 0.79, 0.65],
  [0.82, 1.00, 0.54, 0.89, 0.45],
  [0.88, 0.54, 1.00, 0.48, 0.52],
  [0.79, 0.89, 0.48, 1.00, 0.49],
  [0.65, 0.45, 0.52, 0.49, 1.00],
];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function RiskAnalysis() {
  const { data: greeks = FB_GREEKS } = useQuery<GreeksData>({
    queryKey:  ["risk-greeks"],
    queryFn:   () => fetch("/api/risk/greeks").then(r => r.json()),
    staleTime: 30_000,
  });

  const { data: varData } = useQuery<VarData>({
    queryKey:  ["risk-var"],
    queryFn:   () => fetch("/api/risk/var").then(r => r.json()),
    staleTime: 60_000,
  });

  const { data: pnl } = useQuery<PnLData>({
    queryKey:  ["risk-pnl"],
    queryFn:   () => fetch("/api/risk/pnl-attribution").then(r => r.json()),
    staleTime: 30_000,
  });

  const { data: corrData } = useQuery<CorrData>({
    queryKey:    ["risk-correlation"],
    queryFn:     () => fetch("/api/risk/correlation").then(r => r.json()),
    staleTime:   300_000,
  });

  const { data: uam } = useQuery<UamData>({
    queryKey:  ["risk-uam"],
    queryFn:   () => fetch("/api/risk/uam").then(r => r.json()),
    staleTime: 30_000,
  });

  const { data: qcLog = [] } = useQuery<QcEntry[]>({
    queryKey:      ["risk-qc-log"],
    queryFn:       () => fetch("/api/risk/qc-log").then(r => r.json()),
    staleTime:     10_000,
    refetchInterval: 30_000,
  });

  // --- Derived: KPI tiles ---
  const kpis = [
    { l: "Portfolio Delta", v: fmtGreek(greeks.portfolio_delta), sub: ["Eq. Shrs", "125,400"],          t: tone(greeks.portfolio_delta), Icon: greeks.portfolio_delta >= 0 ? TrendingUp : TrendingDown },
    { l: "Gamma",           v: fmtGreek(greeks.gamma),           sub: ["Shares", fmtGreek(greeks.gamma)], t: tone(greeks.gamma),           Icon: greeks.gamma >= 0 ? TrendingUp : TrendingDown },
    { l: "$ Gamma",         v: fmtGreek(greeks.dollar_gamma),    sub: ["Per 1% Spot", fmtGreek(greeks.dollar_gamma / 100)], t: tone(greeks.dollar_gamma), Icon: ChevronsDown },
    { l: "Vega",            v: fmtGreek(greeks.vega),            sub: ["1% Vol Shock", fmtGreek(greeks.vega / 100)],       t: tone(greeks.vega),         Icon: BarChart3 },
    { l: "Theta",           v: fmtGreek(greeks.theta),           sub: ["1 Day Decay",  fmtGreek(greeks.theta)],             t: tone(greeks.theta),        Icon: Clock },
    { l: "Rho",             v: fmtGreek(greeks.rho),             sub: ["10bps Rate",   fmtGreek(greeks.rho / 10)],          t: tone(greeks.rho),          Icon: Landmark },
  ];

  // --- Derived: PnL bars ---
  const pnlRows = pnl
    ? [
        { l: "Delta", v: pnl.delta_pnl },
        { l: "Gamma", v: pnl.gamma_pnl },
        { l: "Vega",  v: pnl.vega_pnl  },
        { l: "Theta", v: pnl.theta_pnl },
      ]
    : [
        { l: "Delta", v:  125_400 },
        { l: "Gamma", v:  -45_200 },
        { l: "Vega",  v:  210_800 },
        { l: "Theta", v: -12_500  },
      ];
  const maxAbsPnl = Math.max(1, ...pnlRows.map(p => Math.abs(p.v)));
  const totalPnl  = pnlRows.reduce((s, p) => s + p.v, 0) + (pnl?.rho_pnl ?? 1_200);

  // --- Derived: Correlation ---
  const corrTickers = corrData?.tickers  ?? FB_TICKERS;
  const corrMatrix  = corrData?.matrix   ?? FB_CORR;

  // --- Derived: UAM shock grid ---
  const shockRows      = uam?.rows            ?? [];
  const volColLabels   = uam?.vol_col_labels  ?? ["-30 ΔVol Shock", "ATM Baseline", "+30 ΔVol Shock"];

  return (
    <div className="p-3 flex flex-col gap-2.5 min-h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-1 pb-2 border-b border-zinc-800">
        <h1 className="text-[14px] font-semibold text-zinc-100 flex items-center gap-2 uppercase tracking-tight">
          <Activity className="w-4 h-4 text-[#adc6ff]" />
          Risk Matrix &amp; Sensitivities
        </h1>
        <div className="flex items-center gap-2 font-mono text-[11px] text-zinc-500">
          <span>SYSTEM TIME: 09:41:22.015 GMT</span>
          <span className="w-2 h-2 rounded-full bg-emerald-500 vc-blink" />
        </div>
      </div>

      {/* KPI strip */}
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

      {/* VaR strip */}
      <div className="grid grid-cols-3 gap-2">
        <VarBox label="1D VaR (95%)"  value={varData?.["1d_95"]} />
        <VarBox label="1D VaR (99%)"  value={varData?.["1d_99"]} />
        <VarBox label="7D VaR (99%)"  value={varData?.["7d_99"]} />
      </div>

      {/* Mid grid: PnL attribution | Correlation | UAM shock */}
      <div className="grid grid-cols-12 gap-2.5" style={{ minHeight: 320 }}>

        {/* PnL Attribution */}
        <Panel className="col-span-4" title="PnL Attribution Grid" right={<span>T-1 → T0</span>}>
          <div className="flex flex-col gap-3 pt-1">
            {pnlRows.map(p => {
              const pos = p.v >= 0;
              const pct = Math.round((Math.abs(p.v) / maxAbsPnl) * 48); // max 48% half-bar
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

        {/* Correlation Matrix */}
        <Panel className="col-span-4" title="Euro Stoxx 50 Component Correlation Matrix" right={<Info className="w-3 h-3" />}>
          <div className="grid font-mono text-[11px]" style={{ gridTemplateColumns: `repeat(${corrTickers.length + 1}, 1fr)`, gap: "1px", background: "#3f3f46", padding: "1px" }}>
            <div className="bg-[#09090b]" />
            {corrTickers.map(t => <CorrHead key={"h"+t}>{t}</CorrHead>)}
            {corrMatrix.map((row, i) => (
              <CorrRow key={i} label={corrTickers[i]} row={row} />
            ))}
          </div>
        </Panel>

        {/* UAM Shock Simulator */}
        <Panel className="col-span-4" title="Shock Simulator: Spot × Vol" right={<StatusPill tone="neutral">PnL (€)</StatusPill>}>
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
                : /* fallback static rows */
                  [
                    { label: "Spot -5%",             cells: [{ pnl:  120_000, tone: "pos" }, { pnl:  950_000, tone: "pos" }, { pnl: 1_800_000, tone: "pos" }] },
                    { label: "Spot Unchanged (Base)", cells: [{ pnl: -450_000, tone: "neg" }, { pnl:        0, tone: "neu" }, { pnl:   650_000, tone: "pos" }] },
                    { label: "Spot +5%",             cells: [{ pnl:-1_200_000, tone: "neg" }, { pnl: -850_000, tone: "neg" }, { pnl:   -50_000, tone: "neg" }] },
                  ].map((row, ri) => (
                    <ShockRow key={ri} label={row.label} cells={row.cells} />
                  ))
              }
            </div>
          </div>
          {uam && (
            <div className="mt-2 flex items-center gap-2 font-mono text-[10px] text-zinc-500">
              <ShieldAlert className="w-3 h-3 text-[#ffb4ab]" />
              <span>UAM: <span className="text-[#ffb4ab]">{(uam.uam_pct * 100).toFixed(2)}%</span></span>
              <span className="ml-2">Worst: <span className="text-[#ffb4ab]">{fmtPnl(uam.worst_case_pnl)}</span></span>
            </div>
          )}
        </Panel>
      </div>

      {/* Pipeline Audit */}
      <Panel
        className="flex-1 min-h-[180px]"
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
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function VarBox({ label, value }: { label: string; value?: number }) {
  const display = value !== undefined ? fmtPnl(value) : "—";
  return (
    <div className="border border-zinc-800 bg-[#131315] px-3 py-2 flex items-center justify-between">
      <span className="text-[10px] font-bold tracking-widest uppercase text-zinc-500">{label}</span>
      <span className="font-mono text-[13px] text-[#ffb4ab]">{display}</span>
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
