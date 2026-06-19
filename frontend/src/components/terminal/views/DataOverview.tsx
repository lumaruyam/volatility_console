import { useState, useRef, useEffect, useCallback, lazy, Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ChevronDown, ChevronRight, Database, Cpu, Radio,
  TrendingUp, CheckCheck, CheckCircle2, ZoomIn, ChevronsUpDown,
  AlertTriangle, XCircle, Download, List, BarChart2, RefreshCw,
  ChevronUp, Filter,
} from "lucide-react";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
} from "recharts";
import { Panel, StatusPill } from "../ui";

const VolSurface3D = lazy(() => import("./VolSurface3D"));

// ---------------------------------------------------------------------------
// API response types
// ---------------------------------------------------------------------------

type IndexRow = { ticker: string; name: string; spot: number; atm_vol: number };
type ChainRow = {
  strike: number;
  call_bid: number; call_ask: number; call_iv: number;
  call_delta: number; call_gamma: number; call_vega: number; call_theta: number;
  call_volume?: number; call_oi?: number; call_qc: string;
  put_bid: number; put_ask: number; put_iv: number;
  put_delta: number; put_gamma: number; put_vega: number; put_theta: number;
  put_volume?: number; put_oi?: number; put_qc: string;
  atm: boolean;
};
type EngineStatus = {
  spot_ingestion: { status: string; latency_ms: number };
  forward_curve:  { id: string; tenor: string };
  calibration:    { rmse: number; status: string };
  engine_load_pct: number;
  rate?: number;
};
type VolSurface = {
  strikes: number[];
  maturities: string[];
  implied_vols: number[][];
  smile_slice_30d: {
    strikes: number[];
    call_ivs: number[];
    put_ivs: number[];
    cal_arb: string;
    bfly_arb: string;
  };
  calibration: { rmse: number; status: string; model: string };
};
type GreeksSummary = {
  total_delta: number;
  total_gamma: number;
  total_vega: number;
  total_theta: number;
  total_rho?: number;
};
type ExpiryOption = { value: string; label: string };
type ForwardPoint = { expiry: string; forward: number; maturity_days: number };
type SmileData = {
  strikes: number[];
  raw_iv: number[];
  fitted_iv: number[];
  expiry: string;
  atm_vol_pct: number;
  atm_strike: number;
};

const apiFetch = (url: string) =>
  fetch(url).then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); });

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SIDEBAR_MIN = 220;
const SIDEBAR_MAX = 450;
const SIDEBAR_DEFAULT = 240;

const FALLBACK_EXPIRIES: ExpiryOption[] = [
  { value: "2026-12-15", label: "2026-12-15 (30D)" },
  { value: "2027-01-19", label: "2027-01-19 (65D)" },
  { value: "2027-03-21", label: "2027-03-21 (120D)" },
];

const LOADING_INDEX: IndexRow[] = [
  { ticker: "ASML",    name: "ASML Holding",         spot: 889.30,   atm_vol: 0.242 },
  { ticker: "MC.PA",   name: "LVMH",                  spot: 512.60,   atm_vol: 0.198 },
  { ticker: "SAP",     name: "SAP SE",                spot: 143.02,   atm_vol: 0.215 },
  { ticker: "SIE",     name: "Siemens AG",            spot: 274.17,   atm_vol: 0.221 },
  { ticker: "OR.PA",   name: "L'Oréal",               spot: 385.85,   atm_vol: 0.174 },
  { ticker: "TTE",     name: "TotalEnergies",         spot:  78.00,   atm_vol: 0.236 },
  { ticker: "SU.PA",   name: "Schneider Electric",    spot: 276.95,   atm_vol: 0.208 },
  { ticker: "AIR",     name: "Airbus SE",             spot: 183.68,   atm_vol: 0.251 },
  { ticker: "ALV",     name: "Allianz SE",            spot: 397.10,   atm_vol: 0.162 },
  { ticker: "SAN.MC",  name: "Banco Santander",       spot:  11.63,   atm_vol: 0.284 },
  { ticker: "BNP",     name: "BNP Paribas",           spot:  98.65,   atm_vol: 0.267 },
  { ticker: "AI.PA",   name: "Air Liquide",           spot: 165.86,   atm_vol: 0.159 },
  { ticker: "DTE",     name: "Deutsche Telekom",      spot:  28.01,   atm_vol: 0.148 },
  { ticker: "IBE.MC",  name: "Iberdrola",             spot:  20.49,   atm_vol: 0.171 },
  { ticker: "SASY",    name: "Sanofi",                spot:  76.30,   atm_vol: 0.168 },
  { ticker: "ITX.MC",  name: "Inditex",               spot:  56.64,   atm_vol: 0.192 },
  { ticker: "UCG.MI",  name: "UniCredit SpA",         spot:  77.64,   atm_vol: 0.315 },
  { ticker: "INGA",    name: "ING Groep",             spot:  26.33,   atm_vol: 0.259 },
  { ticker: "BAS",     name: "BASF SE",               spot:  49.30,   atm_vol: 0.210 },
  { ticker: "BMW",     name: "BMW AG",                spot:  67.14,   atm_vol: 0.234 },
  { ticker: "BAYN",    name: "Bayer AG",              spot:  36.08,   atm_vol: 0.342 },
  { ticker: "BBVA.MC", name: "BBVA",                  spot:  21.10,   atm_vol: 0.291 },
  { ticker: "EL.PA",   name: "EssilorLuxottica",      spot: 184.05,   atm_vol: 0.185 },
  { ticker: "RMS.PA",  name: "Hermès International",  spot: 1712.00,  atm_vol: 0.227 },
  { ticker: "ISP.MI",  name: "Intesa Sanpaolo",       spot:   6.05,   atm_vol: 0.246 },
  { ticker: "DHL",     name: "DHL Group",             spot:  52.82,   atm_vol: 0.203 },
  { ticker: "ENEL.MI", name: "Enel SpA",              spot:   9.93,   atm_vol: 0.190 },
  { ticker: "ENI.MI",  name: "Eni SpA",               spot:  22.01,   atm_vol: 0.225 },
  { ticker: "ABI.BR",  name: "AB InBev",              spot:  70.76,   atm_vol: 0.189 },
  { ticker: "AD.AS",   name: "Ahold Delhaize",        spot:  36.01,   atm_vol: 0.153 },
  { ticker: "ADYEN",   name: "Adyen NV",              spot: 858.90,   atm_vol: 0.416 },
  { ticker: "ADS",     name: "Adidas AG",             spot: 174.55,   atm_vol: 0.278 },
  { ticker: "SGEF",    name: "Vinci SA",              spot: 123.35,   atm_vol: 0.181 },
  { ticker: "SAF.PA",  name: "Safran SA",             spot: 324.00,   atm_vol: 0.212 },
  { ticker: "RACE.MI", name: "Ferrari NV",            spot: 310.30,   atm_vol: 0.250 },
  { ticker: "MUV2",    name: "Munich Re",             spot: 461.50,   atm_vol: 0.175 },
  { ticker: "CRH",     name: "CRH Plc",              spot:  74.20,   atm_vol: 0.239 },
  { ticker: "FLTR",    name: "Flutter Entertainment", spot: 185.40,   atm_vol: 0.280 },
  { ticker: "BN.PA",   name: "Danone",                spot:  66.46,   atm_vol: 0.157 },
  { ticker: "DB1",     name: "Deutsche Börse",        spot: 248.60,   atm_vol: 0.164 },
  { ticker: "DBK",     name: "Deutsche Bank",         spot:  30.30,   atm_vol: 0.295 },
  { ticker: "IFX",     name: "Infineon Technologies", spot:  79.98,   atm_vol: 0.331 },
  { ticker: "PRX.AS",  name: "Prosus NV",             spot:  39.21,   atm_vol: 0.263 },
  { ticker: "CS.PA",   name: "AXA SA",                spot:  41.88,   atm_vol: 0.179 },
  { ticker: "KER.PA",  name: "Kering",                spot: 259.20,   atm_vol: 0.285 },
  { ticker: "STLAM",   name: "Stellantis NV",         spot:   5.97,   atm_vol: 0.320 },
  { ticker: "HEIA",    name: "Heineken NV",           spot:  92.45,   atm_vol: 0.183 },
  { ticker: "VOW3",    name: "Volkswagen Pref",       spot:  85.42,   atm_vol: 0.261 },
  { ticker: "ENGI",    name: "Engie SA",              spot:  27.01,   atm_vol: 0.214 },
  { ticker: "NOKIA",   name: "Nokia Oyj",             spot:  12.10,   atm_vol: 0.248 },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function spreadPct(bid: number, ask: number): number {
  const mid = (bid + ask) / 2;
  return mid > 0 ? ((ask - bid) / mid) * 100 : 0;
}

function exportChainCSV(chain: ChainRow[], ticker: string, expiry: string) {
  const headers = [
    "Strike",
    "CallMid","CallSprd%","CallIV%","CallDelta","CallGamma","CallVega","CallTheta","CallVol","CallOI","CallQC",
    "PutMid","PutSprd%","PutIV%","PutDelta","PutGamma","PutVega","PutTheta","PutVol","PutOI","PutQC",
  ];
  const rows = chain.map(r => [
    r.strike,
    ((r.call_bid + r.call_ask) / 2).toFixed(2), spreadPct(r.call_bid, r.call_ask).toFixed(2),
    r.call_iv.toFixed(4), r.call_delta.toFixed(4), r.call_gamma.toFixed(6),
    r.call_vega.toFixed(2), r.call_theta.toFixed(2),
    r.call_volume ?? "", r.call_oi ?? "", r.call_qc,
    ((r.put_bid + r.put_ask) / 2).toFixed(2), spreadPct(r.put_bid, r.put_ask).toFixed(2),
    r.put_iv.toFixed(4), r.put_delta.toFixed(4), r.put_gamma.toFixed(6),
    r.put_vega.toFixed(2), r.put_theta.toFixed(2),
    r.put_volume ?? "", r.put_oi ?? "", r.put_qc,
  ].join(","));
  const csv = [headers.join(","), ...rows].join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${ticker}_${expiry}_chain.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function DataOverview() {
  const [selected, setSelected]         = useState("ASML");
  const [expiry, setExpiry]             = useState(FALLBACK_EXPIRIES[0].value);
  const [optionSide, setOptionSide]     = useState<"BOTH" | "CALLS" | "PUTS">("BOTH");
  const [strikeFilter, setStrikeFilter] = useState<"ALL" | "ATM10" | "DELTA30">("ALL");
  const [timeRange, setTimeRange]       = useState<"1D" | "1W" | "1M" | "3M" | "1Y">("1M");
  const [liquidityFilter, setLiquidity] = useState(false);
  const [chainView, setChainView]       = useState<"TABLE" | "HEATMAP">("TABLE");
  const [showForwardCurve, setShowFwd]  = useState(false);
  const [showSmile, setShowSmile]       = useState(false);
  const [smileExpiry, setSmileExpiry]   = useState("3M");
  const [lastUpdated, setLastUpdated]   = useState<Date | null>(null);
  const [secsAgo, setSecsAgo]           = useState(0);

  // ── Sidebar resize ────────────────────────────────────────────────────────
  const [sidebarW, setSidebarW] = useState(SIDEBAR_DEFAULT);
  const dragActive  = useRef(false);
  const dragOriginX = useRef(0);
  const dragOriginW = useRef(SIDEBAR_DEFAULT);

  const onResizeStart = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    e.preventDefault();
    dragActive.current  = true;
    dragOriginX.current = e.clientX;
    dragOriginW.current = sidebarW;
    document.body.style.cursor     = "col-resize";
    document.body.style.userSelect = "none";
  }, [sidebarW]);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragActive.current) return;
      const next = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, dragOriginW.current + e.clientX - dragOriginX.current));
      setSidebarW(next);
    };
    const onUp = () => {
      if (!dragActive.current) return;
      dragActive.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => { window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
  }, []);

  // ── "X ago" ticker ───────────────────────────────────────────────────────
  useEffect(() => {
    const id = setInterval(() => {
      if (lastUpdated) setSecsAgo(Math.floor((Date.now() - lastUpdated.getTime()) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [lastUpdated]);

  // ── Queries ───────────────────────────────────────────────────────────────
  const { data: indexData = LOADING_INDEX, isPending: indexLoading } = useQuery<IndexRow[]>({
    queryKey: ["index-matrix"],
    queryFn:  () => apiFetch("/api/market/index-matrix"),
    staleTime: 30_000,
    refetchInterval: 30_000,
  });

  const { data: expiriesRaw } = useQuery<ExpiryOption[]>({
    queryKey: ["expiries", selected],
    queryFn:  () => apiFetch(`/api/market/expiries?ticker=${selected}`),
    staleTime: 60_000,
  });
  const expiryOptions = expiriesRaw ?? FALLBACK_EXPIRIES;

  const { data: chainData = [], dataUpdatedAt } = useQuery<ChainRow[]>({
    queryKey: ["options-chain", selected, expiry, timeRange],
    queryFn:  () =>
      apiFetch(`/api/market/options-chain?ticker=${selected}&expiry=${expiry}&range=${timeRange}`),
    staleTime: 15_000,
    refetchInterval: 15_000,
  });

  useEffect(() => {
    if (chainData.length > 0) { setLastUpdated(new Date(dataUpdatedAt)); setSecsAgo(0); }
  }, [dataUpdatedAt, chainData.length]);

  const { data: status } = useQuery<EngineStatus>({
    queryKey: ["engine-status"],
    queryFn:  () => apiFetch("/api/market/engine-status"),
    staleTime: 5_000,
    refetchInterval: 30_000,
  });

  const { data: surface } = useQuery<VolSurface>({
    queryKey: ["vol-surface", selected],
    queryFn:  () => apiFetch(`/api/market/vol-surface?ticker=${selected}`),
    staleTime: 30_000,
    refetchInterval: 30_000,
  });

  const { data: greeks } = useQuery<GreeksSummary>({
    queryKey: ["greeks-summary", selected],
    queryFn:  () => apiFetch(`/api/market/greeks-summary?ticker=${selected}`),
    staleTime: 30_000,
    refetchInterval: 30_000,
  });

  const { data: forwardCurve = [] } = useQuery<ForwardPoint[]>({
    queryKey: ["forward-curve", selected],
    queryFn:  () => apiFetch(`/api/market/forward-curve?ticker=${selected}`),
    staleTime: 60_000,
    enabled: showForwardCurve,
  });

  const { data: smileSlice } = useQuery<SmileData>({
    queryKey: ["smile", selected, smileExpiry],
    queryFn:  () => apiFetch(`/api/market/smile?ticker=${selected}&expiry=${smileExpiry}`),
    staleTime: 60_000,
    enabled: showSmile,
  });

  // ── Derived display data ──────────────────────────────────────────────────
  const smileData = surface?.smile_slice_30d
    ? surface.smile_slice_30d.strikes.map((k, i) => ({
        strike: k,
        callIV: +(surface.smile_slice_30d.call_ivs[i] * 100).toFixed(2),
        putIV:  +(surface.smile_slice_30d.put_ivs[i] * 100).toFixed(2),
      }))
    : [];

  // ATM vol term structure derived from surface grid (moneyness index 4 = 1.00)
  const atmTermStructure = surface?.maturities?.map((m, i) => ({
    maturity: m,
    atm_vol: +((surface.implied_vols[i]?.[4] ?? 0) * 100).toFixed(2),
  })) ?? [];

  // Smile chart data from dedicated smile query
  const smileChartData = smileSlice
    ? smileSlice.strikes.map((k, i) => ({
        strike: k,
        raw:    smileSlice.raw_iv[i],
        fitted: smileSlice.fitted_iv[i],
      }))
    : [];

  const calArb      = surface?.smile_slice_30d?.cal_arb  ?? "clear";
  const bflyArb     = surface?.smile_slice_30d?.bfly_arb ?? "clear";
  const calibStatus = surface?.calibration?.status ?? "pending";
  const calibModel  = surface?.calibration?.model  ?? "SVI Spline";
  const calibRmse   = surface?.calibration?.rmse   ?? 0.0;
  const isQcPass    = calibStatus === "converged";

  // Rate from API (EngineStatus) or fallback
  const displayRate = status?.rate != null
    ? (status.rate * 100).toFixed(2) + "%"
    : "3.45%";

  const selectedRow = indexData.find(r => r.ticker === selected);
  const refSpot = selectedRow
    ? selectedRow.spot.toLocaleString(undefined, { maximumFractionDigits: 1 })
    : "—";

  // Chain filtering
  const atmStrike = chainData.find(r => r.atm)?.strike ?? 0;
  const showCalls = optionSide !== "PUTS";
  const showPuts  = optionSide !== "CALLS";

  const filteredChain = chainData.filter(row => {
    if (strikeFilter === "ATM10" && atmStrike)
      return Math.abs(row.strike - atmStrike) / atmStrike <= 0.10;
    if (strikeFilter === "DELTA30")
      return row.call_delta >= 0.20 && row.call_delta <= 0.80;
    if (liquidityFilter)
      return (row.call_volume ?? 0) >= 100 || (row.put_volume ?? 0) >= 100;
    return true;
  }).filter(row => {
    if (liquidityFilter)
      return (row.call_volume ?? 0) >= 100 || (row.put_volume ?? 0) >= 100;
    return true;
  });

  const fmtVol = (v?: number) =>
    v == null ? "—" : v >= 10_000 ? (v / 1000).toFixed(0) + "k" : v.toLocaleString();
  const fmtOI = (v?: number) =>
    v == null ? "—" : v >= 10_000 ? (v / 1000).toFixed(0) + "k" : v.toLocaleString();

  // Heatmap column normalisation (max abs per Greek column)
  const heatmaxDelta = Math.max(...filteredChain.map(r => Math.abs(r.call_delta)), 0.001);
  const heatmaxGamma = Math.max(...filteredChain.map(r => Math.abs(r.call_gamma)), 0.001);
  const heatmaxVega  = Math.max(...filteredChain.map(r => Math.abs(r.call_vega)),  0.001);
  const heatmaxTheta = Math.max(...filteredChain.map(r => Math.abs(r.call_theta)), 0.001);

  return (
    <div className="flex h-full min-h-0 overflow-hidden">
      {/* ── EURO STOXX 50 sidebar ─────────────────────────────────────────── */}
      <aside
        style={{ width: sidebarW }}
        className="shrink-0 bg-[#131315] flex flex-col overflow-hidden"
      >
        <div className="h-8 shrink-0 flex items-center justify-between px-2.5 border-b border-zinc-800 bg-[#1c1b1d]">
          <span className="text-[10px] font-bold tracking-[0.16em] uppercase text-zinc-200">EURO STOXX 50</span>
          <ChevronsUpDown className="w-3 h-3 text-zinc-500" />
        </div>
        <div className="overflow-y-auto flex-1 vc-scroll">
          <table className="w-full">
            <thead className="sticky top-0 bg-[#1c1b1d] z-10">
              <tr className="text-[9px] font-bold tracking-widest uppercase text-zinc-500 border-b border-zinc-800">
                <th className="px-2.5 py-1.5 text-left font-normal">Ticker / Name</th>
                <th className="px-2.5 py-1.5 text-right font-normal">Spot</th>
                <th className="px-2.5 py-1.5 text-right font-normal">ATM Vol</th>
              </tr>
            </thead>
            <tbody className="font-mono text-[11px]">
              {indexLoading
                ? Array.from({ length: 12 }).map((_, i) => (
                    <tr key={i} className="border-b border-zinc-800/60">
                      <td className="px-2.5 py-1.5"><div className="animate-pulse bg-zinc-800 h-3 w-20 rounded" /></td>
                      <td className="px-2.5 py-1.5"><div className="animate-pulse bg-zinc-800 h-3 w-12 rounded ml-auto" /></td>
                      <td className="px-2.5 py-1.5"><div className="animate-pulse bg-zinc-800 h-3 w-10 rounded ml-auto" /></td>
                    </tr>
                  ))
                : indexData.map(r => {
                    const isSel  = selected === r.ticker;
                    const volPct = (r.atm_vol * 100).toFixed(1) + "%";
                    const vColor = r.atm_vol < 0.15
                      ? "text-emerald-400"
                      : r.atm_vol > 0.25
                      ? "text-[#ffb4ab]"
                      : "text-zinc-300";
                    const spotStr = r.spot.toLocaleString(undefined, { maximumFractionDigits: 2 });
                    return (
                      <tr
                        key={r.ticker}
                        onClick={() => setSelected(r.ticker)}
                        className={`border-b border-zinc-800/60 cursor-pointer hover:bg-zinc-800/60 ${isSel ? "bg-[#adc6ff]/10" : ""}`}
                      >
                        <td className={`px-2.5 py-1 leading-tight ${isSel ? "text-[#adc6ff]" : "text-zinc-200"}`}>
                          <div className="font-bold">{r.ticker}</div>
                          <div className="text-[9px] text-zinc-500 truncate max-w-[100px]">{r.name}</div>
                        </td>
                        <td className="px-2.5 py-1 text-right text-zinc-200 align-top">{spotStr}</td>
                        <td className={`px-2.5 py-1 text-right align-top ${vColor}`}>{volPct}</td>
                      </tr>
                    );
                  })
              }
            </tbody>
          </table>
        </div>
      </aside>

      {/* ── Resize handle ─────────────────────────────────────────────────── */}
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Drag to resize sidebar"
        onMouseDown={onResizeStart}
        className="group relative w-1 shrink-0 cursor-col-resize select-none bg-zinc-800"
      >
        <div className="absolute inset-0 transition-colors duration-100 group-hover:bg-[#adc6ff]/50" />
        <div className="absolute inset-y-0 left-1/2 -translate-x-1/2 flex flex-col items-center justify-center gap-[3px] opacity-0 group-hover:opacity-100 transition-opacity duration-100">
          {[0, 1, 2].map(i => <div key={i} className="w-[3px] h-[3px] rounded-full bg-[#adc6ff]/80" />)}
        </div>
      </div>

      {/* ── Main grid ─────────────────────────────────────────────────────── */}
      <div className="flex-1 min-w-0 flex flex-col">
        {/* Context header */}
        <div className="h-10 shrink-0 border-b border-zinc-800 bg-[#0e0e10] px-3 flex items-center gap-3">
          <Database className="w-4 h-4 text-[#adc6ff]" />
          <span className="text-[10px] font-bold tracking-[0.18em] uppercase text-zinc-300">MARKET DATA &amp; SNAPSHOTS</span>
          <ChevronRight className="w-3 h-3 text-zinc-600" />
          <span className="font-mono text-[11px] text-zinc-500">{selected}</span>

          <div className="ml-auto flex items-center gap-2">
            {/* Auto-refresh badge */}
            {lastUpdated && (
              <div className="flex items-center gap-1 px-1.5 py-0.5 bg-zinc-900 border border-zinc-800">
                <RefreshCw className="w-2.5 h-2.5 text-zinc-600" />
                <span className="font-mono text-[9px] text-zinc-600">
                  {secsAgo < 5 ? "LIVE" : `${secsAgo}s ago`}
                </span>
              </div>
            )}
            <button className="flex items-center gap-1 px-2 py-1 border border-zinc-800 bg-[#1c1b1d] hover:bg-zinc-800">
              <span className="font-mono text-[12px] text-[#adc6ff]">{selected}</span>
              <ChevronDown className="w-3 h-3 text-zinc-500" />
            </button>
            <Metric label="REF SPOT (S0)" value={refSpot} />
            <Metric label="RATE (r)" value={displayRate} valueClass="text-emerald-400" />
            <button className="flex items-center gap-1 px-2 py-1 border border-zinc-800 bg-[#2a2a2c]">
              <Cpu className="w-3 h-3 text-[#adc6ff]" />
              <span className="font-mono text-[12px]">{calibModel}</span>
            </button>
            <StatusPill tone={isQcPass ? "ok" : "warn"}>
              <span className="inline-flex items-center gap-1">
                <span className={`w-1.5 h-1.5 rounded-full vc-blink ${isQcPass ? "bg-emerald-400" : "bg-yellow-400"}`} />
                QC: {isQcPass ? "PASS" : "PENDING"}
              </span>
            </StatusPill>
          </div>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto vc-scroll">
          <div className="p-2.5 flex flex-col gap-2.5">
          {/* KPI strip */}
          <div className="grid grid-cols-4 gap-2.5 shrink-0">
            <KPI title="SPOT INGESTION" Icon={Radio} iconClass="text-emerald-400">
              <div className="flex items-baseline gap-2">
                <span className="font-mono text-[15px] text-zinc-100">
                  {(status?.spot_ingestion.status ?? "SYNCHRONIZED").toUpperCase()}
                </span>
                <span className="font-mono text-[11px] text-zinc-500">
                  Δ: {status?.spot_ingestion.latency_ms ?? 2}ms
                </span>
              </div>
            </KPI>
            <KPI title="FORWARD CURVE ID" Icon={TrendingUp}>
              <div className="flex items-baseline gap-2">
                <span className="font-mono text-[15px] text-[#adc6ff]">
                  {status?.forward_curve.id ?? "SOFR-OIS + Div"}
                </span>
                <span className="px-1 py-[1px] bg-zinc-800 text-[9px] font-bold tracking-wider rounded-sm text-zinc-300">
                  {status?.forward_curve.tenor ?? "T+1"}
                </span>
              </div>
            </KPI>
            <KPI title="CALIBRATION PERF" Icon={CheckCheck} iconClass="text-emerald-400">
              <div className="flex items-baseline justify-between gap-2">
                <span className="font-mono text-[15px]">
                  RMSE {(status?.calibration.rmse ?? calibRmse).toFixed(4)}
                </span>
                <StatusPill tone={isQcPass ? "ok" : "warn"}>
                  {(status?.calibration.status ?? calibStatus).toUpperCase()}
                </StatusPill>
              </div>
            </KPI>
            <KPI title="ENGINE HEALTH" Icon={Cpu}>
              <div className="flex items-center gap-2">
                <div className="flex-1 h-1.5 bg-zinc-800 overflow-hidden">
                  <div className="h-full bg-[#adc6ff]" style={{ width: `${status?.engine_load_pct ?? 42}%` }} />
                </div>
                <span className="font-mono text-[11px] text-zinc-500">
                  {status?.engine_load_pct ?? 42}% LOAD
                </span>
              </div>
            </KPI>
          </div>

          {/* Vol surface + 2D smile */}
          <div className="grid grid-cols-12 gap-2.5 shrink-0" style={{ minHeight: 280 }}>
            <Panel
              className="col-span-8"
              title="Vol Surface 3D — Strike / Maturity / IV%"
              right={<>
                <span className="text-zinc-600">x = STRIKE</span>
                <span className="pl-2 border-l border-zinc-800">z = MATURITY</span>
                <span className="pl-2 border-l border-zinc-800">y = IV%</span>
              </>}
              padded={false}
            >
              <Suspense fallback={
                <div className="vc-mesh-bg w-full h-full flex items-center justify-center">
                  <span className="font-mono text-[10px] text-zinc-600">LOADING 3D ENGINE…</span>
                </div>
              }>
                {surface ? (
                  <VolSurface3D surface={surface} />
                ) : (
                  <div className="vc-mesh-bg w-full h-full flex items-center justify-center">
                    <span className="font-mono text-[10px] text-zinc-600">AWAITING SURFACE DATA…</span>
                  </div>
                )}
              </Suspense>
            </Panel>

            <Panel
              className="col-span-4"
              title="2D Smile Expiry Slice: 30D"
              right={<ZoomIn className="w-3 h-3 text-zinc-500" />}
            >
              <div className="flex gap-1 mb-1.5 shrink-0">
                <StatusPill tone={calArb === "clear" ? "ok" : "warn"}>
                  <span className="inline-flex items-center gap-1">
                    <span className={`w-1 h-1 rounded-full ${calArb === "clear" ? "bg-emerald-400" : "bg-yellow-400"}`} />
                    CAL ARB: {calArb.toUpperCase()}
                  </span>
                </StatusPill>
                <StatusPill tone={bflyArb === "clear" ? "ok" : "warn"}>
                  <span className="inline-flex items-center gap-1">
                    <span className={`w-1 h-1 rounded-full ${bflyArb === "clear" ? "bg-emerald-400" : "bg-yellow-400"}`} />
                    BFLY ARB: {bflyArb.toUpperCase()}
                  </span>
                </StatusPill>
              </div>
              <div className="flex-1 min-h-0" style={{ height: "calc(100% - 28px)" }}>
                {smileData.length > 0 ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={smileData} margin={{ top: 4, right: 8, bottom: 16, left: 24 }}>
                      <XAxis dataKey="strike" tick={{ fontSize: 9, fill: "#71717a" }} tickLine={false} />
                      <YAxis
                        tickFormatter={v => `${(v as number).toFixed(0)}%`}
                        tick={{ fontSize: 9, fill: "#71717a" }}
                        tickLine={false}
                        axisLine={false}
                        domain={["auto", "auto"]}
                      />
                      <Tooltip
                        contentStyle={{ background: "#1c1b1d", border: "1px solid #3f3f46", fontSize: 10, borderRadius: 2 }}
                        formatter={(v: unknown, name: string) => [`${(v as number).toFixed(2)}%`, name]}
                      />
                      <Line type="monotone" dataKey="callIV" stroke="#adc6ff" strokeWidth={1.4} dot={false} name="Call IV" />
                      <Line type="monotone" dataKey="putIV"  stroke="#4edea3" strokeWidth={1.4} dot={false} name="Put IV" />
                    </LineChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="w-full h-full flex items-center justify-center">
                    <span className="font-mono text-[10px] text-zinc-600">LOADING…</span>
                  </div>
                )}
              </div>
            </Panel>
          </div>

          {/* Greeks summary — 5 boxes including Rho */}
          {greeks && (
            <div className="grid grid-cols-5 gap-2.5 shrink-0">
              <MetricBox label="PORTFOLIO Δ" value={greeks.total_delta.toFixed(4)} />
              <MetricBox label="PORTFOLIO Γ" value={greeks.total_gamma.toFixed(4)} />
              <MetricBox label="PORTFOLIO V" value={"€" + greeks.total_vega.toLocaleString(undefined, { maximumFractionDigits: 0 })} />
              <MetricBox label="PORTFOLIO Θ" value={"€" + greeks.total_theta.toLocaleString(undefined, { maximumFractionDigits: 0 })} valueClass="text-[#ffb4ab]" />
              <MetricBox
                label="PORTFOLIO ρ"
                value={greeks.total_rho != null
                  ? "€" + greeks.total_rho.toLocaleString(undefined, { maximumFractionDigits: 0 })
                  : "—"}
                valueClass="text-[#adc6ff]"
              />
            </div>
          )}

          {/* Options chain */}
          <Panel
            title="Centered-Strike Straddle Options Chain"
            padded={false}
            className="min-h-[360px]"
            right={<>
              {/* VIEW TOGGLE */}
              <button
                onClick={() => setChainView(v => v === "TABLE" ? "HEATMAP" : "TABLE")}
                className="flex items-center gap-1 px-2 py-0.5 border border-zinc-800 hover:border-zinc-600 text-zinc-500 hover:text-zinc-300 transition-colors"
                title="Toggle heatmap / table view"
              >
                {chainView === "TABLE"
                  ? <BarChart2 className="w-3 h-3" />
                  : <List className="w-3 h-3" />
                }
                <span className="font-mono text-[10px]">{chainView === "TABLE" ? "HEATMAP" : "TABLE"}</span>
              </button>
              {/* EXPORT */}
              <button
                onClick={() => exportChainCSV(filteredChain, selected, expiry)}
                className="flex items-center gap-1 px-2 py-0.5 border border-zinc-800 hover:border-zinc-600 text-zinc-500 hover:text-zinc-300 transition-colors"
                title="Export chain to CSV"
              >
                <Download className="w-3 h-3" />
                <span className="font-mono text-[10px]">CSV</span>
              </button>
              {/* EXPIRY */}
              <span>EXPIRY:</span>
              <select
                value={expiry}
                onChange={e => setExpiry(e.target.value)}
                className="bg-[#131315] border border-zinc-800 px-1 py-[1px] font-mono text-[11px] focus:outline-none focus:border-[#adc6ff]"
              >
                {expiryOptions.map(o => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </>}
          >
            <div className="flex flex-col h-full min-h-0">
              {/* ── Filter sub-bar ── */}
              <div className="shrink-0 flex items-center gap-2 flex-wrap px-2.5 py-1.5 border-b border-zinc-800 bg-[#0e0e10]">
                {/* Option type */}
                <span className="text-[9px] font-bold tracking-widest uppercase text-zinc-600">TYPE:</span>
                {(["BOTH", "CALLS", "PUTS"] as const).map(s => (
                  <button
                    key={s}
                    onClick={() => setOptionSide(s)}
                    className={`px-2 py-0.5 font-mono text-[10px] font-bold border transition-colors ${
                      optionSide === s
                        ? "border-[#adc6ff] bg-[#adc6ff]/10 text-[#adc6ff]"
                        : "border-zinc-800 text-zinc-500 hover:border-zinc-700 hover:text-zinc-300"
                    }`}
                  >
                    {s}
                  </button>
                ))}

                <div className="w-px h-3 bg-zinc-800 mx-0.5" />

                {/* Strike range */}
                <span className="text-[9px] font-bold tracking-widest uppercase text-zinc-600">RANGE:</span>
                {([
                  ["ALL",     "ALL STRIKES"],
                  ["ATM10",   "ATM ±10%"],
                  ["DELTA30", "−30Δ · +30Δ"],
                ] as const).map(([val, label]) => (
                  <button
                    key={val}
                    onClick={() => setStrikeFilter(val)}
                    className={`px-2 py-0.5 font-mono text-[10px] font-bold border transition-colors ${
                      strikeFilter === val
                        ? "border-[#adc6ff] bg-[#adc6ff]/10 text-[#adc6ff]"
                        : "border-zinc-800 text-zinc-500 hover:border-zinc-700 hover:text-zinc-300"
                    }`}
                  >
                    {label}
                  </button>
                ))}

                <div className="w-px h-3 bg-zinc-800 mx-0.5" />

                {/* Time range */}
                <span className="text-[9px] font-bold tracking-widest uppercase text-zinc-600">RANGE:</span>
                {(["1D", "1W", "1M", "3M", "1Y"] as const).map(r => (
                  <button
                    key={r}
                    onClick={() => setTimeRange(r)}
                    className={`px-2 py-0.5 font-mono text-[10px] font-bold border transition-colors ${
                      timeRange === r
                        ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-400"
                        : "border-zinc-800 text-zinc-500 hover:border-zinc-700 hover:text-zinc-300"
                    }`}
                  >
                    {r}
                  </button>
                ))}

                <div className="w-px h-3 bg-zinc-800 mx-0.5" />

                {/* Liquidity filter */}
                <button
                  onClick={() => setLiquidity(v => !v)}
                  className={`flex items-center gap-1 px-2 py-0.5 font-mono text-[10px] font-bold border transition-colors ${
                    liquidityFilter
                      ? "border-yellow-500/50 bg-yellow-500/10 text-yellow-400"
                      : "border-zinc-800 text-zinc-500 hover:border-zinc-700 hover:text-zinc-300"
                  }`}
                  title="Show only options with volume ≥ 100"
                >
                  <Filter className="w-2.5 h-2.5" />
                  LIQ ≥100
                </button>

                <span className="ml-auto font-mono text-[9px] text-zinc-700">
                  {filteredChain.length} strikes
                </span>
              </div>

              {/* ── Table / Heatmap area ── */}
              <div className="overflow-auto flex-1 min-h-0 vc-scroll">
                {chainData.length === 0 ? (
                  <div className="flex flex-col gap-2 p-3">
                    {Array.from({ length: 8 }).map((_, i) => (
                      <div key={i} className="animate-pulse bg-zinc-800/60 h-5 rounded" />
                    ))}
                  </div>
                ) : chainView === "HEATMAP" ? (
                  <GreeksHeatmap
                    chain={filteredChain}
                    maxDelta={heatmaxDelta}
                    maxGamma={heatmaxGamma}
                    maxVega={heatmaxVega}
                    maxTheta={heatmaxTheta}
                  />
                ) : (
                  <div className="overflow-x-auto scrollbar-thin scrollbar-thumb-zinc-700 scrollbar-track-transparent">
                  <table className="border-collapse text-right font-mono min-w-[1400px]">
                    <colgroup>
                      {/* 10 call cols: Mid Sprd% IV% Δ Γ V Θ Vol OI QC */}
                      {showCalls && <>
                        <col style={{ minWidth: 48 }} /><col style={{ minWidth: 44 }} />
                        <col style={{ minWidth: 44 }} /><col style={{ minWidth: 40 }} />
                        <col style={{ minWidth: 48 }} /><col style={{ minWidth: 38 }} />
                        <col style={{ minWidth: 44 }} /><col style={{ minWidth: 48 }} />
                        <col style={{ minWidth: 48 }} /><col style={{ minWidth: 32 }} />
                      </>}
                      <col style={{ minWidth: 54 }} />{/* STRIKE */}
                      {/* 10 put cols (mirror): QC OI Vol Θ V Γ Δ IV% Sprd% Mid */}
                      {showPuts && <>
                        <col style={{ minWidth: 32 }} /><col style={{ minWidth: 48 }} />
                        <col style={{ minWidth: 48 }} /><col style={{ minWidth: 44 }} />
                        <col style={{ minWidth: 38 }} /><col style={{ minWidth: 48 }} />
                        <col style={{ minWidth: 40 }} /><col style={{ minWidth: 44 }} />
                        <col style={{ minWidth: 44 }} /><col style={{ minWidth: 48 }} />
                      </>}
                    </colgroup>
                    <thead className="sticky top-0 bg-[#1c1b1d] z-10">
                      <tr className="border-b border-zinc-800">
                        {showCalls && <th colSpan={10} className="py-1 text-center text-[10px] font-bold tracking-[0.14em] text-zinc-500 uppercase border-r border-zinc-800">CALLS</th>}
                        <th className="py-1 text-center text-[10px] font-bold tracking-[0.14em] text-zinc-200 uppercase border-r border-zinc-800 bg-[#0e0e10] sticky left-0 z-20">STRIKE</th>
                        {showPuts && <th colSpan={10} className="py-1 text-center text-[10px] font-bold tracking-[0.14em] text-zinc-500 uppercase">PUTS</th>}
                      </tr>
                      <tr className="border-b border-zinc-800 text-[10px] text-zinc-500">
                        {showCalls && (["Mid","Sprd%","IV%","Δ","Γ","V","Θ","Vol","OI","QC"] as const).map((h, i) => (
                          <th key={"c"+i} className={[
                            "px-1.5 py-1 font-normal border-r border-zinc-800/60",
                            h === "IV%"   ? "text-[#adc6ff]" : "",
                            h === "Θ"    ? "text-[#ffb4ab]/70" : "",
                            h === "Sprd%"? "text-yellow-500/70" : "",
                            h === "QC"   ? "text-center" : "",
                          ].join(" ")}>{h}</th>
                        ))}
                        <th className="px-2 py-1 font-bold text-zinc-200 bg-[#0e0e10] border-r border-zinc-800 text-center sticky left-0 z-20">K</th>
                        {showPuts && (["QC","OI","Vol","Θ","V","Γ","Δ","IV%","Sprd%","Mid"] as const).map((h, i) => (
                          <th key={"p"+i} className={[
                            "px-1.5 py-1 font-normal border-r border-zinc-800/60",
                            i === 9 ? "border-r-0" : "",
                            h === "IV%"   ? "text-[#adc6ff]" : "",
                            h === "Θ"    ? "text-[#ffb4ab]/70" : "",
                            h === "Sprd%"? "text-yellow-500/70" : "",
                            h === "QC"   ? "text-center" : "",
                          ].join(" ")}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="text-[11px]">
                      {filteredChain.map(row => {
                        const atm    = row.atm;
                        const rowCls = atm
                          ? "border-y-2 border-emerald-500/50 bg-emerald-500/5 font-bold"
                          : "border-b border-zinc-800/60 hover:bg-zinc-800/40";
                        const cMid  = ((row.call_bid + row.call_ask) / 2).toFixed(1);
                        const pMid  = ((row.put_bid  + row.put_ask)  / 2).toFixed(1);
                        const cSprd = spreadPct(row.call_bid, row.call_ask);
                        const pSprd = spreadPct(row.put_bid, row.put_ask);
                        return (
                          <tr key={row.strike} className={rowCls}>
                            {/* ── Calls: Mid Sprd% IV% Δ Γ V Θ Vol OI QC ── */}
                            {showCalls && <>
                              <Td>{cMid}</Td>
                              <Td className={cSprd > 5 ? "text-red-400 font-bold" : "text-yellow-500/70"}>
                                {cSprd.toFixed(1)}%
                              </Td>
                              <Td className={atm ? "text-emerald-400" : "text-[#adc6ff]"}>{row.call_iv.toFixed(2)}</Td>
                              <Td>{row.call_delta.toFixed(2)}</Td>
                              <Td>{row.call_gamma.toFixed(4)}</Td>
                              <Td>{row.call_vega.toFixed(1)}</Td>
                              <Td className="text-[#ffb4ab]">{row.call_theta.toFixed(1)}</Td>
                              <Td className="text-zinc-400">{fmtVol(row.call_volume)}</Td>
                              <Td className="text-zinc-400">{fmtOI(row.call_oi)}</Td>
                              <td className="px-1.5 py-1 text-center border-r border-zinc-800/60">
                                <QcIcon status={row.call_qc} />
                              </td>
                            </>}

                            {/* ── Strike centre ── */}
                            <td className={`px-2 py-1 text-center font-bold border-r border-zinc-800 sticky left-0 z-10 ${atm ? "text-emerald-400 bg-emerald-500/10" : "text-zinc-200 bg-[#2a2a2c]/40"}`}>
                              {row.strike}
                            </td>

                            {/* ── Puts mirror: QC OI Vol Θ V Γ Δ IV% Sprd% Mid ── */}
                            {showPuts && <>
                              <td className="px-1.5 py-1 text-center border-r border-zinc-800/60">
                                <QcIcon status={row.put_qc} />
                              </td>
                              <Td className="text-zinc-400">{fmtOI(row.put_oi)}</Td>
                              <Td className="text-zinc-400">{fmtVol(row.put_volume)}</Td>
                              <Td className="text-[#ffb4ab]">{row.put_theta.toFixed(1)}</Td>
                              <Td>{row.put_vega.toFixed(1)}</Td>
                              <Td>{row.put_gamma.toFixed(4)}</Td>
                              <Td>{row.put_delta.toFixed(2)}</Td>
                              <Td className={atm ? "text-emerald-400" : "text-[#adc6ff]"}>{row.put_iv.toFixed(2)}</Td>
                              <Td className={pSprd > 5 ? "text-red-400 font-bold" : "text-yellow-500/70"}>
                                {pSprd.toFixed(1)}%
                              </Td>
                              <td className="px-1.5 py-1 border-zinc-800/60">{pMid}</td>
                            </>}
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  </div>
                )}
              </div>
            </div>
          </Panel>

          {/* ── Forward Curve collapsible panel ── */}
          <div className="shrink-0 border border-zinc-800 bg-[#1c1b1d]">
            <button
              className="w-full flex items-center justify-between px-2.5 py-1.5 hover:bg-zinc-800/40 transition-colors"
              onClick={() => setShowFwd(v => !v)}
            >
              <div className="flex items-center gap-2">
                <TrendingUp className="w-3 h-3 text-[#adc6ff]" />
                <span className="text-[10px] font-bold tracking-[0.14em] uppercase text-zinc-400">
                  Forward Curve &amp; Futures Prices
                </span>
                {selected && (
                  <span className="font-mono text-[9px] text-zinc-600">— {selected}</span>
                )}
              </div>
              {showForwardCurve
                ? <ChevronUp className="w-3 h-3 text-zinc-500" />
                : <ChevronDown className="w-3 h-3 text-zinc-500" />
              }
            </button>

            {showForwardCurve && (
              <div className="px-2.5 pb-2.5">
                {forwardCurve.length === 0 ? (
                  <div className="flex gap-2 mt-1.5">
                    {Array.from({ length: 4 }).map((_, i) => (
                      <div key={i} className="flex-1 animate-pulse bg-zinc-800/60 h-14 rounded" />
                    ))}
                  </div>
                ) : (
                  <>
                    {/* Futures tiles — first 4 maturities */}
                    <div className="grid grid-cols-4 gap-2 mt-1.5 mb-2">
                      {forwardCurve.slice(0, 4).map(fp => (
                        <div key={fp.expiry} className="border border-zinc-700/60 bg-[#131315] px-2 py-1.5">
                          <div className="text-[9px] font-bold tracking-widest uppercase text-zinc-600">
                            {fp.maturity_days}D · {fp.expiry}
                          </div>
                          <div className="font-mono text-[13px] text-[#adc6ff] mt-0.5">
                            {fp.forward.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}
                          </div>
                        </div>
                      ))}
                    </div>
                    {/* Full forward curve table */}
                    <table className="w-full font-mono text-[11px] border-collapse">
                      <thead>
                        <tr className="border-b border-zinc-800 text-[9px] text-zinc-500 uppercase tracking-widest">
                          <th className="py-1 text-left font-normal">Expiry</th>
                          <th className="py-1 text-right font-normal">Days</th>
                          <th className="py-1 text-right font-normal">Forward (F)</th>
                          <th className="py-1 text-right font-normal">vs Spot</th>
                        </tr>
                      </thead>
                      <tbody>
                        {forwardCurve.map(fp => {
                          const spot = selectedRow?.spot ?? fp.forward;
                          const bps  = ((fp.forward / spot - 1) * 10000).toFixed(0);
                          const pos  = fp.forward >= spot;
                          return (
                            <tr key={fp.expiry} className="border-b border-zinc-800/40 hover:bg-zinc-800/30">
                              <td className="py-1 text-zinc-300">{fp.expiry}</td>
                              <td className="py-1 text-right text-zinc-500">{fp.maturity_days}</td>
                              <td className="py-1 text-right text-[#adc6ff]">
                                {fp.forward.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}
                              </td>
                              <td className={`py-1 text-right ${pos ? "text-emerald-400" : "text-[#ffb4ab]"}`}>
                                {pos ? "+" : ""}{bps}bp
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>

                    {/* ATM vol term structure chart */}
                    {atmTermStructure.length > 0 && (
                      <div className="mt-3 border-t border-zinc-800/60 pt-2">
                        <div className="text-[9px] font-bold tracking-widest uppercase text-zinc-500 mb-1.5">
                          ATM Vol Term Structure
                        </div>
                        <ResponsiveContainer width="100%" height={90}>
                          <LineChart data={atmTermStructure} margin={{ top: 4, right: 8, bottom: 0, left: -20 }}>
                            <XAxis dataKey="maturity" tick={{ fontSize: 9, fill: "#71717a" }} />
                            <YAxis tick={{ fontSize: 9, fill: "#71717a" }} tickFormatter={v => v.toFixed(1) + "%"} />
                            <Tooltip
                              contentStyle={{ background: "#1c1b1d", border: "1px solid #3f3f46", fontSize: 10 }}
                              formatter={(v: number) => [v.toFixed(2) + "%", "ATM Vol"]}
                              cursor={{ stroke: "#adc6ff40", strokeWidth: 1, strokeDasharray: "4 2" }}
                            />
                            <Line type="monotone" dataKey="atm_vol" stroke="#adc6ff" strokeWidth={1.5} dot={{ r: 2, fill: "#adc6ff" }} />
                          </LineChart>
                        </ResponsiveContainer>
                      </div>
                    )}
                  </>
                )}
              </div>
            )}
          </div>

          {/* ── Smile — Raw vs Fitted collapsible panel ── */}
          <div className="shrink-0 border border-zinc-800 bg-[#1c1b1d]">
            <button
              className="w-full flex items-center justify-between px-2.5 py-1.5 hover:bg-zinc-800/40 transition-colors"
              onClick={() => setShowSmile(v => !v)}
            >
              <div className="flex items-center gap-2">
                <BarChart2 className="w-3 h-3 text-[#adc6ff]" />
                <span className="text-[10px] font-bold tracking-[0.14em] uppercase text-zinc-400">
                  Smile — Raw vs SVI Fitted
                </span>
                {smileSlice && (
                  <span className="font-mono text-[9px] text-zinc-600">
                    — {selected} · {smileSlice.expiry} · ATM {smileSlice.atm_vol_pct.toFixed(1)}%
                  </span>
                )}
              </div>
              <div className="flex items-center gap-2">
                {showSmile && (
                  <select
                    value={smileExpiry}
                    onChange={e => { e.stopPropagation(); setSmileExpiry(e.target.value); }}
                    onClick={e => e.stopPropagation()}
                    className="bg-[#0e0e10] border border-zinc-700 text-zinc-300 font-mono text-[9px] py-0.5 px-1 focus:outline-none"
                  >
                    {["10D", "1M", "3M", "6M", "12M"].map(t => <option key={t}>{t}</option>)}
                  </select>
                )}
                {showSmile
                  ? <ChevronUp className="w-3 h-3 text-zinc-500" />
                  : <ChevronDown className="w-3 h-3 text-zinc-500" />
                }
              </div>
            </button>

            {showSmile && (
              <div className="px-2.5 pb-3">
                {smileChartData.length === 0 ? (
                  <div className="animate-pulse bg-zinc-800/60 h-32 mt-2 rounded" />
                ) : (
                  <>
                    <div className="flex items-center gap-4 mt-1 mb-1">
                      <span className="flex items-center gap-1 text-[9px] text-zinc-500">
                        <span className="inline-block w-4 border-t border-dashed border-[#ffb4ab]" />
                        Raw market quotes
                      </span>
                      <span className="flex items-center gap-1 text-[9px] text-zinc-500">
                        <span className="inline-block w-4 border-t-2 border-[#adc6ff]" />
                        SVI fitted
                      </span>
                      {smileSlice && (
                        <span className="text-[9px] text-zinc-600 ml-auto">
                          ATM @ {smileSlice.atm_strike.toLocaleString()}
                        </span>
                      )}
                    </div>
                    <ResponsiveContainer width="100%" height={140}>
                      <LineChart data={smileChartData} margin={{ top: 4, right: 8, bottom: 0, left: -10 }}>
                        <XAxis
                          dataKey="strike"
                          tick={{ fontSize: 9, fill: "#71717a" }}
                          tickFormatter={v => v.toLocaleString()}
                        />
                        <YAxis
                          tick={{ fontSize: 9, fill: "#71717a" }}
                          tickFormatter={v => v.toFixed(0) + "%"}
                        />
                        <Tooltip
                          contentStyle={{ background: "#1c1b1d", border: "1px solid #3f3f46", fontSize: 10 }}
                          formatter={(v: number, name: string) => [
                            v.toFixed(2) + "%",
                            name === "raw" ? "Raw IV" : "SVI Fitted",
                          ]}
                          cursor={{ stroke: "#adc6ff40", strokeWidth: 1, strokeDasharray: "4 2" }}
                        />
                        <Line
                          type="monotone"
                          dataKey="raw"
                          stroke="#ffb4ab"
                          strokeWidth={1}
                          strokeDasharray="4 2"
                          dot={{ r: 2.5, fill: "#ffb4ab", strokeWidth: 0 }}
                        />
                        <Line
                          type="monotone"
                          dataKey="fitted"
                          stroke="#adc6ff"
                          strokeWidth={1.5}
                          dot={false}
                        />
                      </LineChart>
                    </ResponsiveContainer>
                  </>
                )}
              </div>
            )}
          </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Greeks Heatmap view
// ---------------------------------------------------------------------------

function GreeksHeatmap({
  chain, maxDelta, maxGamma, maxVega, maxTheta,
}: {
  chain: ChainRow[];
  maxDelta: number; maxGamma: number; maxVega: number; maxTheta: number;
}) {
  const cell = (val: number, max: number, color: string) => {
    const intensity = Math.min(1, Math.abs(val) / max);
    return (
      <td
        className="px-2 py-1 font-mono text-[11px] text-right border-r border-zinc-800/40"
        style={{ background: `${color}${Math.round(intensity * 120).toString(16).padStart(2, "0")}` }}
      >
        {val.toFixed(3)}
      </td>
    );
  };
  return (
    <table className="w-full border-collapse font-mono text-[11px]" style={{ minWidth: 640 }}>
      <thead className="sticky top-0 bg-[#1c1b1d] z-10">
        <tr className="border-b border-zinc-800 text-[9px] text-zinc-500 uppercase tracking-widest">
          <th className="px-2 py-1 text-right font-normal border-r border-zinc-800">Strike</th>
          <th className="px-2 py-1 text-right font-normal border-r border-zinc-800">Call IV%</th>
          <th className="px-2 py-1 text-right font-normal border-r border-zinc-800">Δ (Call)</th>
          <th className="px-2 py-1 text-right font-normal border-r border-zinc-800">Γ</th>
          <th className="px-2 py-1 text-right font-normal border-r border-zinc-800">V (Call)</th>
          <th className="px-2 py-1 text-right font-normal border-r border-zinc-800">Θ (Call)</th>
          <th className="px-2 py-1 text-right font-normal border-r border-zinc-800">Put IV%</th>
          <th className="px-2 py-1 text-right font-normal border-r border-zinc-800">Δ (Put)</th>
          <th className="px-2 py-1 text-right font-normal border-r border-zinc-800">V (Put)</th>
          <th className="px-2 py-1 text-right font-normal">Θ (Put)</th>
        </tr>
      </thead>
      <tbody>
        {chain.map(row => (
          <tr key={row.strike} className={`border-b border-zinc-800/40 ${row.atm ? "ring-1 ring-inset ring-emerald-500/40" : ""}`}>
            <td className={`px-2 py-1 text-right font-bold border-r border-zinc-800 ${row.atm ? "text-emerald-400" : "text-zinc-200"}`}>
              {row.strike}
            </td>
            <td className="px-2 py-1 text-right text-[#adc6ff] border-r border-zinc-800/40">
              {row.call_iv.toFixed(2)}
            </td>
            {cell(row.call_delta,  maxDelta, "#adc6ff")}
            {cell(row.call_gamma,  maxGamma, "#4edea3")}
            {cell(row.call_vega,   maxVega,  "#4edea3")}
            {cell(row.call_theta,  maxTheta, "#ff8a80")}
            <td className="px-2 py-1 text-right text-[#adc6ff] border-r border-zinc-800/40">
              {row.put_iv.toFixed(2)}
            </td>
            {cell(Math.abs(row.put_delta), maxDelta, "#adc6ff")}
            {cell(row.put_vega,   maxVega,  "#4edea3")}
            {cell(row.put_theta,  maxTheta, "#ff8a80")}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function QcIcon({ status }: { status: string }) {
  const s = (status ?? "").toLowerCase();
  if (s === "ok" || s === "pass")
    return <CheckCircle2 className="w-3 h-3 inline text-emerald-400" />;
  if (s === "warn" || s === "warning")
    return <AlertTriangle className="w-3 h-3 inline text-yellow-400" />;
  if (s === "fail" || s === "reject")
    return <XCircle className="w-3 h-3 inline text-red-400" />;
  if (s === "synthetic")
    return <span className="font-mono text-[9px] text-zinc-500 tracking-tight">SYN</span>;
  return <span className="text-zinc-600 text-[10px]">—</span>;
}

function Td({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <td className={`px-1.5 py-1 border-r border-zinc-800/60 ${className}`}>{children}</td>;
}

function KPI({
  title, children, Icon, iconClass = "text-zinc-500",
}: {
  title: string;
  children: React.ReactNode;
  Icon: React.ComponentType<{ className?: string }>;
  iconClass?: string;
}) {
  return (
    <div className="border border-zinc-800 bg-[#1c1b1d] px-2.5 py-2 flex flex-col gap-1.5 hover:border-zinc-700">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-bold tracking-[0.14em] uppercase text-zinc-500">{title}</span>
        <Icon className={`w-3.5 h-3.5 ${iconClass}`} />
      </div>
      {children}
    </div>
  );
}

function Metric({
  label, value, valueClass = "text-zinc-200",
}: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="flex flex-col leading-tight px-2 border-l border-zinc-800">
      <span className="text-[9px] font-bold tracking-widest text-zinc-500 uppercase">{label}</span>
      <span className={`font-mono text-[12px] ${valueClass}`}>{value}</span>
    </div>
  );
}

function MetricBox({
  label, value, valueClass = "text-zinc-100",
}: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="border border-zinc-800 bg-[#1c1b1d] px-2.5 py-2">
      <div className="text-[9px] font-bold tracking-[0.14em] uppercase text-zinc-500 mb-1">{label}</div>
      <div className={`font-mono text-[14px] ${valueClass}`}>{value}</div>
    </div>
  );
}
