import type { DisplayMode } from "../model/mode";
import type { DashState } from "../state";

/** Everything a screen needs to render one frame. Screens are pure functions of this. */
export interface Ctx {
  st: DashState;
  /** the simulated clock being displayed (ms since epoch) */
  simNow: number;
  replay: boolean;
  mode: DisplayMode;
  feed: "connecting" | "open" | "error" | "replay";
  rawLogs: boolean;
  chainOpen: boolean;
  killsOpen: boolean;
  voiceOn: boolean;
  voiceSupported: boolean;
}

export const REGIME_LABEL: Record<string, string> = {
  TRENDING_UP: "Trending up",
  TRENDING_DOWN: "Trending down",
  MEAN_REVERTING: "Range-bound",
  VOLATILITY_EXPANSION: "Volatility expansion",
  VOLATILITY_COMPRESSION: "Volatility compression",
  EXPIRY_DAY: "Expiry day",
  VOLATILITY_NORMAL: "Normal volatility",
  OPENING_DRIVE: "Opening drive",
  OPENING_REVERSION: "Opening reversion",
  GAP_REGIME: "Gap day",
  EXPIRY_REGIME: "Expiry day",
  EVENT_REGIME: "Event day",
  NO_EDGE: "No edge (classifier warming up)",
  ABNORMAL_MARKET: "Abnormal market",
};
export const regimeLabel = (t: string): string => REGIME_LABEL[t] ?? t.replaceAll("_", " ").toLowerCase();

export const PHASE_LABEL: Record<string, string> = {
  BEFORE_WINDOW: "Pre-open",
  OPENING_NO_ENTRY: "Opening · no entries",
  ENTRY_ALLOWED: "Entry window open",
  EXIT_ONLY: "Exits only",
  FLATTENING: "Forced flatten",
  CLOSED: "Closed",
};

export const KILL_SHORT: Record<string, string> = {
  STRATEGY_KILL: "Strategy",
  PORTFOLIO_KILL: "Portfolio",
  DAILY_LOSS_KILL: "Daily loss",
  DATA_QUALITY_KILL: "Data quality",
  BROKER_CONNECTIVITY_KILL: "Broker link",
  ABNORMAL_MARKET_KILL: "Abnormal market",
  POSITION_RECONCILIATION_KILL: "Reconciliation",
  SYSTEM_INTEGRITY_KILL: "System integrity",
  MANUAL_MASTER_KILL: "Manual master",
};
export const KILL_ORDER = Object.keys(KILL_SHORT);
