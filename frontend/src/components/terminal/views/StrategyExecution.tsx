import type { ReactNode } from "react";
import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { ArrowLeftRight, ScatterChart, ListOrdered, Bot, AlertTriangle, Info, Zap } from "lucide-react";
import { Panel, StatusPill } from "../ui";

// ---------------------------------------------------------------------------
// API types
// ---------------------------------------------------------------------------

type StrategyPosition = {
  strategy_id: string;
  strategy_name: string;
  strategy_label: string;
  target_strike: string;
  expiry: string;
  open_interest: number;
  allocated_margin_eur: number;
  allocated_margin_pct: number;
  pnl_intraday_eur: number;
  live_exec: boolean;
  legs: string[];
};

type OrderBookRow = {
  time: string;
  bid_size: number;
  bid: number;
  ask: number;
  ask_size: number;
  spread_pct: number;
  wide: boolean;
};

type HedgeSuggestion = {
  type: string;
  severity: string;
  message: string;
  action: string;
  age_display: string;
};

// ---------------------------------------------------------------------------
// Fallback data (shown while loading)
// ---------------------------------------------------------------------------

const FB_POSITIONS: StrategyPosition[] = [
  {
    strategy_id: "strat_001", strategy_name: "SX5E 12-Month Straddle",
    strategy_label: "ALPHA_CORE_V1", target_strike: "4200 / 4200",
    expiry: "12M (Dec 26)", open_interest: 1450,
    allocated_margin_eur: 2_400_000, allocated_margin_pct: 14.2,
    pnl_intraday_eur: 12_450, live_exec: true,
    legs: ["Call 4200 DEC26", "Put 4200 DEC26"],
  },
  {
    strategy_id: "strat_002", strategy_name: "Dispersion Basket",
    strategy_label: "VOL_ARB_Q3", target_strike: "N/A",
    expiry: "3M (Sep 26)", open_interest: 8200,
    allocated_margin_eur: 5_100_000, allocated_margin_pct: 22.5,
    pnl_intraday_eur: -3_210, live_exec: true,
    legs: [],
  },
];

const FB_ORDERS: OrderBookRow[] = [
  { time: "10:42:01", bid_size: 150, bid: 4201.50, ask: 4203.00, ask_size: 200, spread_pct: 0.03,  wide: false },
  { time: "10:42:02", bid_size:  50, bid:   15.20, ask:   16.10, ask_size:  10, spread_pct: 5.92,  wide: true  },
  { time: "10:42:02", bid_size: 500, bid: 4199.00, ask: 4205.50, ask_size: 450, spread_pct: 0.15,  wide: false },
  { time: "10:42:04", bid_size:  12, bid:    0.85, ask:    0.95, ask_size: 100, spread_pct: 11.76, wide: true  },
  { time: "10:42:05", bid_size: 300, bid: 4200.00, ask: 4204.00, ask_size: 300, spread_pct: 0.09,  wide: false },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtEur(v: number): string {
  const sign = v >= 0 ? "+" : "";
  const abs  = Math.abs(v);
  if (abs >= 1_000_000) return `${sign}€${(v / 1e6).toFixed(1)}M`;
  if (abs >= 1_000)     return `${sign}€${(v / 1e3).toFixed(0)}K`;
  return `${sign}€${v}`;
}

function postJson(url: string, body: object) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(r => r.json());
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function StrategyExecution() {
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  const flash = (msg: string) => {
    setActionMsg(msg);
    setTimeout(() => setActionMsg(null), 3000);
  };

  const { data: positions = FB_POSITIONS } = useQuery<StrategyPosition[]>({
    queryKey:  ["strategy-positions"],
    queryFn:   () => fetch("/api/strategy/positions").then(r => r.json()),
    staleTime: 30_000,
  });

  const { data: orderbook = FB_ORDERS } = useQuery<OrderBookRow[]>({
    queryKey:        ["strategy-orderbook"],
    queryFn:         () => fetch("/api/strategy/orderbook").then(r => r.json()),
    staleTime:       1_000,
    refetchInterval: 2_000,
  });

  const { data: suggestions = [] } = useQuery<HedgeSuggestion[]>({
    queryKey:        ["strategy-hedge-suggestions"],
    queryFn:         () => fetch("/api/strategy/hedge-suggestions").then(r => r.json()),
    staleTime:       15_000,
    refetchInterval: 30_000,
  });

  const rollMutation = useMutation({
    mutationFn: (sid: string) => postJson("/api/strategy/roll", { strategy_id: sid }),
    onSuccess:  (_, sid) => flash(`[${sid}] Roll order submitted`),
  });
  const hedgeMutation = useMutation({
    mutationFn: (sid: string) => postJson("/api/strategy/hedge", { strategy_id: sid, target_delta: 0 }),
    onSuccess:  (_, sid) => flash(`[${sid}] Delta hedge submitted`),
  });
  const liquidateMutation = useMutation({
    mutationFn: (sid: string) => postJson("/api/strategy/liquidate", { strategy_id: sid }),
    onSuccess:  (_, sid) => flash(`[${sid}] Liquidation order submitted`),
  });
  const executeMutation = useMutation({
    mutationFn: (body: { action: string; strategy_id: string }) =>
      postJson("/api/strategy/execute-hedge", body),
    onSuccess: (_, v) => flash(`Executed: ${v.action}`),
  });

  const latency = "4ms";
  const stratCount = positions.length;

  return (
    <div className="p-3 flex flex-col gap-2.5 min-h-full">
      {/* Header */}
      <div className="flex items-center justify-between pb-2 border-b border-zinc-800">
        <h1 className="text-[14px] font-semibold uppercase tracking-tight flex items-center gap-2 text-zinc-100">
          <ArrowLeftRight className="w-4 h-4 text-[#adc6ff]" />
          Strategy Execution
        </h1>
        <div className="flex items-center gap-3 font-mono text-[11px]">
          {actionMsg && (
            <span className="text-emerald-400 animate-pulse">&gt; {actionMsg}</span>
          )}
          <span className="text-zinc-500">
            LATENCY: <span className="text-emerald-400">{latency}</span>
            &nbsp;·&nbsp;{stratCount} STRATEGIES
          </span>
          <span className="w-2 h-2 rounded-full bg-emerald-500 vc-blink" />
        </div>
      </div>

      {/* Strategy cards */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-2.5">
        {positions.map(p => (
          <StrategyCard
            key={p.strategy_id}
            position={p}
            onRoll={() => rollMutation.mutate(p.strategy_id)}
            onHedge={() => hedgeMutation.mutate(p.strategy_id)}
            onLiquidate={() => liquidateMutation.mutate(p.strategy_id)}
            rollPending={rollMutation.isPending}
            hedgePending={hedgeMutation.isPending}
            liquidatePending={liquidateMutation.isPending}
          />
        ))}
      </div>

      {/* Order book + hedge engine */}
      <div className="grid grid-cols-12 gap-2.5 flex-1 min-h-[320px]">
        <Panel
          className="col-span-8"
          title="L2_Order_Book_Feed"
          icon={<ListOrdered className="w-3.5 h-3.5 text-zinc-400" />}
          right={<span>Update: <span className="text-emerald-400">2ms</span></span>}
          padded={false}
        >
          <table className="w-full border-collapse font-mono">
            <thead className="bg-[#1c1b1d] text-[10px] uppercase tracking-widest">
              <tr>
                {["Time", "Bid Size", "Bid", "Ask", "Ask Size", "Spread (%)"].map((h, i) => (
                  <th
                    key={h}
                    className={`px-2.5 py-1.5 text-zinc-500 border-b border-zinc-800 ${
                      i === 2 ? "text-right text-emerald-400/70" :
                      i === 3 ? "text-right text-[#ffb4ab]/70" :
                      i === 0 ? "text-left" : "text-right"
                    }`}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="text-[12px]">
              {orderbook.map((o, i) => (
                <tr
                  key={i}
                  className={`border-b border-zinc-800/50 hover:bg-zinc-800/40 ${o.wide ? "bg-red-900/10" : ""}`}
                >
                  <td className="px-2.5 py-1.5 text-zinc-500">{o.time}</td>
                  <td className="px-2.5 py-1.5 text-right text-zinc-300">{o.bid_size}</td>
                  <td className="px-2.5 py-1.5 text-right text-emerald-400 bg-emerald-900/10">{o.bid.toFixed(2)}</td>
                  <td className="px-2.5 py-1.5 text-right text-[#ffb4ab] bg-red-900/10">{o.ask.toFixed(2)}</td>
                  <td className="px-2.5 py-1.5 text-right text-zinc-300">{o.ask_size}</td>
                  <td className={`px-2.5 py-1.5 text-right ${o.wide ? "text-[#ffb4ab] font-bold" : "text-zinc-300"}`}>
                    {o.spread_pct.toFixed(2)}%{o.wide && " ⚠"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel
          className="col-span-4"
          title="Hedge_Suggest_Engine"
          icon={<Bot className="w-3.5 h-3.5 text-[#adc6ff]" />}
        >
          <div className="flex flex-col gap-2">
            {suggestions.length === 0 && (
              <div className="text-zinc-600 font-mono text-[11px] py-4 text-center">
                No active alerts
              </div>
            )}
            {suggestions.map((s, i) => (
              <SuggestionCard
                key={i}
                suggestion={s}
                onExecute={() =>
                  executeMutation.mutate({ action: s.action, strategy_id: "strat_001" })
                }
                executing={executeMutation.isPending}
              />
            ))}

            <div className="mt-auto font-mono text-[10px] text-zinc-700 leading-tight pt-2 border-t border-zinc-800">
              &gt; ENGINE_STATUS: [OK]<br />
              &gt; ML_MODEL_CONFIDENCE: 94.2%<br />
              &gt; AWAITING_NEW_TICK_DATA...<span className="vc-blink">_</span>
            </div>
          </div>
        </Panel>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function StrategyCard({
  position, onRoll, onHedge, onLiquidate, rollPending, hedgePending, liquidatePending,
}: {
  position: StrategyPosition;
  onRoll: () => void;
  onHedge: () => void;
  onLiquidate: () => void;
  rollPending: boolean;
  hedgePending: boolean;
  liquidatePending: boolean;
}) {
  const p = position;
  const pnlPos = p.pnl_intraday_eur >= 0;
  const Icon = p.legs.length > 0 ? ArrowLeftRight : ScatterChart;

  const fields: [string, ReactNode][] = [
    ["Strategy Name",    p.strategy_label],
    ["Target Strike (K)", p.target_strike],
    ["Expiry",           p.expiry],
    ["Open Interest",    p.open_interest.toLocaleString()],
    ["Allocated Margin", `€${(p.allocated_margin_eur / 1e6).toFixed(1)}M (${p.allocated_margin_pct}%)`],
    ["PnL (Intraday)",   <span className={pnlPos ? "text-emerald-400" : "text-[#ffb4ab]"}>{fmtEur(p.pnl_intraday_eur)}</span>],
  ];

  return (
    <div className="border border-zinc-800 bg-[#09090b] flex flex-col">
      <div className="flex justify-between items-center px-2.5 py-1.5 border-b border-zinc-800 bg-[#1c1b1d]">
        <div className="flex items-center gap-2">
          <Icon className="w-4 h-4 text-[#adc6ff]" />
          <h2 className="text-[13px] font-semibold text-zinc-100">{p.strategy_name}</h2>
        </div>
        <div className="flex items-center gap-2">
          {p.legs.length > 0 && p.legs.map(leg => (
            <span key={leg} className="font-mono text-[9px] px-1.5 py-[1px] bg-[#adc6ff]/10 border border-[#adc6ff]/30 text-[#adc6ff]">
              {leg}
            </span>
          ))}
          {p.live_exec && (
            <StatusPill tone="ok">
              <span className="inline-flex items-center gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 vc-blink" />LIVE_EXEC
              </span>
            </StatusPill>
          )}
        </div>
      </div>

      <div className="p-2.5 grid grid-cols-3 gap-3 font-mono text-[12px]">
        {fields.map(([label, value]) => (
          <div key={label} className="flex flex-col">
            <span className="text-[10px] font-bold tracking-widest uppercase text-zinc-500 mb-0.5">{label}</span>
            <span className="text-zinc-100">{value}</span>
          </div>
        ))}
      </div>

      <div className="mt-auto border-t border-zinc-800 bg-[#0e0e10] p-2 flex gap-2">
        <button
          onClick={onRoll}
          disabled={rollPending}
          className="flex-1 px-2 py-1.5 text-[10px] font-bold tracking-widest uppercase border border-zinc-800 bg-[#2a2a2c] hover:border-[#adc6ff] disabled:opacity-50"
        >
          {rollPending ? "ROLLING…" : "[1-Click Roll Position]"}
        </button>
        <button
          onClick={onHedge}
          disabled={hedgePending}
          className="flex-1 px-2 py-1.5 text-[10px] font-bold tracking-widest uppercase bg-[#adc6ff] text-[#002e6a] hover:brightness-110 shadow-[0_0_10px_rgba(173,198,255,0.2)] disabled:opacity-50"
        >
          {hedgePending ? "HEDGING…" : "[Auto-Hedge Delta]"}
        </button>
        <button
          onClick={onLiquidate}
          disabled={liquidatePending}
          className="flex-1 px-2 py-1.5 text-[10px] font-bold tracking-widest uppercase bg-red-500/15 border border-[#ffb4ab]/40 text-[#ffb4ab] hover:bg-red-500/25 disabled:opacity-50"
        >
          {liquidatePending ? "CLOSING…" : "[Liquidate/Close]"}
        </button>
      </div>
    </div>
  );
}

function SuggestionCard({
  suggestion, onExecute, executing,
}: {
  suggestion: HedgeSuggestion;
  onExecute: () => void;
  executing: boolean;
}) {
  const isDelta = suggestion.type === "DELTA_IMBALANCE";
  return (
    <div className="border border-zinc-800 bg-[#1c1b1d] p-2 hover:border-[#adc6ff] cursor-pointer">
      <div className="flex justify-between items-start">
        <span className={`text-[10px] font-bold tracking-widest uppercase flex items-center gap-1 ${isDelta ? "text-[#adc6ff]" : "text-emerald-400"}`}>
          {isDelta ? <AlertTriangle className="w-3 h-3" /> : <Info className="w-3 h-3" />}
          {isDelta ? "Delta Imbalance Detected" : "Vega Roll Opportunity"}
        </span>
        <span className="font-mono text-[10px] text-zinc-500">{suggestion.age_display}</span>
      </div>
      <p className="text-[12px] text-zinc-300 mt-1">{suggestion.message}</p>
      <div className="flex items-center gap-2 mt-2">
        <span className="px-1.5 py-[1px] font-mono text-[11px] text-zinc-200 bg-[#2a2a2c] border border-zinc-800">
          Action: {suggestion.action}
        </span>
        {isDelta ? (
          <button
            onClick={onExecute}
            disabled={executing}
            className="ml-auto px-2 py-[2px] text-[10px] font-bold tracking-widest uppercase bg-[#adc6ff] text-[#002e6a] hover:brightness-110 flex items-center gap-1 disabled:opacity-50"
          >
            <Zap className="w-3 h-3" />
            {executing ? "…" : "Execute"}
          </button>
        ) : (
          <button className="mt-0 ml-auto px-2 py-[2px] text-[10px] font-bold tracking-widest uppercase bg-[#2a2a2c] border border-zinc-800 hover:bg-zinc-800">
            Review Matrix
          </button>
        )}
      </div>
    </div>
  );
}
