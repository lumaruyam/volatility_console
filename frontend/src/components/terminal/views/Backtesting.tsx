import type { ReactNode } from "react";
import { useState, useRef, useEffect } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import {
  History, TrendingDown, MoreVertical, BarChart3,
} from "lucide-react";
import {
  ComposedChart, Area, Line, LineChart, BarChart, Bar, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  ReferenceLine,
} from "recharts";
import { Panel, Chip } from "../ui";

// ---------------------------------------------------------------------------
// API types
// ---------------------------------------------------------------------------

type BacktestStats = {
  cumulative_pnl_ann_pct: number;
  vs_benchmark_pct: number;
  sharpe: number;
  rf_rate: number;
  win_rate_pct: number;
  max_drawdown_pct: number;
  avg_trade_pnl_eur?: number;
};

type GreeksTimePoint = {
  date: string;
  delta: number;
  gamma: number;
  vega: number;
  theta: number;
};

type BacktestResult = {
  timestamp_vector: string[];
  cumulative_pnl_vector: number[];
  benchmark_pnl_vector: number[];
  drawdown_vector: number[];
  stats: BacktestStats;
  greeks_over_time?: GreeksTimePoint[];
};

type McResult = {
  simulation_path_terminal_returns: number[];
  var_95_pct: number;
};

type BacktestRequest = {
  strategy_id: string;
  start_date: string;
  end_date: string;
  rebalance_frequency: string;
  shock_preset: string | null;
  underlying?: string;
};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const TODAY = "2026-06-18";

const TIME_PRESETS: { label: string; start: string }[] = [
  { label: "1Y",  start: "2025-06-18" },
  { label: "3Y",  start: "2023-06-18" },
  { label: "5Y",  start: "2021-06-18" },
  { label: "MAX", start: "2005-01-01" },
];

const SHOCK_WINDOWS: Record<string, [string, string]> = {
  "2008 Crash":           ["2007-01-01", "2009-12-31"],
  "2020 Liquidity Shock": ["2019-10-01", "2021-06-30"],
  "BREXIT":               ["2016-01-01", "2017-06-30"],
  "COVID Vol Spike":      ["2020-01-01", "2021-12-31"],
};

const SHOCK_PRESETS = Object.keys(SHOCK_WINDOWS);

const UNDERLYINGS = ["SX5E", "SPX", "NDX", "DAX", "FTSE100"];

const GREEK_COLORS: Record<string, string> = {
  delta: "#adc6ff",
  gamma: "#4ade80",
  vega:  "#fb923c",
  theta: "#f87171",
};

// ---------------------------------------------------------------------------
// Fallback (only used before first API response — hide v BM when isLive=false)
// ---------------------------------------------------------------------------

const FB_STATS: BacktestStats = {
  cumulative_pnl_ann_pct: 12.45,
  vs_benchmark_pct:       3.8,
  sharpe:                 1.85,
  rf_rate:                4.5,
  win_rate_pct:           62.8,
  max_drawdown_pct:       -8.2,
  avg_trade_pnl_eur:      4_250,
};

// ---------------------------------------------------------------------------
// Histogram helper
// ---------------------------------------------------------------------------

const BIN_MIN = -40, BIN_MAX = 80, N_BINS = 30;
const BIN_WIDTH = (BIN_MAX - BIN_MIN) / N_BINS;

function makeBins(returns: number[]): { x: number; count: number }[] {
  const bins = Array.from({ length: N_BINS }, (_, i) => ({
    x: parseFloat((BIN_MIN + (i + 0.5) * BIN_WIDTH).toFixed(1)),
    count: 0,
  }));
  returns.forEach(r => {
    const i = Math.min(N_BINS - 1, Math.max(0, Math.floor((r - BIN_MIN) / BIN_WIDTH)));
    bins[i].count++;
  });
  return bins;
}

const apiFetch = (url: string) =>
  fetch(url).then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); });

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function Backtesting() {
  const [strat, setStrat]             = useState("VOL_CARRY_01");
  const [underlying, setUnderlying]   = useState("SX5E");
  const [activeShock, setShock]       = useState<string | null>(null);
  const [startDate, setStartDate]     = useState("2005-01-01");
  const [endDate, setEndDate]         = useState(TODAY);
  const [timePreset, setTimePreset]   = useState<string>("MAX");

  // ── Strategy list ─────────────────────────────────────────────────────────
  const { data: stratList = ["VOL_CARRY_01", "SX5E_STRADDLE", "DISPERSION_Q3"] } =
    useQuery<string[]>({
      queryKey:  ["backtest-strategies"],
      queryFn:   () => apiFetch("/api/backtest/strategies"),
      staleTime: 300_000,
    });

  // ── Backtest run (POST) ────────────────────────────────────────────────────
  const btMutation = useMutation<BacktestResult, Error, BacktestRequest>({
    mutationFn: (body) =>
      fetch("/api/backtest/run", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(body),
      }).then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }),
  });

  // ── Monte Carlo ────────────────────────────────────────────────────────────
  const { data: mcData } = useQuery<McResult>({
    queryKey:  ["monte-carlo", strat, underlying],
    queryFn:   () =>
      apiFetch(`/api/backtest/monte-carlo?n_paths=500&strategy_id=${strat}&underlying=${underlying}`),
    staleTime: 300_000,
  });

  // Stable ref so event handlers don't need mutate in deps
  const mutateRef = useRef(btMutation.mutate);
  mutateRef.current = btMutation.mutate;

  // Auto-run default backtest on first mount so the chart is never blank
  useEffect(() => {
    mutateRef.current({
      strategy_id: strat,
      shock_preset: null,
      start_date: startDate,
      end_date: endDate,
      rebalance_frequency: "weekly",
      underlying,
    });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const runBacktest = (
    sid: string,
    shock: string | null,
    sd: string,
    ed: string,
    und: string,
  ) => {
    mutateRef.current({
      strategy_id:        sid,
      shock_preset:       shock,
      start_date:         sd,
      end_date:           ed,
      rebalance_frequency: "weekly",
      underlying:         und,
    });
  };

  // ── Handlers ──────────────────────────────────────────────────────────────
  const handleStratChange = (s: string) => {
    setStrat(s);
    runBacktest(s, activeShock, startDate, endDate, underlying);
  };

  const handleUnderlyingChange = (u: string) => {
    setUnderlying(u);
    runBacktest(strat, activeShock, startDate, endDate, u);
  };

  const handlePreset = (label: string, start: string) => {
    setTimePreset(label);
    setStartDate(start);
    setEndDate(TODAY);
    setShock(null);
    runBacktest(strat, null, start, TODAY, underlying);
  };

  const handleShockClick = (s: string) => {
    if (activeShock === s) {
      setShock(null);
      setTimePreset("MAX");
      setStartDate("2005-01-01");
      setEndDate(TODAY);
      runBacktest(strat, null, "2005-01-01", TODAY, underlying);
    } else {
      const [sd, ed] = SHOCK_WINDOWS[s];
      setShock(s);
      setStartDate(sd);
      setEndDate(ed);
      setTimePreset("CUSTOM");
      runBacktest(strat, s, sd, ed, underlying);
    }
  };

  const handleDateChange = (sd: string, ed: string) => {
    setStartDate(sd);
    setEndDate(ed);
    setTimePreset("CUSTOM");
    setShock(null);
    runBacktest(strat, null, sd, ed, underlying);
  };

  // ── Derived ───────────────────────────────────────────────────────────────
  const result  = btMutation.data;
  const pending = btMutation.isPending;
  const isLive  = !!result;
  const stats   = result?.stats ?? FB_STATS;

  const equityData = result
    ? result.timestamp_vector.map((t, i) => ({
        date:  t,
        strat: result.cumulative_pnl_vector[i],
        bm:    result.benchmark_pnl_vector[i],
        dd:    Math.min(0, result.drawdown_vector[i]),
      }))
    : [];

  const mcReturns = mcData?.simulation_path_terminal_returns ?? [];
  const mcBins    = makeBins(mcReturns);
  const var95     = mcData?.var_95_pct ?? -12.84;

  const probProfit = mcReturns.length > 0
    ? (mcReturns.filter(r => r > 0).length / mcReturns.length * 100).toFixed(1) + "%"
    : "—";

  const greeksData = result?.greeks_over_time ?? [];

  return (
    <div className="p-3 flex flex-col gap-2.5 h-full min-h-0 overflow-x-hidden overflow-y-auto vc-scroll">

      {/* ── Filter ribbon ─────────────────────────────────────────────────── */}
      <section className="border border-zinc-800 bg-[#131315] p-2 flex flex-col gap-2 shrink-0">
        {/* Row 1: strategy + underlying + time presets + custom dates */}
        <div className="flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-2 px-2 py-[3px] border border-zinc-800 bg-[#0e0e10] shrink-0">
            <History className="w-3.5 h-3.5 text-[#adc6ff]" />
            <span className="font-mono text-[11px] text-zinc-200 tracking-wider uppercase font-bold">
              Replay Frame
            </span>
          </div>

          <div className="flex items-center gap-2">
            <span className="text-[10px] font-bold tracking-widest uppercase text-zinc-500">STRAT:</span>
            <select
              value={strat}
              onChange={e => handleStratChange(e.target.value)}
              className="bg-[#0e0e10] border border-zinc-800 text-zinc-200 font-mono text-[11px] py-1 px-2 focus:outline-none focus:border-[#adc6ff] uppercase"
            >
              {stratList.map(s => <option key={s}>{s}</option>)}
            </select>
          </div>

          <div className="flex items-center gap-2">
            <span className="text-[10px] font-bold tracking-widest uppercase text-zinc-500">UNDERLYING:</span>
            <select
              value={underlying}
              onChange={e => handleUnderlyingChange(e.target.value)}
              className="bg-[#0e0e10] border border-zinc-800 text-zinc-200 font-mono text-[11px] py-1 px-2 focus:outline-none focus:border-[#adc6ff] uppercase"
            >
              {UNDERLYINGS.map(u => <option key={u}>{u}</option>)}
            </select>
          </div>

          <div className="flex items-center gap-1.5 ml-auto">
            <span className="text-[10px] font-bold tracking-widest uppercase text-zinc-500">TIME:</span>
            {TIME_PRESETS.map(({ label, start }) => (
              <button
                key={label}
                onClick={() => handlePreset(label, start)}
                className={`px-2 py-[2px] font-mono text-[10px] font-bold border transition-colors ${
                  timePreset === label && !activeShock
                    ? "border-[#adc6ff] bg-[#adc6ff]/10 text-[#adc6ff]"
                    : "border-zinc-800 text-zinc-500 hover:border-zinc-700 hover:text-zinc-300"
                }`}
              >
                {label}
              </button>
            ))}
            <span className="mx-1 text-zinc-700 text-[10px]">|</span>
            <input
              type="date"
              value={startDate}
              onChange={e => handleDateChange(e.target.value, endDate)}
              className="bg-[#0e0e10] border border-zinc-800 text-zinc-400 font-mono text-[10px] px-1.5 py-[3px] focus:outline-none focus:border-[#adc6ff] w-[120px]"
            />
            <span className="text-zinc-700 text-[10px]">→</span>
            <input
              type="date"
              value={endDate}
              onChange={e => handleDateChange(startDate, e.target.value)}
              className="bg-[#0e0e10] border border-zinc-800 text-zinc-400 font-mono text-[10px] px-1.5 py-[3px] focus:outline-none focus:border-[#adc6ff] w-[120px]"
            />
          </div>
        </div>

        {/* Row 2: shock presets + running indicator */}
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[10px] font-bold tracking-widest uppercase text-zinc-500">SHOCK PRESET:</span>
          {SHOCK_PRESETS.map(s => (
            <Chip key={s} active={activeShock === s} onClick={() => handleShockClick(s)}>
              {s}
            </Chip>
          ))}
          {activeShock && (
            <button
              onClick={() => handleShockClick(activeShock)}
              className="text-[10px] font-mono text-zinc-600 hover:text-zinc-300 border border-zinc-800 px-2 py-[2px]"
            >
              ✕ Clear
            </button>
          )}
          {pending && (
            <span className="font-mono text-[10px] text-[#adc6ff] animate-pulse ml-auto">
              RUNNING BACKTEST…
            </span>
          )}
        </div>
      </section>

      {/* ── Charts ────────────────────────────────────────────────────────── */}
      <section className="grid grid-cols-2 gap-2.5 flex-1 min-h-[360px]">
        <Panel
          title={activeShock ? `Cumulative PnL — ${activeShock}` : "Cumulative PnL vs Benchmark"}
          right={
            <div className="flex items-center gap-3">
              <LegendDot color="#adc6ff" label="Strategy" />
              <LegendDot color="#52525b" label={underlying} />
              <LegendDot color="#ffb4ab" label="Drawdown" opacity={0.5} />
            </div>
          }
        >
          {equityData.length > 0 ? (
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={equityData} margin={{ top: 4, right: 4, bottom: 16, left: 28 }}>
                <CartesianGrid strokeDasharray="2 4" stroke="#27272a" />
                <XAxis
                  dataKey="date"
                  tickFormatter={v => (v as string).slice(0, 4)}
                  interval={Math.floor(equityData.length / 5)}
                  tick={{ fill: "#71717a", fontSize: 9, fontFamily: "monospace" }}
                  axisLine={{ stroke: "#3f3f46" }}
                  tickLine={false}
                />
                <YAxis
                  tickFormatter={v => `${(v as number) > 0 ? "+" : ""}${(v as number).toFixed(0)}%`}
                  tick={{ fill: "#71717a", fontSize: 9, fontFamily: "monospace" }}
                  axisLine={false}
                  tickLine={false}
                  width={32}
                />
                <Tooltip
                  cursor={{ stroke: "#adc6ff40", strokeWidth: 1, strokeDasharray: "4 2" }}
                  contentStyle={{ background: "#09090b", border: "1px solid #3f3f46", borderRadius: 0 }}
                  labelStyle={{ color: "#71717a", fontSize: 10 }}
                  itemStyle={{ fontSize: 11, fontFamily: "monospace" }}
                  formatter={(v: number, name: string) => [
                    `${v > 0 ? "+" : ""}${v.toFixed(2)}%`,
                    name === "strat" ? "Strategy" : name === "bm" ? underlying : "Drawdown",
                  ]}
                />
                <Area
                  type="monotone" dataKey="dd"
                  fill="#ffb4ab" fillOpacity={0.2} stroke="none"
                />
                <Line
                  type="monotone" dataKey="bm"
                  stroke="#52525b" strokeWidth={1.5} dot={false} strokeOpacity={0.7}
                />
                <Line
                  type="monotone" dataKey="strat"
                  stroke="#adc6ff" strokeWidth={2.2} dot={false}
                />
              </ComposedChart>
            </ResponsiveContainer>
          ) : (
            <ChartSkeleton />
          )}
        </Panel>

        <Panel
          title="Monte Carlo Return Paths Distribution"
          right={<MoreVertical className="w-3 h-3" />}
        >
          {mcBins.some(b => b.count > 0) ? (
            <div className="relative h-full">
              <div className="absolute top-1 left-4 z-10">
                <div className="bg-[#09090b] px-2 py-1 border border-[#ffb4ab]/40">
                  <div className="font-mono text-[9px] text-[#ffb4ab] tracking-widest uppercase">95% Expected VaR</div>
                  <div className="font-mono text-[14px] text-[#ffb4ab]">{var95.toFixed(2)}%</div>
                </div>
              </div>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={mcBins} margin={{ top: 4, right: 4, bottom: 16, left: 8 }}>
                  <CartesianGrid strokeDasharray="2 4" stroke="#27272a" vertical={false} />
                  <XAxis
                    dataKey="x"
                    type="number"
                    domain={[BIN_MIN, BIN_MAX]}
                    tickFormatter={v => `${v}%`}
                    ticks={[-40, -20, 0, 20, 40, 60, 80]}
                    tick={{ fill: "#71717a", fontSize: 9, fontFamily: "monospace" }}
                    axisLine={{ stroke: "#3f3f46" }}
                    tickLine={false}
                  />
                  <YAxis hide />
                  <Tooltip
                    cursor={{ fill: "#adc6ff10" }}
                    contentStyle={{ background: "#09090b", border: "1px solid #3f3f46", borderRadius: 0 }}
                    labelStyle={{ color: "#71717a", fontSize: 10 }}
                    formatter={(v: number) => [v, "Paths"]}
                    labelFormatter={(x: number) => `Return: ~${(x as number).toFixed(1)}%`}
                  />
                  <ReferenceLine
                    x={var95} stroke="#ffb4ab" strokeDasharray="4 2" strokeWidth={1.5}
                  />
                  <Bar dataKey="count" barSize={BIN_WIDTH * 0.85}>
                    {mcBins.map((b, i) => (
                      <Cell
                        key={i}
                        fill={b.x < var95 ? "#ffb4ab" : "#adc6ff"}
                        fillOpacity={b.x < var95 ? 0.6 : 0.3}
                      />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <ChartSkeleton />
          )}
        </Panel>
      </section>

      {/* ── Greeks over time ──────────────────────────────────────────────── */}
      <section className="grid grid-cols-4 gap-2 shrink-0" style={{ height: 140 }}>
        {(["delta", "gamma", "vega", "theta"] as const).map(g => (
          <Panel
            key={g}
            title={`${g.toUpperCase()} / Time`}
            icon={<BarChart3 className="w-3 h-3" style={{ color: GREEK_COLORS[g] }} />}
          >
            {greeksData.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={greeksData} margin={{ top: 2, right: 4, bottom: 8, left: 0 }}>
                  <XAxis
                    dataKey="date"
                    tickFormatter={v => (v as string).slice(0, 4)}
                    interval={Math.floor(greeksData.length / 3)}
                    tick={{ fill: "#52525b", fontSize: 8, fontFamily: "monospace" }}
                    axisLine={false}
                    tickLine={false}
                  />
                  <YAxis
                    tick={{ fill: "#52525b", fontSize: 8, fontFamily: "monospace" }}
                    axisLine={false}
                    tickLine={false}
                    width={28}
                  />
                  <Tooltip
                    cursor={{ stroke: GREEK_COLORS[g] + "40", strokeWidth: 1 }}
                    contentStyle={{ background: "#09090b", border: "1px solid #3f3f46", borderRadius: 0, fontSize: 10 }}
                    labelStyle={{ color: "#71717a", fontSize: 9 }}
                    itemStyle={{ fontSize: 10, fontFamily: "monospace", color: GREEK_COLORS[g] }}
                    formatter={(v: number) => [v.toFixed(4), g.toUpperCase()]}
                  />
                  <Line
                    type="monotone"
                    dataKey={g}
                    stroke={GREEK_COLORS[g]}
                    strokeWidth={1.5}
                    dot={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex flex-col items-center justify-center h-full gap-1">
                <div className="w-full h-[2px] animate-pulse" style={{ background: GREEK_COLORS[g] + "40" }} />
                <span className="font-mono text-[9px] text-zinc-700">awaiting greeks_over_time</span>
              </div>
            )}
          </Panel>
        ))}
      </section>

      {/* ── Stat grid ─────────────────────────────────────────────────────── */}
      <section className="grid grid-cols-6 gap-2.5 shrink-0">
        <StatCard
          label="Cumulative PnL (Ann.)"
          value={`${stats.cumulative_pnl_ann_pct >= 0 ? "+" : ""}${stats.cumulative_pnl_ann_pct.toFixed(2)}%`}
          valueClass={stats.cumulative_pnl_ann_pct >= 0 ? "text-emerald-400" : "text-[#ffb4ab]"}
          tail={isLive
            ? `${stats.vs_benchmark_pct >= 0 ? "+" : ""}${stats.vs_benchmark_pct.toFixed(1)} v BM`
            : undefined}
          tailTone={stats.vs_benchmark_pct >= 0 ? "emerald" : undefined}
        />
        <StatCard
          label="Sharpe Ratio"
          value={stats.sharpe.toFixed(2)}
          valueClass="text-zinc-100"
          tail={`rf=${stats.rf_rate}%`}
        />
        <StatCard
          label="Win Rate %"
          value={`${stats.win_rate_pct.toFixed(1)}%`}
          valueClass="text-zinc-100"
        >
          <div className="w-16 h-1 bg-zinc-800">
            <div
              className="h-full bg-[#adc6ff]"
              style={{ width: `${Math.min(100, stats.win_rate_pct)}%` }}
            />
          </div>
        </StatCard>
        <StatCard
          label="Max Drawdown"
          value={`${stats.max_drawdown_pct.toFixed(1)}%`}
          valueClass="text-[#ffb4ab]"
        >
          <TrendingDown className="w-4 h-4 text-[#ffb4ab]" />
        </StatCard>
        <StatCard
          label="Prob. of Profit"
          value={probProfit}
          valueClass={
            probProfit === "—" ? "text-zinc-600" :
            parseFloat(probProfit) >= 50 ? "text-emerald-400" : "text-[#ffb4ab]"
          }
          tail={mcReturns.length > 0 ? `${mcReturns.length} paths` : undefined}
        />
        <StatCard
          label="Avg Trade PnL"
          value={
            stats.avg_trade_pnl_eur != null
              ? `€${(stats.avg_trade_pnl_eur >= 0 ? "+" : "") + (stats.avg_trade_pnl_eur / 1000).toFixed(1)}K`
              : "—"
          }
          valueClass={
            stats.avg_trade_pnl_eur == null ? "text-zinc-600" :
            stats.avg_trade_pnl_eur >= 0 ? "text-emerald-400" : "text-[#ffb4ab]"
          }
        />
      </section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function LegendDot({ color, label, opacity = 1 }: { color: string; label: string; opacity?: number }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className="w-2 h-2 rounded-full" style={{ backgroundColor: color, opacity }} />
      <span className="font-mono text-[10px] text-zinc-400">{label}</span>
    </span>
  );
}

function ChartSkeleton() {
  return (
    <div className="flex items-center justify-center h-full">
      <span className="font-mono text-[11px] text-zinc-600 animate-pulse">COMPUTING…</span>
    </div>
  );
}

function StatCard({
  label, value, valueClass, tail, tailTone, children,
}: {
  label: string;
  value: string;
  valueClass: string;
  tail?: string;
  tailTone?: "emerald";
  children?: ReactNode;
}) {
  return (
    <div className="border border-zinc-800 bg-[#131315] p-3 flex flex-col gap-2 hover:bg-[#1c1b1d] group">
      <span className="text-[10px] font-bold tracking-widest uppercase text-zinc-500 group-hover:text-[#adc6ff]">
        {label}
      </span>
      <div className="flex items-end justify-between">
        <span className={`font-mono text-[22px] leading-none ${valueClass}`}>{value}</span>
        <div className="flex flex-col items-end gap-1">
          {tail && (
            <span
              className={`font-mono text-[10px] px-1 border ${
                tailTone === "emerald"
                  ? "text-emerald-400 border-emerald-500/30 bg-emerald-500/10"
                  : "text-zinc-500 border-zinc-800"
              }`}
            >
              {tail}
            </span>
          )}
          {children}
        </div>
      </div>
    </div>
  );
}
