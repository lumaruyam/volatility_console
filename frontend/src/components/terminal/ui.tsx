import type { ReactNode } from "react";

/** Dense bordered panel. Title row uses the surface-container header treatment from the reference. */
export function Panel({
  title, icon, right, children, className = "", padded = true, headerClass = "",
}: {
  title?: ReactNode; icon?: ReactNode; right?: ReactNode; children: ReactNode;
  className?: string; padded?: boolean; headerClass?: string;
}) {
  return (
    <section className={`border border-zinc-800 bg-[#131315] flex flex-col min-w-0 overflow-hidden ${className}`}>
      {title && (
        <header className={`flex items-center justify-between gap-2 px-2.5 py-1.5 border-b border-zinc-800 bg-[#1c1b1d] ${headerClass}`}>
          <h3 className="text-[10px] font-bold tracking-[0.14em] uppercase text-zinc-300 flex items-center gap-1.5">
            {icon}
            {title}
          </h3>
          {right && <div className="flex items-center gap-2 text-[10px] text-zinc-500 font-mono">{right}</div>}
        </header>
      )}
      <div className={padded ? "p-2.5 flex-1 min-w-0" : "flex-1 min-w-0"}>{children}</div>
    </section>
  );
}

export function Label({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <span className={`text-[10px] font-bold tracking-[0.14em] uppercase text-zinc-500 ${className}`}>{children}</span>;
}

export function StatusPill({
  tone = "ok", children,
}: { tone?: "ok" | "warn" | "fail" | "neutral" | "info"; children: ReactNode }) {
  const toneMap = {
    ok: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30",
    warn: "bg-amber-500/10 text-amber-400 border-amber-500/30",
    fail: "bg-red-500/10 text-red-400 border-red-500/40",
    neutral: "bg-zinc-800 text-zinc-300 border-zinc-700",
    info: "bg-[#adc6ff]/10 text-[#adc6ff] border-[#adc6ff]/30",
  } as const;
  return (
    <span className={`px-1.5 py-[1px] rounded text-[9px] font-bold tracking-[0.16em] uppercase border ${toneMap[tone]}`}>
      {children}
    </span>
  );
}

export function Chip({
  active, children, onClick,
}: { active?: boolean; children: ReactNode; onClick?: () => void }) {
  return (
    <button
      onClick={onClick}
      className={[
        "px-2 py-1 text-[11px] font-mono border transition-colors",
        active
          ? "border-[#adc6ff] bg-[#adc6ff]/15 text-[#adc6ff]"
          : "border-zinc-800 bg-[#1c1b1d] text-zinc-300 hover:border-zinc-600",
      ].join(" ")}
    >
      {children}
    </button>
  );
}
