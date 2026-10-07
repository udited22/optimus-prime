// The cognitive connectome's anatomy (docs/architecture/observability.md §17.5, v3.2): six cognitive regions plus Tony's integration core
// and the external broker port (macro), and inside them the neural assemblies (meso) that real events address:
// one per strategy, one per family of Governor checks, one per execution process, and so on. Every topology node
// belongs to exactly one region, so any real flow on the stream (a [from, to, kind] triple on a declared edge)
// becomes an axonal pathway between two regions. Nothing here is decorative state.

export type RegionId = "PERCEPTION" | "STRATEGY" | "RISK" | "PORTFOLIO" | "EXECUTION" | "RESEARCH" | "CORE" | "BROKER";
export const REGIONS: RegionId[] = ["PERCEPTION", "STRATEGY", "RISK", "PORTFOLIO", "EXECUTION", "RESEARCH", "CORE", "BROKER"];
/** the six regions the brief names (CORE is Tony; BROKER is outside the organism) */
export const COGNITIVE: RegionId[] = ["PERCEPTION", "STRATEGY", "RISK", "PORTFOLIO", "EXECUTION", "RESEARCH"];
export const REGION_INDEX: Record<RegionId, number> = Object.fromEntries(REGIONS.map((r, i) => [r, i])) as Record<RegionId, number>;

export interface RegionInfo {
  title: string;
  /** what the region is, in one line (hover) */
  role: string;
  /** which topology nodes it contains */
  nodes: string;
  /** the operational layer a click opens */
  layer: string;
}

export const REGION_INFO: Record<RegionId, RegionInfo> = {
  PERCEPTION: { title: "Market perception", role: "Ticks in, data-quality gate, regime tags out", nodes: "Market Intelligence · Data Quality", layer: "Signals and data" },
  STRATEGY: { title: "Strategy", role: "Each strategy is a neural assembly here; ideas compete for attention. Brightness is activity, not expected profit", nodes: "the five strategies", layer: "Active hypotheses" },
  RISK: { title: "Risk", role: "The Risk Governor: every intent is approved or rejected here", nodes: "Risk Governor", layer: "Constraints and rejection rationale" },
  PORTFOLIO: { title: "Portfolio", role: "Sizes and routes intents (1 lot max); holds the open position", nodes: "Portfolio Allocator", layer: "Allocation" },
  EXECUTION: { title: "Execution", role: "The only path to the broker; places orders and the protective stop", nodes: "Execution Gateway", layer: "Orders and broker state" },
  RESEARCH: { title: "Learning / research", role: "Post-trade evidence, validation and the strategy factory", nodes: "Post-trade · Validation · Strategy Factory", layer: "Experiments and candidate strategies" },
  CORE: { title: "Tony · Trading CIO", role: "Sets the day's posture and budgets; explains itself", nodes: "CIO (Tony)", layer: "Tony's brief" },
  BROKER: { title: "Broker (fake, SIMULATED)", role: "No real broker is connected", nodes: "fake broker", layer: "Orders and broker state" },
};

/** topology node → region (strategy nodes are `strat:<id>`) */
export function regionOfNode(node: string): RegionId | null {
  if (node.startsWith("strat:")) return "STRATEGY";
  switch (node) {
    case "market_intel":
    case "data_quality":
      return "PERCEPTION";
    case "cio":
      return "CORE";
    case "allocator":
      return "PORTFOLIO";
    case "risk_governor":
      return "RISK";
    case "execution":
      return "EXECUTION";
    case "broker":
      return "BROKER";
    case "post_trade":
    case "validation":
    case "strategy_factory":
      return "RESEARCH";
    default:
      return null;
  }
}

/** which region a latched kill switch belongs to: only that region changes state (the brief: "the affected region
 *  changes state rather than making the entire UI flash red"). MANUAL_MASTER_KILL and SYSTEM_INTEGRITY_KILL stop
 *  the whole kernel, so they mark Tony's core, not every region. */
export const KILL_REGION: Record<string, RegionId> = {
  STRATEGY_KILL: "STRATEGY",
  PORTFOLIO_KILL: "PORTFOLIO",
  DAILY_LOSS_KILL: "RISK",
  DATA_QUALITY_KILL: "PERCEPTION",
  BROKER_CONNECTIVITY_KILL: "EXECUTION",
  ABNORMAL_MARKET_KILL: "PERCEPTION",
  POSITION_RECONCILIATION_KILL: "EXECUTION",
  SYSTEM_INTEGRITY_KILL: "CORE",
  MANUAL_MASTER_KILL: "CORE",
};

/** semantic tones (the brief): cyan perception/system, violet strategy/reasoning, amber evaluation/uncertainty,
 *  green approved/executed, red rejected/intervention; white is neutral */
export type Tone = "cyan" | "violet" | "amber" | "green" | "red" | "white";
export const TONE_RGB: Record<Tone, [number, number, number]> = {
  cyan: [0.49, 0.83, 0.97],
  violet: [0.66, 0.61, 0.95],
  amber: [0.94, 0.71, 0.3],
  green: [0.4, 0.84, 0.6],
  red: [0.95, 0.38, 0.42],
  white: [0.86, 0.9, 0.95],
};

// ---------------------------------------------------------------- meso: neural assemblies
export interface AssemblySpec { id: string; region: RegionId; title: string }
export const MAX_STRATEGIES = 8;
/** the fixed assemblies; strategy assemblies (`S:<id>`) are inserted after PERCEPTION's */
const FIXED: AssemblySpec[] = [
  { id: "P.FEED", region: "PERCEPTION", title: "Tick feed" },
  { id: "P.DQ", region: "PERCEPTION", title: "Data-quality gate" },
  { id: "P.REGIME", region: "PERCEPTION", title: "Regime classifier" },
  { id: "P.VOL", region: "PERCEPTION", title: "Volatility / VIX" },
  { id: "P.CHAIN", region: "PERCEPTION", title: "Option chain" },
  { id: "R.MANDATE", region: "RISK", title: "Mandate and order validity" },
  { id: "R.BUDGET", region: "RISK", title: "Per-trade risk budget" },
  { id: "R.HEADROOM", region: "RISK", title: "Loss headroom and buying power" },
  { id: "R.POSITION", region: "RISK", title: "Position, trade count and cool-down" },
  { id: "R.WINDOW", region: "RISK", title: "Trading window" },
  { id: "R.MARKET", region: "RISK", title: "Quote and liquidity checks" },
  { id: "R.KILLS", region: "RISK", title: "Kill switches and halts" },
  { id: "PF.SIZING", region: "PORTFOLIO", title: "Sizing (allocator)" },
  { id: "PF.BUDGET", region: "PORTFOLIO", title: "Day budget" },
  { id: "PF.POSITION", region: "PORTFOLIO", title: "Open position" },
  { id: "E.ENTRY", region: "EXECUTION", title: "Entry orders" },
  { id: "E.STOP", region: "EXECUTION", title: "Protective stop" },
  { id: "E.EXIT", region: "EXECUTION", title: "Exit orders" },
  { id: "E.LINK", region: "EXECUTION", title: "Broker link and reconciliation" },
  { id: "RS.POST", region: "RESEARCH", title: "Post-trade evidence" },
  { id: "RS.VALID", region: "RESEARCH", title: "Validation" },
  { id: "RS.FACTORY", region: "RESEARCH", title: "Strategy factory" },
  { id: "RS.ECON", region: "RESEARCH", title: "Whole-system economics" },
  { id: "C.POSTURE", region: "CORE", title: "Posture (regime → stance)" },
  { id: "C.BUDGET", region: "CORE", title: "Budgets" },
  { id: "C.ATTN", region: "CORE", title: "Attention" },
  { id: "C.VOICE", region: "CORE", title: "Explanation" },
  { id: "B.PORT", region: "BROKER", title: "Fake broker port" },
];
export const strategyAssembly = (id: string): string => `S:${id}`;
export function assemblies(strategyIds: string[]): AssemblySpec[] {
  const s = strategyIds.slice(0, MAX_STRATEGIES).map((id) => ({ id: strategyAssembly(id), region: "STRATEGY" as RegionId, title: id }));
  const i = FIXED.findIndex((a) => a.region !== "PERCEPTION");
  return [...FIXED.slice(0, i), ...s, ...FIXED.slice(i)];
}
export const MAX_ASSEMBLIES = FIXED.length + MAX_STRATEGIES;
/** the region an assembly id lives in */
export function assemblyRegion(id: string): RegionId | undefined {
  return id.startsWith("S:") ? "STRATEGY" : FIXED.find((a) => a.id === id)?.region;
}

/** Governor reason code → the gating assembly inside RISK that produced it (kernel/governor.py `Reason`) */
export const REASON_ASSEMBLY: Record<string, string> = {
  MANDATE_LONG_ONLY: "R.MANDATE", MANDATE_UNDERLYING: "R.MANDATE", MANDATE_INSTRUMENT_TYPE: "R.MANDATE", INVALID_QUANTITY: "R.MANDATE",
  INSTRUMENT_NOT_TRADEABLE: "R.MANDATE", NO_PROTECTIVE_EXIT: "R.MANDATE", STOP_INVALID: "R.MANDATE", STOP_WIDENED: "R.MANDATE", TICK_SIZE: "R.MANDATE",
  SMALLEST_LOT_EXCEEDS_BUDGET: "R.BUDGET", RISK_BUDGET_EXCEEDED: "R.BUDGET", COST_MODEL_UNAVAILABLE: "R.BUDGET",
  DAILY_HEADROOM: "R.HEADROOM", WEEKLY_HEADROOM: "R.HEADROOM", DRAWDOWN_HEADROOM: "R.HEADROOM", INSUFFICIENT_BUYING_POWER: "R.HEADROOM", NAV_UNKNOWN: "R.HEADROOM",
  MAX_POSITION: "R.POSITION", NO_AVERAGING: "R.POSITION", NOT_A_STRADDLE: "R.POSITION", OPEN_ORDER_LIMIT: "R.POSITION", MARTINGALE: "R.POSITION", MAX_TRADES_PER_DAY: "R.POSITION", STRATEGY_MAX_ENTRIES: "R.POSITION", COOLDOWN: "R.POSITION",
  OUTSIDE_TRADING_WINDOW: "R.WINDOW", ENTRY_WINDOW_CLOSED: "R.WINDOW", EVENT_DAY_NOT_CERTIFIED: "R.WINDOW", TICKET_EXPIRED: "R.WINDOW",
  NO_QUOTE: "R.MARKET", STALE_QUOTE: "R.MARKET", LOW_LIQUIDITY: "R.MARKET", PRICE_SANITY: "R.MARKET", ABNORMAL_MARKET: "R.MARKET",
  REGIME_UNKNOWN: "R.MARKET", REGIME_STALE: "R.MARKET", REGIME_BLOCKED: "R.MARKET", REGIME_NOT_ALLOWED: "R.MARKET",
  KILL_ACTIVE: "R.KILLS", STRATEGY_KILLED: "R.KILLS", HALT_ACTIVE: "R.KILLS", ALL_TRADING_HALTED: "R.KILLS", STRATEGY_STATUS: "R.KILLS", SYSTEM_INTEGRITY_FAILURE: "R.KILLS",
};
/** kill switch → the assembly that carries it inside its home region (KILL_REGION) */
export const KILL_ASSEMBLY: Record<string, string> = {
  PORTFOLIO_KILL: "PF.BUDGET", DAILY_LOSS_KILL: "R.HEADROOM", DATA_QUALITY_KILL: "P.DQ", BROKER_CONNECTIVITY_KILL: "E.LINK",
  ABNORMAL_MARKET_KILL: "P.VOL", POSITION_RECONCILIATION_KILL: "E.LINK", SYSTEM_INTEGRITY_KILL: "C.ATTN", MANUAL_MASTER_KILL: "C.ATTN",
};

/** short human phrases for Governor reason codes, used only when the decision has no `explain` text */
export const REASON_SHORT: Record<string, string> = {
  SMALLEST_LOT_EXCEEDS_BUDGET: "smallest lot exceeds the per-trade budget",
  RISK_BUDGET_EXCEEDED: "per-trade exposure exceeds the permitted budget",
  INSUFFICIENT_BUYING_POWER: "not enough buying power",
  DAILY_HEADROOM: "could breach the daily loss stop",
  WEEKLY_HEADROOM: "could breach the weekly loss limit",
  DRAWDOWN_HEADROOM: "could breach the drawdown limit",
  COOLDOWN: "cool-down after a stop-out",
  ENTRY_WINDOW_CLOSED: "entry window closed",
  OUTSIDE_TRADING_WINDOW: "outside the trading window",
  KILL_ACTIVE: "a kill switch is latched",
  MAX_TRADES_PER_DAY: "maximum trades for the day",
  STRATEGY_MAX_ENTRIES: "the strategy's own daily entry cap",
  MAX_POSITION: "maximum position reached",
  REGIME_UNKNOWN: "no regime reading",
  REGIME_STALE: "the regime reading is stale",
  REGIME_BLOCKED: "a prohibited regime is present",
  REGIME_NOT_ALLOWED: "the regime is outside the strategy's policy",
  ABNORMAL_MARKET: "abnormal market move (OD-017)",
};
