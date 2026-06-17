import { createFileRoute } from "@tanstack/react-router";
import { TerminalShell } from "@/components/terminal/TerminalShell";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Volatility Console — Options Trading Terminal" },
      { name: "description", content: "Institutional multi-tab volatility & options trading terminal with replay, risk, strategy, backtest and shock simulation." },
      { property: "og:title", content: "Volatility Console" },
      { property: "og:description", content: "Institutional options & volatility terminal." },
    ],
  }),
  component: () => <TerminalShell />,
});
