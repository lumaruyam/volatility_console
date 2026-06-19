import type { ReactNode } from "react";
import { useState, useMemo } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import {
  ArrowLeftRight, ScatterChart, ListOrdered, Bot, AlertTriangle,
  Info, Zap, Plus, X, Filter, ChevronDown, BarChart3, Clock,
} from "lucide-react";
import { Panel, StatusPill } from "../ui";

// ---------------------------------------------------------------------------
// API types
// ---------------------------------------------------------------------------

type StrategyPosition = {
  strategy_id: string;
  strategy_name: string;
  strategy_label: string;
  strategy_type?: string;
  status?: string;
  target_strike: string;
  expiry: string;
  days_to_expiry?: number;
  open_interest: number;
  allocated_margin_eur: number;
  allocated_margin_pct: number;
  pnl_intraday_eur: number;
  live_exec: boolean;
  legs: string[];
  total_delta?: number;
  total_vega?: number;
  constituent_strikes?: { ticker: string; strike: number }[];
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
  strategy_id?: string;
};

type LatencyData = { latency_ms: number };

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const STRATEGY_TYPES = ["ALL", "STRADDLE", "DISPERSION", "CALENDAR", "BUTTERFLY"] as const;
const STATUS_OPTIONS  = ["ALL", "OPEN", "CLOSED", "ROLLED", "PENDING"] as const;
const MATURITY_LABELS = [
  ["ALL",    "All Maturities"],
  ["LT3M",   "< 3M"],
  ["3TO6M",  "3–6M"],
  ["6TO12M", "6–12M"],
  ["GT12M",  "> 12M"],
] as const;

const UNDERLYINGS  = ["SX5E", "ASML", "MC.PA", "SAP", "TTE", "SIE", "OR.PA"];
const INSTRUMENTS  = ["Call", "Put", "Future", "Stock"];
const ORDER_TYPES  = ["MARKET", "LIMIT"] as const;
const DIRECTIONS   = ["BUY", "SELL"] as const;

// ---------------------------------------------------------------------------
// Fallback data
// ---------------------------------------------------------------------------

function makeFbOrders(): OrderBookRow[] {
  const now = new Date();
  const t = (offset: number) =>
    new Date(now.getTime() - offset * 1000)
      .toLocaleTimeString("en-GB", { hour12: false });
  return [
    { time: t(4), bid_size: 150, bid: 4201.50, ask: 4203.00, ask_size: 200, spread_pct: 0.03,  wide: false },
    { time: t(3), bid_size:  50, bid:   15.20, ask:   16.10, ask_size:  10, spread_pct: 5.92,  wide: true  },
    { time: t(3), bid_size: 500, bid: 4199.00, ask: 4205.50, ask_size: 450, spread_pct: 0.15,  wide: false },
    { time: t(1), bid_size:  12, bid:    0.85, ask:    0.95, ask_size: 100, spread_pct: 11.76, wide: true  },
    { time: t(0), bid_size: 300, bid: 4200.00, ask: 4204.00, ask_size: 300, spread_pct: 0.09,  wide: false },
  ];
}

const FB_POSITIONS: StrategyPosition[] = [
  {
    strategy_id: "strat_001",
    strategy_name: "SX5E 12-Month Straddle",
    strategy_label: "ALPHA_CORE_V1",
    strategy_type: "STRADDLE",
    status: "OPEN",
    target_strike: "4200 / 4200",
    expiry: "12M (Dec 26)",
    days_to_expiry: 184,
    open_interest: 1450,
    allocated_margin_eur: 2_400_000,
    allocated_margin_pct: 14.2,
    pnl_intraday_eur: 12_450,
    live_exec: true,
    legs: ["Call 4200 DEC26", "Put 4200 DEC26"],
    total_delta: 0.42,
    total_vega: 4_250,
  },
  {
    strategy_id: "strat_002",
    strategy_name: "Dispersion Basket",
    strategy_label: "VOL_ARB_Q3",
    strategy_type: "DISPERSION",
    status: "OPEN",
    target_strike: "SX5E 4200 (index anchor)",
    expiry: "3M (Sep 26)",
    days_to_expiry: 85,
    open_interest: 8200,
    allocated_margin_eur: 5_100_000,
    allocated_margin_pct: 22.5,
    pnl_intraday_eur: -3_210,
    live_exec: true,
    legs: [],
    total_delta: 0.10,
    total_vega: 3_204,
    constituent_strikes: [
      { ticker: "ASML",  strike: 900 },
      { ticker: "MC.PA", strike: 500 },
      { ticker: "SAP",   strike: 140 },
      { ticker: "SIE",   strike: 270 },
    ],
  },
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

function fmtGreek(v: number): string {
  const abs = Math.abs(v);
  if (abs >= 1_000) return (v / 1_000).toFixed(1) + "K";
  return v.toFixed(2);
}

const apiFetch = (url: string) =>
  fetch(url).then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); });

function postJson(url: string, body: object) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); });
}

function maturityBucket(days?: number): string {
  if (days == null) return "ALL";
  if (days < 90)  return "LT3M";
  if (days < 180) return "3TO6M";
  if (days < 365) return "6TO12M";
  return "GT12M";
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function StrategyExecution() {
  const [actionMsg, setActionMsg]           = useState<string | null>(null);
  const [selectedStrategyId, setSelected]   = useState<string>("strat_001");
  const [showOrderTicket, setShowTicket]    = useState(false);
  const [typeFilter, setTypeFilter]         = useState<string>("ALL");
  const [statusFilter, setStatusFilter]     = useState<string>("ALL");
  const [maturityFilter, setMaturityFilter] = useState<string>("ALL");

  // Order ticket form state
  const [orderUnderlying, setOrderUnderlying] = useState("SX5E");
  const [orderInstrument, setOrderInstrument] = useState("Call");
  const [orderDirection, setOrderDirection]   = useState<"BUY" | "SELL">("BUY");
  const [orderQty, setOrderQty]               = useState("100");
  const [orderStrike, setOrderStrike]         = useState("");
  const [orderExpiry, setOrderExpiry]         = useState("");
  const [orderType, setOrderType]             = useState<"MARKET" | "LIMIT">("LIMIT");
  const [orderLimitPrice, setOrderLimitPrice] = useState("");

  const flash = (msg: string) => {
    setActionMsg(msg);
    setTimeout(() => setActionMsg(null), 4000);
  };

  // ── Queries ───────────────────────────────────────────────────────────────
  const { data: latencyData } = useQuery<LatencyData>({
    queryKey:        ["strategy-latency"],
    queryFn:         () => apiFetch("/api/strategy/latency"),
    staleTime:       5_000,
    refetchInterval: 10_000,
  });

  const { data: positions = FB_POSITIONS } = useQuery<StrategyPosition[]>({
    queryKey:        ["strategy-positions"],
    queryFn:         () => apiFetch("/api/strategy/positions"),
    staleTime:       30_000,
    refetchInterval: 30_000,
  });

  const { data: orderbook = makeFbOrders() } = useQuery<OrderBookRow[]>({
    queryKey:        ["strategy-orderbook"],
    queryFn:         () => apiFetch("/api/strategy/orderbook"),
    staleTime:       1_000,
    refetchInterval: 2_000,
  });

  const { data: suggestions = [] } = useQuery<HedgeSuggestion[]>({
    queryKey:        ["strategy-hedge-suggestions"],
    queryFn:         () => apiFetch("/api/strategy/hedge-suggestions"),
    staleTime:       15_000,
    refetchInterval: 30_000,
  });

  // ── Mutations ─────────────────────────────────────────────────────────────
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
  const orderMutation = useMutation({
    mutationFn: (body: object) => postJson("/api/strategy/order", body),
    onSuccess: () => {
      flash("Order submitted to IBKR");
      setShowTicket(false);
    },
  });

  // ── Derived ───────────────────────────────────────────────────────────────
  const latency     = latencyData?.latency_ms != null ? `${latencyData.latency_ms}ms` : "—";
  const stratCount  = positions.length;

  const filteredPositions = useMemo(() => positions.filter(p => {
    if (typeFilter !== "ALL" && (p.strategy_type ?? "STRADDLE") !== typeFilter) return false;
    if (statusFilter !== "ALL" && (p.status ?? "OPEN") !== statusFilter) return false;
    if (maturityFilter !== "ALL" && maturityBucket(p.days_to_expiry) !== maturityFilter) return false;
    return true;
  }), [positions, typeFilter, statusFilter, maturityFilter]);

  // Portfolio-level Greeks summary
  const totalDelta     = positions.reduce((s, p) => s + (p.total_delta ?? 0), 0);
  const totalVega      = positions.reduce((s, p) => s + (p.total_vega  ?? 0), 0);
  const totalMarginPct = positions.reduce((s, p) => s + p.allocated_margin_pct, 0);

  const handleOrderSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    orderMutation.mutate({
      underlying:   orderUnderlying,
      instrument:   orderInstrument,
      direction:    orderDirection,
      quantity:     parseInt(orderQty, 10),
      strike:       orderStrike ? parseFloat(orderStrike) : undefined,
      expiry:       orderExpiry || undefined,
      order_type:   orderType,
      limit_price:  orderType === "LIMIT" && orderLimitPrice ? parseFloat(orderLimitPrice) : undefined,
      destination:  "IBKR",
    });
  };

  return (
    <div className="p-3 flex flex-col gap-2.5 h-full min-h-0 overflow-x-hidden overflow-y-auto vc-scroll">

      {/* ── Header ─────────────────────────────────────────────────────────── */}
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
            LATENCY: <span className={latencyData ? "text-emerald-400" : "text-zinc-600"}>{latency}</span>
            &nbsp;·&nbsp;{stratCount} STRATEGIES
          </span>
          {/* NEW ORDER button */}
          <button
            onClick={() => setShowTicket(v => !v)}
            className={`flex items-center gap-1.5 px-2.5 py-1 font-bold tracking-widest uppercase text-[10px] border transition-colors ${
              showOrderTicket
                ? "border-[#ffb4ab]/50 bg-[#ffb4ab]/10 text-[#ffb4ab]"
                : "border-[#adc6ff]/50 bg-[#adc6ff]/10 text-[#adc6ff] hover:bg-[#adc6ff]/20"
            }`}
          >
            {showOrderTicket ? <X className="w-3 h-3" /> : <Plus className="w-3 h-3" />}
            {showOrderTicket ? "CLOSE TICKET" : "NEW ORDER"}
          </button>
          <span className="w-2 h-2 rounded-full bg-emerald-500 vc-blink" />
        </div>
      </div>

      {/* ── Order Ticket (collapsible) ─────────────────────────────────────── */}
      {showOrderTicket && (
        <div className="border border-[#adc6ff]/30 bg-[#adc6ff]/5 p-3">
          <div className="flex items-center gap-2 mb-2.5">
            <Plus className="w-3.5 h-3.5 text-[#adc6ff]" />
            <span className="text-[11px] font-bold tracking-[0.14em] uppercase text-[#adc6ff]">Order Ticket</span>
            <span className="font-mono text-[9px] text-zinc-600 ml-auto">→ IBKR</span>
          </div>
          <form onSubmit={handleOrderSubmit}>
            <div className="grid grid-cols-8 gap-2 items-end">
              {/* Direction */}
              <div className="col-span-1 flex flex-col gap-1">
                <span className="text-[9px] font-bold tracking-widest uppercase text-zinc-500">Direction</span>
                <div className="flex">
                  {DIRECTIONS.map(d => (
                    <button
                      key={d} type="button"
                      onClick={() => setOrderDirection(d)}
                      className={`flex-1 py-1.5 text-[10px] font-bold border transition-colors ${
                        orderDirection === d
                          ? d === "BUY"
                            ? "bg-emerald-500/20 border-emerald-500/50 text-emerald-400"
                            : "bg-red-500/20 border-[#ffb4ab]/50 text-[#ffb4ab]"
                          : "border-zinc-700 text-zinc-500 hover:text-zinc-300"
                      }`}
                    >{d}</button>
                  ))}
                </div>
              </div>

              {/* Underlying */}
              <TicketField label="Underlying">
                <select value={orderUnderlying} onChange={e => setOrderUnderlying(e.target.value)}
                  className="w-full bg-[#131315] border border-zinc-700 text-zinc-200 font-mono text-[11px] px-1.5 py-1.5 focus:outline-none focus:border-[#adc6ff]">
                  {UNDERLYINGS.map(u => <option key={u} value={u}>{u}</option>)}
                </select>
              </TicketField>

              {/* Instrument */}
              <TicketField label="Instrument">
                <select value={orderInstrument} onChange={e => setOrderInstrument(e.target.value)}
                  className="w-full bg-[#131315] border border-zinc-700 text-zinc-200 font-mono text-[11px] px-1.5 py-1.5 focus:outline-none focus:border-[#adc6ff]">
                  {INSTRUMENTS.map(i => <option key={i} value={i}>{i}</option>)}
                </select>
              </TicketField>

              {/* Strike */}
              <TicketField label="Strike">
                <input type="number" value={orderStrike} onChange={e => setOrderStrike(e.target.value)}
                  placeholder="e.g. 4200"
                  className="w-full bg-[#131315] border border-zinc-700 text-zinc-200 font-mono text-[11px] px-1.5 py-1.5 focus:outline-none focus:border-[#adc6ff]" />
              </TicketField>

              {/* Expiry */}
              <TicketField label="Expiry">
                <input type="text" value={orderExpiry} onChange={e => setOrderExpiry(e.target.value)}
                  placeholder="YYYY-MM-DD"
                  className="w-full bg-[#131315] border border-zinc-700 text-zinc-200 font-mono text-[11px] px-1.5 py-1.5 focus:outline-none focus:border-[#adc6ff]" />
              </TicketField>

              {/* Quantity */}
              <TicketField label="Quantity">
                <input type="number" value={orderQty} onChange={e => setOrderQty(e.target.value)}
                  min="1"
                  className="w-full bg-[#131315] border border-zinc-700 text-zinc-200 font-mono text-[11px] px-1.5 py-1.5 focus:outline-none focus:border-[#adc6ff]" />
              </TicketField>

              {/* Order type + limit price */}
              <div className="col-span-1 flex flex-col gap-1">
                <span className="text-[9px] font-bold tracking-widest uppercase text-zinc-500">Order Type</span>
                <div className="flex mb-1">
                  {ORDER_TYPES.map(ot => (
                    <button
                      key={ot} type="button"
                      onClick={() => setOrderType(ot)}
                      className={`flex-1 py-1.5 text-[10px] font-bold border transition-colors ${
                        orderType === ot
                          ? "bg-[#adc6ff]/10 border-[#adc6ff]/50 text-[#adc6ff]"
                          : "border-zinc-700 text-zinc-500 hover:text-zinc-300"
                      }`}
                    >{ot}</button>
                  ))}
                </div>
                {orderType === "LIMIT" && (
                  <input type="number" value={orderLimitPrice} onChange={e => setOrderLimitPrice(e.target.value)}
                    placeholder="Limit price"
                    className="w-full bg-[#131315] border border-zinc-700 text-zinc-200 font-mono text-[11px] px-1.5 py-1 focus:outline-none focus:border-[#adc6ff]" />
                )}
              </div>

              {/* Submit */}
              <div className="col-span-1 flex flex-col justify-end">
                <button
                  type="submit"
                  disabled={orderMutation.isPending}
                  className={`w-full py-2 text-[10px] font-bold tracking-widest uppercase transition-colors disabled:opacity-50 ${
                    orderDirection === "BUY"
                      ? "bg-emerald-500/80 hover:bg-emerald-500 text-black"
                      : "bg-[#ffb4ab]/80 hover:bg-[#ffb4ab] text-black"
                  }`}
                >
                  {orderMutation.isPending ? "SENDING…" : `${orderDirection} → IBKR`}
                </button>
              </div>
            </div>
          </form>
        </div>
      )}

      {/* ── Portfolio Greeks banner ─────────────────────────────────────────── */}
      <div className="grid grid-cols-4 gap-2 shrink-0">
        <BannerKPI label="NET Δ (Portfolio)"    value={totalDelta >= 0 ? "+" + totalDelta.toFixed(2) : totalDelta.toFixed(2)} valueClass={totalDelta >= 0 ? "text-emerald-400" : "text-[#ffb4ab]"} Icon={BarChart3} />
        <BannerKPI label="NET VEGA (Portfolio)" value={fmtGreek(totalVega)}                                                   valueClass="text-[#adc6ff]"  Icon={BarChart3} />
        <BannerKPI label="STRATEGIES ACTIVE"   value={String(positions.filter(p => (p.status ?? "OPEN") === "OPEN").length)} valueClass="text-zinc-200"   Icon={ArrowLeftRight} />
        <BannerKPI label="TOTAL MARGIN USED"   value={totalMarginPct.toFixed(1) + "%"}                                       valueClass={totalMarginPct > 50 ? "text-yellow-400" : "text-zinc-200"} Icon={Filter} />
      </div>

      {/* ── Filter bar ────────────────────────────────────────────────────── */}
      <div className="shrink-0 flex items-center gap-2 flex-wrap px-2.5 py-1.5 border border-zinc-800 bg-[#0e0e10]">
        <span className="text-[9px] font-bold tracking-widest uppercase text-zinc-600">TYPE:</span>
        {STRATEGY_TYPES.map(t => (
          <FilterChip key={t} label={t} active={typeFilter === t} onClick={() => setTypeFilter(t)} />
        ))}
        <div className="w-px h-3 bg-zinc-800 mx-0.5" />
        <span className="text-[9px] font-bold tracking-widest uppercase text-zinc-600">STATUS:</span>
        {STATUS_OPTIONS.map(s => (
          <FilterChip key={s} label={s} active={statusFilter === s} onClick={() => setStatusFilter(s)} />
        ))}
        <div className="w-px h-3 bg-zinc-800 mx-0.5" />
        <span className="text-[9px] font-bold tracking-widest uppercase text-zinc-600">MATURITY:</span>
        {MATURITY_LABELS.map(([val, label]) => (
          <FilterChip key={val} label={label} active={maturityFilter === val} onClick={() => setMaturityFilter(val)} />
        ))}
        <span className="ml-auto font-mono text-[9px] text-zinc-700">
          {filteredPositions.length}/{positions.length} shown
        </span>
      </div>

      {/* ── Strategy cards ────────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-2.5">
        {filteredPositions.map(p => (
          <StrategyCard
            key={p.strategy_id}
            position={p}
            selected={selectedStrategyId === p.strategy_id}
            onSelect={() => setSelected(p.strategy_id)}
            onRoll={() => rollMutation.mutate(p.strategy_id)}
            onHedge={() => hedgeMutation.mutate(p.strategy_id)}
            onLiquidate={() => liquidateMutation.mutate(p.strategy_id)}
            rollPending={rollMutation.isPending}
            hedgePending={hedgeMutation.isPending}
            liquidatePending={liquidateMutation.isPending}
          />
        ))}
        {filteredPositions.length === 0 && (
          <div className="col-span-2 flex items-center justify-center h-24 border border-zinc-800 text-zinc-600 font-mono text-[11px]">
            No strategies match the current filters
          </div>
        )}
      </div>

      {/* ── Order book + hedge engine ─────────────────────────────────────── */}
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
          {/* Wide spread legend */}
          <div className="px-2.5 py-1 border-t border-zinc-800 flex items-center gap-1 font-mono text-[9px] text-zinc-700">
            <AlertTriangle className="w-2.5 h-2.5 text-[#ffb4ab]/50" />
            Wide spread: bid-ask &gt; 5% of mid — highlighted in red
          </div>
        </Panel>

        <Panel
          className="col-span-4"
          title="Hedge_Suggest_Engine"
          icon={<Bot className="w-3.5 h-3.5 text-[#adc6ff]" />}
        >
          <div className="flex flex-col gap-2">
            <div className="flex items-center gap-1.5 px-1.5 py-1 bg-zinc-900/60 border border-zinc-800 font-mono text-[9px] text-zinc-500">
              <span>ACTIVE STRATEGY:</span>
              <span className="text-[#adc6ff]">{selectedStrategyId}</span>
              <ChevronDown className="w-2.5 h-2.5 ml-auto" />
            </div>
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
                  executeMutation.mutate({
                    action: s.action,
                    // Use the strategy_id from the suggestion if provided, otherwise the selected one
                    strategy_id: s.strategy_id ?? selectedStrategyId,
                  })
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

function BannerKPI({
  label, value, valueClass, Icon,
}: {
  label: string;
  value: string;
  valueClass: string;
  Icon: React.ComponentType<{ className?: string }>;
}) {
  return (
    <div className="border border-zinc-800 bg-[#131315] px-2.5 py-2 flex items-center justify-between">
      <div className="flex flex-col">
        <span className="text-[9px] font-bold tracking-widest uppercase text-zinc-600">{label}</span>
        <span className={`font-mono text-[14px] mt-0.5 ${valueClass}`}>{value}</span>
      </div>
      <Icon className="w-4 h-4 text-zinc-700" />
    </div>
  );
}

function FilterChip({
  label, active, onClick,
}: {
  label: string; active: boolean; onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`px-2 py-0.5 font-mono text-[10px] font-bold border transition-colors ${
        active
          ? "border-[#adc6ff] bg-[#adc6ff]/10 text-[#adc6ff]"
          : "border-zinc-800 text-zinc-500 hover:border-zinc-700 hover:text-zinc-300"
      }`}
    >
      {label}
    </button>
  );
}

function TicketField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="col-span-1 flex flex-col gap-1">
      <span className="text-[9px] font-bold tracking-widest uppercase text-zinc-500">{label}</span>
      {children}
    </div>
  );
}

function RollBadge({ days }: { days: number }) {
  if (days > 90) return null;
  const urgent = days < 30;
  return (
    <span className={`inline-flex items-center gap-1 font-mono text-[9px] font-bold px-1.5 py-0.5 border ${
      urgent
        ? "border-red-500/50 bg-red-500/10 text-red-400"
        : "border-yellow-500/50 bg-yellow-500/10 text-yellow-400"
    }`}>
      <Clock className="w-2.5 h-2.5" />
      {urgent ? `ROLL NOW (${days}d)` : `Roll in ${days}d`}
    </span>
  );
}

function StrategyCard({
  position, selected, onSelect, onRoll, onHedge, onLiquidate,
  rollPending, hedgePending, liquidatePending,
}: {
  position: StrategyPosition;
  selected: boolean;
  onSelect: () => void;
  onRoll: () => void;
  onHedge: () => void;
  onLiquidate: () => void;
  rollPending: boolean;
  hedgePending: boolean;
  liquidatePending: boolean;
}) {
  const p = position;
  const pnlPos  = p.pnl_intraday_eur >= 0;
  const Icon    = p.legs.length > 0 ? ArrowLeftRight : ScatterChart;
  const isDisp  = p.strategy_type === "DISPERSION";

  const fields: [string, ReactNode][] = [
    ["Strategy Label",    p.strategy_label],
    ["Target Strike (K)", isDisp
      ? <span title="Index anchor for dispersion">{p.target_strike}</span>
      : p.target_strike],
    ["Expiry",           p.expiry],
    ["Open Interest",    p.open_interest.toLocaleString()],
    ["Allocated Margin", `€${(p.allocated_margin_eur / 1e6).toFixed(1)}M (${p.allocated_margin_pct}%)`],
    ["PnL (Intraday)",   <span className={pnlPos ? "text-emerald-400" : "text-[#ffb4ab]"}>{fmtEur(p.pnl_intraday_eur)}</span>],
    ["Total Δ",          p.total_delta != null
      ? <span className={p.total_delta >= 0 ? "text-emerald-400" : "text-[#ffb4ab]"}>{p.total_delta >= 0 ? "+" : ""}{p.total_delta.toFixed(2)}</span>
      : <span className="text-zinc-600">—</span>],
    ["Total Vega",       p.total_vega != null
      ? <span className="text-[#adc6ff]">{fmtGreek(p.total_vega)}</span>
      : <span className="text-zinc-600">—</span>],
  ];

  return (
    <div
      onClick={onSelect}
      className={`border bg-[#09090b] flex flex-col cursor-pointer transition-colors ${
        selected ? "border-[#adc6ff]/40" : "border-zinc-800 hover:border-zinc-700"
      }`}
    >
      {/* Card header */}
      <div className="flex justify-between items-center px-2.5 py-1.5 border-b border-zinc-800 bg-[#1c1b1d]">
        <div className="flex items-center gap-2">
          <Icon className="w-4 h-4 text-[#adc6ff]" />
          <h2 className="text-[13px] font-semibold text-zinc-100">{p.strategy_name}</h2>
          {p.days_to_expiry != null && <RollBadge days={p.days_to_expiry} />}
        </div>
        <div className="flex items-center gap-2">
          {p.strategy_type && (
            <span className="font-mono text-[9px] px-1.5 py-[1px] bg-zinc-800 text-zinc-400 border border-zinc-700">
              {p.strategy_type}
            </span>
          )}
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

      {/* Fields grid */}
      <div className="p-2.5 grid grid-cols-4 gap-3 font-mono text-[12px]">
        {fields.map(([label, value]) => (
          <div key={label} className="flex flex-col">
            <span className="text-[10px] font-bold tracking-widest uppercase text-zinc-500 mb-0.5">{label}</span>
            <span className="text-zinc-100">{value}</span>
          </div>
        ))}
      </div>

      {/* Dispersion constituents mini-table */}
      {isDisp && p.constituent_strikes && p.constituent_strikes.length > 0 && (
        <div className="mx-2.5 mb-2 border border-zinc-800/60 bg-[#0e0e10]">
          <div className="px-2 py-1 text-[9px] font-bold tracking-widest uppercase text-zinc-600 border-b border-zinc-800">
            Basket Constituents
          </div>
          <div className="grid font-mono text-[10px] p-1 gap-px"
            style={{ gridTemplateColumns: `repeat(${Math.min(p.constituent_strikes.length, 4)}, 1fr)` }}>
            {p.constituent_strikes.map(cs => (
              <div key={cs.ticker} className="flex flex-col items-center py-1 border border-zinc-800/40 bg-[#131315]">
                <span className="text-[#adc6ff] font-bold">{cs.ticker}</span>
                <span className="text-zinc-400">{cs.strike.toLocaleString()}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Action buttons */}
      <div className="mt-auto border-t border-zinc-800 bg-[#0e0e10] p-2 flex gap-2" onClick={e => e.stopPropagation()}>
        <button
          onClick={onRoll}
          disabled={rollPending}
          className="flex-1 px-2 py-1.5 text-[10px] font-bold tracking-widest uppercase border border-zinc-800 bg-[#2a2a2c] hover:border-[#adc6ff] disabled:opacity-50"
        >
          {rollPending ? "ROLLING…" : "[1-Click Roll]"}
        </button>
        <button
          onClick={onHedge}
          disabled={hedgePending}
          className="flex-1 px-2 py-1.5 text-[10px] font-bold tracking-widest uppercase bg-[#adc6ff] text-[#002e6a] hover:brightness-110 shadow-[0_0_10px_rgba(173,198,255,0.2)] disabled:opacity-50"
        >
          {hedgePending ? "HEDGING…" : "[Auto-Hedge Δ]"}
        </button>
        <button
          onClick={onLiquidate}
          disabled={liquidatePending}
          className="flex-1 px-2 py-1.5 text-[10px] font-bold tracking-widest uppercase bg-red-500/15 border border-[#ffb4ab]/40 text-[#ffb4ab] hover:bg-red-500/25 disabled:opacity-50"
        >
          {liquidatePending ? "CLOSING…" : "[Close]"}
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
