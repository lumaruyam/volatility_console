import { useMemo, useState } from "react";
import {
  Database, ShieldAlert, ArrowLeftRight, History, SlidersHorizontal,
  Terminal as TerminalIcon, HelpCircle, Settings, Bell, Search, CheckCircle2, Gauge,
  ClipboardList,
} from "lucide-react";
import { DataOverview } from "./views/DataOverview";
import { RiskAnalysis } from "./views/RiskAnalysis";
import { StrategyExecution } from "./views/StrategyExecution";
import { Backtesting } from "./views/Backtesting";
import { ShockSimulator } from "./views/ShockSimulator";
import { Orders } from "./views/Orders";

export type ViewKey = "data" | "risk" | "strategy" | "backtest" | "shock" | "orders";

const NAV: { key: ViewKey; label: string; short: string; Icon: typeof Database }[] = [
  { key: "data",     label: "Data Overview",      short: "DATA",  Icon: Database },
  { key: "risk",     label: "Risk Analysis",      short: "RISK",  Icon: ShieldAlert },
  { key: "strategy", label: "Strategy Execution", short: "EXEC",  Icon: ArrowLeftRight },
  { key: "backtest", label: "Backtesting",        short: "BACK",  Icon: History },
  { key: "shock",    label: "Shock Simulator",    short: "SHOCK", Icon: SlidersHorizontal },
  { key: "orders",   label: "Orders",             short: "OMS",   Icon: ClipboardList },
];

export function TerminalShell() {
  const [view, setView] = useState<ViewKey>("data");

  const active = useMemo(() => {
    switch (view) {
      case "data":     return <DataOverview />;
      case "risk":     return <RiskAnalysis />;
      case "strategy": return <StrategyExecution />;
      case "backtest": return <Backtesting />;
      case "shock":    return <ShockSimulator />;
      case "orders":   return <Orders />;
    }
  }, [view]);

  return (
    <div className="dark h-screen w-full overflow-hidden bg-[#09090b] text-zinc-200 flex flex-col">
      {/* Top header */}
      <header className="h-12 shrink-0 flex items-center gap-4 px-4 border-b border-zinc-800 bg-[#09090b] z-30">
        <div className="flex items-baseline gap-3">
          <span className="font-mono text-[16px] font-bold tracking-tighter text-[#adc6ff] leading-none">
            VOLATILITY_CONSOLE.v1
          </span>
        </div>
        <div className="h-4 w-px bg-zinc-800" />
        <div className="flex items-center gap-1.5 px-2.5 py-1 border border-emerald-500/30 bg-emerald-500/5 rounded-sm">
          <History className="w-3 h-3 text-emerald-400" />
          <span className="font-mono text-[11px] text-emerald-300 tracking-wider">REPLAY FRAME: 2026-06-14 16:30:00 UTC</span>
        </div>

        <div className="ml-auto relative hidden md:block">
          <Search className="w-3.5 h-3.5 absolute left-2 top-1/2 -translate-y-1/2 text-zinc-600" />
          <input
            placeholder="CMD / SEARCH..."
            className="bg-[#131315] border border-zinc-800 font-mono text-[11px] pl-7 pr-2 py-1 h-7 w-56 focus:outline-none focus:border-[#adc6ff] placeholder:text-zinc-700"
          />
        </div>
        <div className="flex items-center gap-1 text-zinc-500">
          <IconBtn><Settings className="w-4 h-4" /></IconBtn>
          <IconBtn>
            <span className="relative">
              <Bell className="w-4 h-4" />
              <span className="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 bg-[#ffb4ab] rounded-full" />
            </span>
          </IconBtn>
        </div>
      </header>

      <div className="flex flex-1 min-h-0 overflow-hidden">
        {/* Sidebar */}
        <aside className="w-14 shrink-0 border-r border-zinc-800 bg-[#0e0e10] flex flex-col items-center pt-6 pb-2 gap-1">

          {NAV.map(({ key, label, short, Icon }) => {
            const isActive = view === key;
            return (
              <button
                key={key}
                title={label}
                onClick={() => setView(key)}
                className={[
                  "group relative w-12 h-12 flex flex-col items-center justify-center gap-0.5 transition-colors",
                  isActive
                    ? "bg-[#adc6ff]/10 text-[#adc6ff] border-l-2 border-[#adc6ff]"
                    : "text-zinc-500 hover:text-zinc-100 hover:bg-zinc-900 border-l-2 border-transparent",
                ].join(" ")}
              >
                <Icon className="w-[18px] h-[18px]" strokeWidth={1.5} />
                <span className="text-[8px] font-bold tracking-[0.18em]">{short}</span>
                <span className="pointer-events-none absolute left-full ml-2 z-50 whitespace-nowrap rounded bg-[#1c1b1d] border border-zinc-800 px-2 py-1 text-[10px] text-zinc-300 opacity-0 group-hover:opacity-100">
                  {label}
                </span>
              </button>
            );
          })}

          <div className="mt-auto flex flex-col items-center gap-1 pt-2 border-t border-zinc-800 w-full">
            <SideUtil title="Terminal" Icon={TerminalIcon} />
            <SideUtil title="Help" Icon={HelpCircle} />
            <SideUtil title="System Stable" Icon={CheckCircle2} tone="emerald" />
            <SideUtil title="Latency 4ms" Icon={Gauge} />
          </div>
        </aside>

        {/* View */}
        <main className="flex-1 min-w-0 overflow-hidden bg-[#09090b]">
          {active}
        </main>
      </div>
    </div>
  );
}

function IconBtn({ children }: { children: React.ReactNode }) {
  return <button className="w-7 h-7 flex items-center justify-center hover:bg-zinc-900 hover:text-zinc-200 rounded-sm">{children}</button>;
}

function SideUtil({ title, Icon, tone }: { title: string; Icon: typeof Database; tone?: "emerald" }) {
  return (
    <button
      title={title}
      className={[
        "w-10 h-8 flex items-center justify-center hover:bg-zinc-900",
        tone === "emerald" ? "text-emerald-400" : "text-zinc-500 hover:text-zinc-200",
      ].join(" ")}
    >
      <Icon className="w-4 h-4" strokeWidth={1.5} />
    </button>
  );
}
