import { useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ClipboardList, AlertTriangle, RefreshCw } from "lucide-react";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type OrderStatus = "staged" | "submitted" | "partial" | "filled" | "cancelled" | "rejected";

type Order = {
  order_id: string;
  status: OrderStatus;
  side: "BUY" | "SELL";
  qty: number;
  underlying: string;
  expiry: string;       // YYYYMMDD
  strike: number;
  right: "C" | "P";
  order_type: "LMT" | "MKT";
  limit_price?: number;
  filled_qty: number;
  fill_price?: number;
  reason?: string;
};

// ---------------------------------------------------------------------------
// Fallback seed data (shown before first API response and on API error)
// ---------------------------------------------------------------------------

const SEED: Order[] = [
  { order_id: "ord_a1b2c3d4e5f60001", status: "filled",    side: "BUY",  qty: 50,  underlying: "SX5E",  expiry: "20260717", strike: 4200, right: "C", order_type: "LMT", limit_price: 82.40,  filled_qty: 50,  fill_price: 82.35 },
  { order_id: "ord_a1b2c3d4e5f60002", status: "submitted", side: "SELL", qty: 25,  underlying: "SX5E",  expiry: "20260717", strike: 4400, right: "C", order_type: "LMT", limit_price: 41.10,  filled_qty: 0 },
  { order_id: "ord_a1b2c3d4e5f60003", status: "partial",   side: "BUY",  qty: 100, underlying: "ASML",  expiry: "20261215", strike: 900,  right: "P", order_type: "LMT", limit_price: 54.25,  filled_qty: 35,  fill_price: 54.20 },
  { order_id: "ord_a1b2c3d4e5f60004", status: "staged",    side: "SELL", qty: 75,  underlying: "MC.PA", expiry: "20260919", strike: 510,  right: "P", order_type: "LMT", limit_price: 9.85,   filled_qty: 0 },
  { order_id: "ord_a1b2c3d4e5f60005", status: "rejected",  side: "BUY",  qty: 40,  underlying: "ASML",  expiry: "20260821", strike: 1000, right: "C", order_type: "MKT", filled_qty: 0, reason: "Insufficient buying power — margin check failed" },
  { order_id: "ord_a1b2c3d4e5f60006", status: "filled",    side: "SELL", qty: 30,  underlying: "SX5E",  expiry: "20260619", strike: 4000, right: "P", order_type: "LMT", limit_price: 12.80,  filled_qty: 30,  fill_price: 12.92 },
  { order_id: "ord_a1b2c3d4e5f60007", status: "cancelled", side: "BUY",  qty: 60,  underlying: "SAP",   expiry: "20261016", strike: 140,  right: "C", order_type: "LMT", limit_price: 7.55,   filled_qty: 0, reason: "User cancelled before fill" },
  { order_id: "ord_a1b2c3d4e5f60008", status: "submitted", side: "BUY",  qty: 200, underlying: "SX5E",  expiry: "20260918", strike: 4100, right: "C", order_type: "LMT", limit_price: 14.20,  filled_qty: 0 },
  { order_id: "ord_a1b2c3d4e5f60009", status: "staged",    side: "SELL", qty: 15,  underlying: "TTE",   expiry: "20261218", strike: 60,   right: "P", order_type: "LMT", limit_price: 11.05,  filled_qty: 0 },
  { order_id: "ord_a1b2c3d4e5f60010", status: "rejected",  side: "SELL", qty: 80,  underlying: "SX5E",  expiry: "20260717", strike: 4600, right: "C", order_type: "LMT", limit_price: 28.60,  filled_qty: 0, reason: "Price outside NBBO tolerance band" },
  { order_id: "ord_a1b2c3d4e5f60011", status: "filled",    side: "BUY",  qty: 45,  underlying: "SIE",   expiry: "20260918", strike: 270,  right: "C", order_type: "MKT", filled_qty: 45, fill_price: 6.18 },
  { order_id: "ord_a1b2c3d4e5f60012", status: "partial",   side: "SELL", qty: 120, underlying: "OR.PA", expiry: "20261120", strike: 220,  right: "P", order_type: "LMT", limit_price: 18.40,  filled_qty: 70,  fill_price: 18.42 },
];

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const FILTERS = ["ALL", "STAGED", "WORKING", "FILLED", "REJECTED"] as const;
type Filter = typeof FILTERS[number];

const STATUS_BADGE: Record<OrderStatus, string> = {
  staged:    "bg-zinc-600 text-zinc-200",
  submitted: "bg-blue-500/20 text-blue-300 border border-blue-500/40",
  partial:   "bg-yellow-500/20 text-yellow-300",
  filled:    "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40",
  cancelled: "bg-zinc-700 text-zinc-400",
  rejected:  "bg-red-500/20 text-red-400 border border-red-500/40",
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function Orders() {
  const qc = useQueryClient();
  const [filter, setFilter]         = useState<Filter>("ALL");
  const [cancellingId, setCancelId] = useState<string | null>(null);
  const [secsAgo, setSecsAgo]       = useState(0);

  // ── Query: blotter (polls every 10 s) ─────────────────────────────────────
  const {
    data: orders = SEED,
    isError,
    isFetching,
    dataUpdatedAt,
  } = useQuery<Order[]>({
    queryKey:        ["orders-blotter"],
    queryFn:         () =>
      fetch("/api/orders/blotter").then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json().then((d: Order[] | { orders: Order[] }) =>
          Array.isArray(d) ? d : (d?.orders ?? []),
        );
      }),
    placeholderData: SEED,
    refetchInterval: 10_000,
    staleTime:       5_000,
    retry:           2,
  });

  // ── Mutation: cancel order ─────────────────────────────────────────────────
  const cancelMutation = useMutation({
    mutationFn: (orderId: string) => {
      setCancelId(orderId);
      return fetch(`/api/orders/${orderId}/cancel`, { method: "POST" }).then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      });
    },
    onSettled: () => setCancelId(null),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["orders-blotter"] }),
  });

  // ── "Xs ago" refresh counter ───────────────────────────────────────────────
  useEffect(() => {
    if (!dataUpdatedAt) return;
    setSecsAgo(0);
    const id = setInterval(() => setSecsAgo(s => s + 1), 1_000);
    return () => clearInterval(id);
  }, [dataUpdatedAt]);

  // ── Dev-mode OMS sanity assertions ───────────────────────────────────────
  if (process.env.NODE_ENV === "development") {
    orders.forEach(o => {
      if (o.order_type === "LMT" && o.fill_price != null && o.limit_price != null) {
        if (o.side === "SELL" && o.fill_price < o.limit_price) {
          console.warn(`[OMS] SELL fill below limit: ${o.order_id} fill=${o.fill_price} < limit=${o.limit_price}`);
        }
        if (o.side === "BUY" && o.fill_price > o.limit_price) {
          console.warn(`[OMS] BUY fill above limit: ${o.order_id} fill=${o.fill_price} > limit=${o.limit_price}`);
        }
      }
    });
  }

  // ── Derived ───────────────────────────────────────────────────────────────
  const kpis = useMemo(() => ({
    total:    orders.length,
    staged:   orders.filter(o => o.status === "staged").length,
    working:  orders.filter(o => o.status === "submitted" || o.status === "partial").length,
    filled:   orders.filter(o => o.status === "filled").length,
    rejected: orders.filter(o => o.status === "rejected" || o.status === "cancelled").length,
  }), [orders]);

  const rows = useMemo(() => {
    switch (filter) {
      case "STAGED":   return orders.filter(o => o.status === "staged");
      case "WORKING":  return orders.filter(o => o.status === "submitted" || o.status === "partial");
      case "FILLED":   return orders.filter(o => o.status === "filled");
      case "REJECTED": return orders.filter(o => o.status === "rejected" || o.status === "cancelled");
      default:         return orders;
    }
  }, [orders, filter]);

  const refreshLabel = secsAgo < 3 ? "LIVE" : `${secsAgo}s ago`;

  return (
    <div className="p-3 font-mono text-zinc-200 bg-[#09090b] min-h-full flex flex-col gap-3">

      {/* ── Header ────────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-2 pb-2 border-b border-zinc-800">
        <ClipboardList className="w-4 h-4 text-[#adc6ff]" />
        <h1 className="text-[13px] font-bold tracking-[0.22em] text-[#adc6ff]">
          ORDER MANAGEMENT SYSTEM // BLOTTER
        </h1>
        <div className="ml-auto flex items-center gap-3 text-[10px] text-zinc-500 tracking-wider">
          {isFetching && (
            <RefreshCw className="w-3 h-3 text-[#adc6ff] animate-spin" />
          )}
          <span>
            CHANNEL: OMS-PRIMARY ·{" "}
            <span className={secsAgo < 3 ? "text-emerald-400" : "text-zinc-500"}>
              {refreshLabel}
            </span>
          </span>
          <span className={`w-2 h-2 rounded-full ${isFetching ? "bg-[#adc6ff] animate-pulse" : "bg-emerald-500"}`} />
        </div>
      </div>

      {/* ── Error banner ──────────────────────────────────────────────────── */}
      {isError && (
        <div className="flex items-center gap-2 px-3 py-2 border border-[#ffb4ab]/40 bg-red-900/10 text-[10px] text-[#ffb4ab]">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
          API ERROR — showing cached / seed data · will retry automatically
        </div>
      )}

      {/* ── KPI strip ─────────────────────────────────────────────────────── */}
      <div className="grid grid-cols-5 gap-2 shrink-0">
        <KpiTile label="TOTAL ORDERS" value={kpis.total}    tone="default" />
        <KpiTile label="STAGED"       value={kpis.staged}   tone="muted"   />
        <KpiTile label="WORKING"      value={kpis.working}  tone="blue"    />
        <KpiTile label="FILLED"       value={kpis.filled}   tone="emerald" />
        <KpiTile label="REJ / CNCL"   value={kpis.rejected} tone="red"     />
      </div>

      {/* ── Filter bar ────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-1 border border-zinc-800 bg-[#0e0e10] p-1 w-fit shrink-0">
        {FILTERS.map(f => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={[
              "px-3 py-1 text-[10px] tracking-[0.18em] transition-colors",
              filter === f
                ? "bg-[#adc6ff]/15 text-[#adc6ff] border border-[#adc6ff]/40"
                : "text-zinc-500 hover:text-zinc-200 border border-transparent",
            ].join(" ")}
          >
            [ {f} ]
          </button>
        ))}
        <span className="ml-3 text-[10px] text-zinc-600 tracking-wider">
          {rows.length} ROW{rows.length === 1 ? "" : "S"}
        </span>
      </div>

      {/* ── Blotter table ─────────────────────────────────────────────────── */}
      <div className="border border-zinc-800 bg-[#0e0e10] overflow-auto vc-scroll flex-1">
        <table className="w-full text-[11px]">
          <thead className="bg-[#131315] text-zinc-500 tracking-[0.16em] sticky top-0 z-10">
            <tr className="text-left">
              <th className="px-3 py-2 font-medium">STATUS</th>
              <th className="px-3 py-2 font-medium">ORDER ID</th>
              <th className="px-3 py-2 font-medium">SIDE</th>
              <th className="px-3 py-2 font-medium text-right">QTY</th>
              <th className="px-3 py-2 font-medium">INSTRUMENT</th>
              <th className="px-3 py-2 font-medium">TYPE</th>
              <th className="px-3 py-2 font-medium">FILLED</th>
              <th className="px-3 py-2 font-medium text-right">FILL PX</th>
              <th className="px-3 py-2 font-medium">REASON</th>
              <th className="px-3 py-2 font-medium">ACTIONS</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(o => {
              const instrument = `${o.underlying} ${o.expiry} ${o.strike}${o.right}`;
              const typeStr    = o.order_type === "MKT" ? "MKT" : `LMT @${o.limit_price?.toFixed(2)}`;
              const cancellable = o.status === "staged" || o.status === "submitted";
              const isCancel    = cancellingId === o.order_id;
              return (
                <tr key={o.order_id} className="border-t border-zinc-800/70 hover:bg-zinc-900/40">
                  <td className="px-3 py-2">
                    <span className={`px-2 py-0.5 text-[9px] tracking-[0.18em] uppercase ${STATUS_BADGE[o.status]}`}>
                      {o.status}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-zinc-400">…{o.order_id.slice(-8)}</td>
                  <td className={`px-3 py-2 font-bold tracking-[0.18em] ${o.side === "BUY" ? "text-emerald-400" : "text-red-400"}`}>
                    {o.side}
                  </td>
                  <td className="px-3 py-2 text-right text-zinc-200">{o.qty}</td>
                  <td className="px-3 py-2 text-zinc-100">{instrument}</td>
                  <td className="px-3 py-2 text-zinc-300">{typeStr}</td>
                  <td className="px-3 py-2 text-zinc-400">
                    <span className={o.filled_qty > 0 && o.filled_qty < o.qty ? "text-yellow-300" : ""}>
                      {o.filled_qty}/{o.qty}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-right text-zinc-200">
                    {o.fill_price != null
                      ? o.fill_price.toFixed(2)
                      : <span className="text-zinc-600">—</span>}
                  </td>
                  <td className="px-3 py-2 text-zinc-500 max-w-[240px] truncate" title={o.reason ?? ""}>
                    {o.reason ?? <span className="text-zinc-700">—</span>}
                  </td>
                  <td className="px-3 py-2">
                    {cancellable && (
                      <button
                        onClick={() => cancelMutation.mutate(o.order_id)}
                        disabled={cancelMutation.isPending}
                        className="px-2 py-[1px] text-[9px] font-bold uppercase tracking-wider border border-[#ffb4ab]/40 text-[#ffb4ab] hover:bg-red-900/20 disabled:opacity-40 transition-colors"
                      >
                        {isCancel ? "…" : "Cancel"}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={10} className="px-3 py-10 text-center text-zinc-600 tracking-wider">
                  NO ORDERS MATCH FILTER
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function KpiTile({
  label, value, tone,
}: {
  label: string;
  value: number;
  tone: "default" | "muted" | "blue" | "emerald" | "red";
}) {
  const toneCls = {
    default: "text-[#adc6ff]",
    muted:   "text-zinc-400",
    blue:    "text-blue-300",
    emerald: "text-emerald-300",
    red:     "text-red-400",
  }[tone];
  return (
    <div className="border border-zinc-800 bg-[#0e0e10] px-3 py-2.5">
      <div className="text-[9px] tracking-[0.22em] text-zinc-500">{label}</div>
      <div className={`mt-1 text-2xl font-bold tabular-nums ${toneCls}`}>{value}</div>
    </div>
  );
}
