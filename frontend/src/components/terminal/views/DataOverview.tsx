import { useState, useRef, useEffect, useCallback, lazy, Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ChevronDown, ChevronRight, Database, Cpu, Radio,
  TrendingUp, CheckCheck, CheckCircle2, ZoomIn, ChevronsUpDown,
} from "lucide-react";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
} from "recharts";
import { Panel, StatusPill } from "../ui";

// Three.js / R3F must never run on the server; lazy import keeps it client-only.
const VolSurface3D = lazy(() => import("./VolSurface3D"));

// ---------------------------------------------------------------------------
// API response types
// ---------------------------------------------------------------------------

type IndexRow   = { ticker: string; name: string; spot: number; atm_vol: number };
type ChainRow   = {
  strike: number;
  call_bid: number; call_ask: number; call_iv: number;
  call_delta: number; call_gamma: number; call_vega: number; call_theta: number; call_qc: string;
  put_bid: number; put_ask: number; put_iv: number;
  put_delta: number; put_gamma: number; put_vega: number; put_theta: number; put_qc: string;
  atm: boolean;
};
type EngineStatus = {
  spot_ingestion: { status: string; latency_ms: number };
  forward_curve:  { id: string; tenor: string };
  calibration:    { rmse: number; status: string };
  engine_load_pct: number;
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
};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SIDEBAR_MIN = 220;
const SIDEBAR_MAX = 450;
const SIDEBAR_DEFAULT = 240;

const EXPIRY_OPTIONS = [
  { value: "2026-12-15", label: "2026-12-15 (30D)" },
  { value: "2027-01-19", label: "2027-01-19 (65D)" },
  { value: "2027-03-21", label: "2027-03-21 (120D)" },
];

// Shown while index-matrix is loading — hardcoded Euro Stoxx 50 snapshot
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
// Component
// ---------------------------------------------------------------------------

export function DataOverview() {
  const [selected, setSelected] = useState("ASML");
  const [expiry, setExpiry]     = useState("2026-12-15");

  // ── Sidebar resize ────────────────────────────────────────────────────────
  const [sidebarW, setSidebarW] = useState(SIDEBAR_DEFAULT);
  const dragActive = useRef(false);
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
      const next = Math.min(
        SIDEBAR_MAX,
        Math.max(SIDEBAR_MIN, dragOriginW.current + e.clientX - dragOriginX.current),
      );
      setSidebarW(next);
    };
    const onUp = () => {
      if (!dragActive.current) return;
      dragActive.current             = false;
      document.body.style.cursor     = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup",   onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup",   onUp);
    };
  }, []);
  // ─────────────────────────────────────────────────────────────────────────

  // --- Queries ---
  const { data: indexData = LOADING_INDEX } = useQuery<IndexRow[]>({
    queryKey: ["index-matrix"],
    queryFn:  () => fetch("/api/market/index-matrix").then(r => r.json()),
    staleTime: 30_000,
  });

  const { data: chainData = [] } = useQuery<ChainRow[]>({
    queryKey: ["options-chain", selected, expiry],
    queryFn:  () =>
      fetch(`/api/market/options-chain?ticker=${selected}&expiry=${expiry}`)
        .then(r => r.json()),
    staleTime: 15_000,
  });

  const { data: status } = useQuery<EngineStatus>({
    queryKey: ["engine-status"],
    queryFn:  () => fetch("/api/market/engine-status").then(r => r.json()),
    staleTime: 5_000,
    refetchInterval: 30_000,
  });

  const { data: surface } = useQuery<VolSurface>({
    queryKey: ["vol-surface", selected],
    queryFn:  () =>
      fetch(`/api/market/vol-surface?ticker=${selected}`).then(r => r.json()),
    staleTime: 30_000,
  });

  const { data: greeks } = useQuery<GreeksSummary>({
    queryKey: ["greeks-summary", selected],
    queryFn:  () =>
      fetch(`/api/market/greeks-summary?ticker=${selected}`).then(r => r.json()),
    staleTime: 30_000,
  });

  // --- Derived display data ---
  const smileData = surface?.smile_slice_30d
    ? surface.smile_slice_30d.strikes.map((k, i) => ({
        strike: k,
        callIV: +(surface.smile_slice_30d.call_ivs[i] * 100).toFixed(2),
        putIV:  +(surface.smile_slice_30d.put_ivs[i] * 100).toFixed(2),
      }))
    : [];

  const calArb      = surface?.smile_slice_30d?.cal_arb  ?? "clear";
  const bflyArb     = surface?.smile_slice_30d?.bfly_arb ?? "clear";
  const calibStatus = surface?.calibration.status ?? "pending";
  const calibModel  = surface?.calibration.model  ?? "SVI Spline";
  const calibRmse   = surface?.calibration.rmse   ?? 0.0;
  const isQcPass    = calibStatus === "converged";

  const selectedRow = indexData.find(r => r.ticker === selected);
  const refSpot = selectedRow
    ? selectedRow.spot.toLocaleString(undefined, { maximumFractionDigits: 1 })
    : "—";

  return (
    <div className="flex h-full min-h-0 overflow-hidden">
      {/* EURO STOXX 50 sidebar — width is pointer-draggable */}
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
              {indexData.map(r => {
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
              })}
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
        {/* Highlight strip — visible on hover and during active drag */}
        <div className="absolute inset-0 transition-colors duration-100 group-hover:bg-[#adc6ff]/50" />
        {/* Three-dot grip centred on the handle */}
        <div className="absolute inset-y-0 left-1/2 -translate-x-1/2 flex flex-col items-center justify-center gap-[3px] opacity-0 group-hover:opacity-100 transition-opacity duration-100">
          {[0, 1, 2].map(i => (
            <div key={i} className="w-[3px] h-[3px] rounded-full bg-[#adc6ff]/80" />
          ))}
        </div>
      </div>
      {/* ─────────────────────────────────────────────────────────────────── */}

      {/* Main grid */}
      <div className="flex-1 min-w-0 overflow-hidden flex flex-col">
        {/* Context header */}
        <div className="h-10 shrink-0 border-b border-zinc-800 bg-[#0e0e10] px-3 flex items-center gap-3">
          <Database className="w-4 h-4 text-[#adc6ff]" />
          <span className="text-[10px] font-bold tracking-[0.18em] uppercase text-zinc-300">MARKET DATA &amp; SNAPSHOTS</span>
          <ChevronRight className="w-3 h-3 text-zinc-600" />
          <span className="font-mono text-[11px] text-zinc-500">{selected}</span>

          <div className="ml-auto flex items-center gap-2">
            <button className="flex items-center gap-1 px-2 py-1 border border-zinc-800 bg-[#1c1b1d] hover:bg-zinc-800">
              <span className="font-mono text-[12px] text-[#adc6ff]">{selected}</span>
              <ChevronDown className="w-3 h-3 text-zinc-500" />
            </button>
            <Metric label="REF SPOT (S0)" value={refSpot} />
            <Metric label="RATE (r)" value="3.45%" valueClass="text-emerald-400" />
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

        <div className="flex-1 min-h-0 p-2.5 flex flex-col gap-2.5 overflow-hidden">
          {/* KPI strip — wired to /api/market/engine-status */}
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
                  <div
                    className="h-full bg-[#adc6ff]"
                    style={{ width: `${status?.engine_load_pct ?? 42}%` }}
                  />
                </div>
                <span className="font-mono text-[11px] text-zinc-500">
                  {status?.engine_load_pct ?? 42}% LOAD
                </span>
              </div>
            </KPI>
          </div>

          {/* Vol surface + 2D smile */}
          <div className="grid grid-cols-12 gap-2.5 shrink-0" style={{ height: 280 }}>
            <Panel
              className="col-span-8"
              title="Vol Surface 3D — Strike / Maturity / IV%"
              right={<>
                <span className="text-zinc-600">z = IV%</span>
                <span className="pl-2 border-l border-zinc-800">x = STRIKE</span>
                <span className="pl-2 border-l border-zinc-800">y = MATURITY</span>
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
                      <XAxis
                        dataKey="strike"
                        tick={{ fontSize: 9, fill: "#71717a" }}
                        tickLine={false}
                      />
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

          {/* Advanced Metrics Grid — wired to /api/market/greeks-summary */}
          {greeks && (
            <div className="grid grid-cols-4 gap-2.5 shrink-0">
              <MetricBox label="PORTFOLIO Δ"  value={greeks.total_delta.toFixed(4)} />
              <MetricBox label="PORTFOLIO Γ"  value={greeks.total_gamma.toFixed(4)} />
              <MetricBox label="PORTFOLIO V"  value={"€" + greeks.total_vega.toLocaleString(undefined, { maximumFractionDigits: 0 })} />
              <MetricBox label="PORTFOLIO Θ"  value={"€" + greeks.total_theta.toLocaleString(undefined, { maximumFractionDigits: 0 })} valueClass="text-[#ffb4ab]" />
            </div>
          )}

          {/* Options chain — wired to /api/market/options-chain */}
          <Panel
            title="Centered-Strike Straddle Options Chain"
            padded={false}
            className="flex-1 min-h-0"
            right={<>
              <span>EXPIRY:</span>
              <select
                value={expiry}
                onChange={e => setExpiry(e.target.value)}
                className="bg-[#131315] border border-zinc-800 px-1 py-[1px] font-mono text-[11px] focus:outline-none focus:border-[#adc6ff]"
              >
                {EXPIRY_OPTIONS.map(o => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </>}
          >
            <div className="overflow-auto h-full vc-scroll">
              {chainData.length === 0 ? (
                <div className="flex items-center justify-center h-24">
                  <span className="font-mono text-[10px] text-zinc-600">LOADING CHAIN…</span>
                </div>
              ) : (
                <table className="w-full border-collapse text-right font-mono">
                  <thead className="sticky top-0 bg-[#1c1b1d] z-10">
                    <tr className="border-b border-zinc-800">
                      <th colSpan={7} className="py-1 text-center text-[10px] font-bold tracking-[0.14em] text-zinc-500 uppercase border-r border-zinc-800">CALLS</th>
                      <th className="py-1 text-center text-[10px] font-bold tracking-[0.14em] text-zinc-200 uppercase border-r border-zinc-800 bg-[#2a2a2c]/60">STRIKE</th>
                      <th colSpan={7} className="py-1 text-center text-[10px] font-bold tracking-[0.14em] text-zinc-500 uppercase">PUTS</th>
                    </tr>
                    <tr className="border-b border-zinc-800 text-[10px] text-zinc-500">
                      {["Mid","IV%","Δ","Γ","V","Θ","QC"].map((h, i) => (
                        <th key={"c"+i} className={`px-2 py-1 font-normal border-r border-zinc-800/60 ${h==="IV%" ? "text-[#adc6ff]" : ""}`}>{h}</th>
                      ))}
                      <th className="px-2 py-1 font-bold text-zinc-200 bg-[#2a2a2c]/40 border-r border-zinc-800">K</th>
                      {["QC","IV%","Mid","Δ","Γ","V","Θ"].map((h, i) => (
                        <th key={"p"+i} className={`px-2 py-1 font-normal border-r border-zinc-800/60 last:border-r-0 ${h==="IV%" ? "text-[#adc6ff]" : ""}`}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="text-[11px]">
                    {chainData.map(row => {
                      const atm    = row.atm;
                      const rowCls = atm
                        ? "border-y-2 border-emerald-500/50 bg-emerald-500/5 font-bold"
                        : "border-b border-zinc-800/60 hover:bg-zinc-800/40";
                      const cMid = ((row.call_bid + row.call_ask) / 2).toFixed(1);
                      const pMid = ((row.put_bid  + row.put_ask)  / 2).toFixed(1);
                      return (
                        <tr key={row.strike} className={rowCls}>
                          <Td>{cMid}</Td>
                          <Td className={atm ? "text-emerald-400" : "text-[#adc6ff]"}>{row.call_iv.toFixed(2)}</Td>
                          <Td>{row.call_delta.toFixed(2)}</Td>
                          <Td>{row.call_gamma.toFixed(4)}</Td>
                          <Td>{row.call_vega.toFixed(1)}</Td>
                          <Td className="text-[#ffb4ab]">{row.call_theta.toFixed(1)}</Td>
                          <td className="px-2 py-1 text-center text-emerald-400 border-r border-zinc-800/60">
                            <CheckCircle2 className="w-3 h-3 inline" />
                          </td>
                          <td className={`px-2 py-1 text-center font-bold border-r border-zinc-800 ${atm ? "text-emerald-400 bg-emerald-500/10" : "text-zinc-200 bg-[#2a2a2c]/40"}`}>
                            {row.strike}
                          </td>
                          <td className="px-2 py-1 text-center text-emerald-400 border-r border-zinc-800/60">
                            <CheckCircle2 className="w-3 h-3 inline" />
                          </td>
                          <Td className={atm ? "text-emerald-400" : "text-[#adc6ff]"}>{row.put_iv.toFixed(2)}</Td>
                          <Td>{pMid}</Td>
                          <Td>{row.put_delta.toFixed(2)}</Td>
                          <Td>{row.put_gamma.toFixed(4)}</Td>
                          <Td>{row.put_vega.toFixed(1)}</Td>
                          <Td className="text-[#ffb4ab]">{row.put_theta.toFixed(1)}</Td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function Td({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <td className={`px-2 py-1 border-r border-zinc-800/60 ${className}`}>{children}</td>;
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
