import { useState, useEffect, useRef } from "react";
import { useMutation } from "@tanstack/react-query";
import { Zap } from "lucide-react";
import { Panel, Chip } from "../ui";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type Method = "Parallel Grid Shift" | "Historical Copula Resampling" | "VIX-Indexed Skew Stressing";

type RepriceRequest = {
  spot_stress: number;
  vol_stress: number;
  rate_stress_bps: number;
  methodology: string;
  active_methods: number;
};

type MatrixCell = { spot_pct: number; vol_pct: number; pnl_eur: number; nav_bps: number };

type RepriceResponse = {
  scenario_matrix: MatrixCell[][];
  spot_row_labels: string[];
  vol_col_labels: string[];
  base_portfolio_value: number;
  nav_total: number;
  aggregate_shift_pct: number;
  active_methods: number;
  rate_bps: number;
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

// ---------------------------------------------------------------------------
// Client-side fallback (used before first API response)
// ---------------------------------------------------------------------------

function clientCellPnl(sPct: number, vPct: number, rate: number): number {
  const delta = 0.32 * 500_000;
  const gamma = 28_000;
  const vega  = 120_000;
  return delta * sPct + 0.5 * gamma * sPct ** 2 + vega * vPct * 0.01 - 18_000 * rate * 0.01;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ShockSimulator() {
  const [methodActive, setMethodActive] = useState<Record<Method, boolean>>({
    "Parallel Grid Shift": true,
    "Historical Copula Resampling": false,
    "VIX-Indexed Skew Stressing": false,
  });
  const [spotShock, setSpotShock] = useState(0);
  const [volShock,  setVolShock]  = useState(0);
  const [rateShock, setRateShock] = useState(0);

  const repriceMutation = useMutation<RepriceResponse, Error, RepriceRequest>({
    mutationFn: (body) =>
      fetch("/api/shock/reprice", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(body),
      }).then(r => r.json()),
  });

  // Stable mutate ref — lets useEffect avoid adding mutate to its deps
  const mutateRef = useRef(repriceMutation.mutate);
  mutateRef.current = repriceMutation.mutate;
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const activeCount   = METHODS.filter(m => methodActive[m]).length;
    const primaryMethod = (METHODS.find(m => methodActive[m]) ?? METHODS[0]);

    debounceRef.current = setTimeout(() => {
      mutateRef.current({
        spot_stress:      spotShock / 100,
        vol_stress:       volShock  / 100,
        rate_stress_bps:  rateShock,
        methodology:      METHOD_SLUGS[primaryMethod],
        active_methods:   Math.max(1, activeCount),
      });
    }, 300);

    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [spotShock, volShock, rateShock, methodActive]);

  const apiMatrix = repriceMutation.data?.scenario_matrix;
  const maxAbsPnl = apiMatrix
    ? Math.max(1, ...apiMatrix.flat().map(c => Math.abs(c.pnl_eur)))
    : 2_000_000;

  function getCell(ri: number, ci: number): { pnl: number; nav_bps: number } {
    if (apiMatrix) {
      const c = apiMatrix[ri][ci];
      return { pnl: c.pnl_eur, nav_bps: c.nav_bps };
    }
    const sTot = SPOT_ROWS[ri] + spotShock;
    const vTot = VOL_COLS[ci]  + volShock;
    const pnl  = clientCellPnl(sTot, vTot, rateShock);
    return { pnl, nav_bps: pnl / 250_000 };
  }

  // Footer stats — API values or local fallback
  const apiStats       = repriceMutation.data;
  const aggregateShift = apiStats != null
    ? apiStats.aggregate_shift_pct
    : parseFloat(Math.sqrt(spotShock ** 2 + (volShock * 0.4) ** 2).toFixed(2));
  const activeMethods  = apiStats?.active_methods ?? METHODS.filter(m => methodActive[m]).length;
  const rateBpsDisplay = apiStats?.rate_bps ?? rateShock;

  const handleReset = () => { setSpotShock(0); setVolShock(0); setRateShock(0); };

  return (
    <div className="flex flex-col gap-3 p-1">
      {/* Methodology strip */}
      <div className="flex flex-wrap items-center gap-2 p-2.5 border border-zinc-800 bg-[#131315]">
        <span className="text-[10px] font-bold tracking-[0.18em] uppercase text-zinc-500 mr-1">
          Simulation Methodology
        </span>
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
          onClick={handleReset}
          className="ml-auto px-3 py-1.5 border border-zinc-700 bg-[#09090b] text-zinc-300 text-[11px] font-bold tracking-wider uppercase hover:bg-zinc-800"
        >
          Reset Shocks
        </button>
        {repriceMutation.isPending && (
          <span className="font-mono text-[10px] text-[#adc6ff] animate-pulse flex items-center gap-1">
            <Zap className="w-3 h-3" />REPRICING…
          </span>
        )}
      </div>

      <div className="grid grid-cols-12 gap-3">
        {/* Left: Manual shock controls */}
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
          </div>

          {/* Footer stats */}
          <div className="mt-4 pt-3 border-t border-zinc-800 grid grid-cols-3 gap-2 text-[10px] font-mono">
            <Kpi
              k="Aggregate Shift"
              v={`${aggregateShift.toFixed != null ? aggregateShift.toFixed(2) : aggregateShift}%`}
            />
            <Kpi k="Active Methods" v={`${activeMethods}/3`} />
            <Kpi k="Rate" v={`${rateBpsDisplay >= 0 ? "+" : ""}${rateBpsDisplay} bps`} />
          </div>
        </Panel>

        {/* Right: Scenario matrix */}
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

          {/* Loss / Gain intensity legend */}
          <div className="mt-3 flex items-center justify-between text-[10px] font-mono text-zinc-500">
            <span>Loss intensity</span>
            <div className="flex items-center gap-1">
              <span className="w-5 h-3" style={{ background: "rgba(244,63,94,0.75)" }} />
              <span className="w-5 h-3" style={{ background: "rgba(244,63,94,0.40)" }} />
              <span className="w-5 h-3 bg-zinc-900 border border-zinc-800" />
              <span className="w-5 h-3" style={{ background: "rgba(34,197,94,0.40)" }} />
              <span className="w-5 h-3" style={{ background: "rgba(34,197,94,0.75)" }} />
            </div>
            <span>Gain intensity</span>
          </div>
        </Panel>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function ShockSlider({
  label, min, max, step, value, onChange, format, baseline,
}: {
  label: string; min: number; max: number; step: number; value: number;
  onChange: (v: number) => void; format: (v: number) => string; baseline: string;
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

function Kpi({ k, v }: { k: string; v: string }) {
  return (
    <div className="border border-zinc-800 bg-[#09090b] px-2 py-1.5">
      <div className="text-zinc-500 text-[9px] uppercase tracking-wider">{k}</div>
      <div className="text-zinc-200 font-mono">{v}</div>
    </div>
  );
}
