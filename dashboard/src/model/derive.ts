// Pure derivations over event payloads. Everything here is arithmetic on numbers the backend sent;
// nothing is estimated or invented. Unknown inputs give null, which the UI renders as "—" or "unknown".

export const n = (x: unknown): number | null => {
  if (x === null || x === undefined || x === "") return null;
  const v = Number(x);
  return Number.isFinite(v) ? v : null;
};

export interface PositionLike {
  open?: boolean;
  qty?: number;
  avg?: unknown;
  bid?: unknown;
  stop_limit?: unknown;
  stop_trigger?: unknown;
  target?: unknown;
}

/** premium paid for the open position (long options: the most it can lose) */
export function exposure(p: PositionLike | null | undefined): number {
  if (!p?.open) return 0;
  const avg = n(p.avg);
  return avg === null || !p.qty ? 0 : avg * p.qty;
}

/** share of NAV deployed as option premium */
export function deployedFrac(p: PositionLike | null | undefined, nav: unknown): number | null {
  const v = n(nav);
  if (v === null || v <= 0) return null;
  return exposure(p) / v;
}

/** rupees lost if the stop-limit fills (what the Governor sized the trade against) */
export function riskAtStop(p: PositionLike | null | undefined): number | null {
  if (!p?.open) return null;
  const avg = n(p.avg);
  const sl = n(p.stop_limit);
  if (avg === null || sl === null || !p.qty) return null;
  return Math.max(0, (avg - sl) * p.qty);
}

/** unrealised move expressed in R (1R = entry to stop-limit) */
export function rMultiple(p: PositionLike | null | undefined): number | null {
  if (!p?.open) return null;
  const avg = n(p.avg);
  const bid = n(p.bid);
  const sl = n(p.stop_limit);
  if (avg === null || bid === null || sl === null || avg - sl <= 0) return null;
  return (bid - avg) / (avg - sl);
}

/** where the bid sits between stop (0) and target (1); clamped to [0, 1] */
export function ladderFrac(p: PositionLike | null | undefined): { entry: number; bid: number } | null {
  const sl = n(p?.stop_trigger);
  const tg = n(p?.target);
  const avg = n(p?.avg);
  const bid = n(p?.bid);
  if (!p?.open || sl === null || tg === null || avg === null || bid === null || tg <= sl) return null;
  const f = (v: number) => Math.max(0, Math.min(1, (v - sl) / (tg - sl)));
  return { entry: f(avg), bid: f(bid) };
}

/** points from spot to each breakout trigger (positive = still that far away) */
export function triggerDistance(spot: unknown, levels: { trigger_up?: unknown; trigger_down?: unknown } | null | undefined) {
  const s = n(spot);
  const up = n(levels?.trigger_up);
  const dn = n(levels?.trigger_down);
  if (s === null || up === null || dn === null) return null;
  return { up: up - s, down: s - dn };
}

export type Trend = "up" | "down" | "flat";
/** VIX direction against the session open (a 2% band counts as flat) */
export function trend(now: unknown, open: unknown, band = 0.02): Trend | null {
  const a = n(now);
  const b = n(open);
  if (a === null || b === null || b === 0) return null;
  const r = a / b - 1;
  return r > band ? "up" : r < -band ? "down" : "flat";
}

export interface ChainRow { strike: number; ce: { bid: unknown; ask: unknown; iv?: unknown }; pe: { bid: unknown; ask: unknown; iv?: unknown } }
export interface ChainSummary { atm: number; ceMid: number; peMid: number; straddle: number; atmIv: number | null; skew: number | null; spread: number }

/** ATM summary of the SIMULATED chain. No OI or PCR: the simulator does not produce them. */
export function chainSummary(chain: { atm?: number; rows?: ChainRow[] } | null | undefined): ChainSummary | null {
  const row = chain?.rows?.find((r) => r.strike === chain.atm);
  if (!row) return null;
  const mid = (q: { bid: unknown; ask: unknown }) => ((n(q.bid) ?? 0) + (n(q.ask) ?? 0)) / 2;
  const ceIv = n(row.ce.iv);
  const peIv = n(row.pe.iv);
  const spread = ((n(row.ce.ask) ?? 0) - (n(row.ce.bid) ?? 0) + (n(row.pe.ask) ?? 0) - (n(row.pe.bid) ?? 0)) / 2;
  return {
    atm: row.strike,
    ceMid: mid(row.ce),
    peMid: mid(row.pe),
    straddle: mid(row.ce) + mid(row.pe),
    atmIv: ceIv !== null && peIv !== null ? (ceIv + peIv) / 2 : null,
    skew: ceIv !== null && peIv !== null ? peIv - ceIv : null,
    spread,
  };
}

/** "HH:MM[:SS]" to minutes after midnight */
export const hm = (s: string): number => {
  const [h, m, sec] = s.split(":").map(Number);
  return h * 60 + m + (sec || 0) / 60;
};

export interface SessionMark { at: number; label: string; key: string }
/** the trading-window rules on a 09:15-15:30 line */
export function sessionMarks(w: Record<string, string> | null | undefined): SessionMark[] {
  const g = (k: string, d: string) => hm(w?.[k] ?? d);
  return [
    { key: "open", at: hm("09:15"), label: "Open" },
    { key: "entry_start", at: g("entry_start", "09:20"), label: "Entries" },
    { key: "entry_cutoff", at: g("entry_cutoff", "14:00"), label: "Last entry" },
    { key: "flatten_start", at: g("flatten_start", "14:50"), label: "Flatten" },
    { key: "hard_flat", at: g("hard_flat", "15:00"), label: "Hard flat" },
    { key: "close", at: hm("15:30"), label: "Close" },
  ];
}

/** next window boundary after `now` (minutes), or null after the close */
export function nextMark(marks: SessionMark[], now: number): SessionMark | null {
  return marks.find((m) => m.at > now + 1e-9) ?? null;
}

/** The right rail's "Risk budget" word, from the risk numbers and the kill switches only
 *  (deliberately not the global attention level: a broker drop is not a risk-budget breach). */
export function riskBudgetState(o: { dailyLossFrac: number; ddFrac: number; ddWarnFrac: number; ddSuspendFrac: number; killLatched: boolean; halted: boolean }): { label: string; tone: "pos" | "warn" | "neg"; why: string } {
  if (o.killLatched || o.halted) return { label: "ENTRIES HALTED", tone: "neg", why: "a kill switch or halt is latched, so no new entries are allowed" };
  if (o.dailyLossFrac >= 1) return { label: "BREACHED", tone: "neg", why: "the daily loss stop has been used up" };
  if (o.ddFrac >= o.ddSuspendFrac) return { label: "BREACHED", tone: "neg", why: "drawdown is at or beyond the suspension level" };
  if (o.dailyLossFrac >= 0.5) return { label: "ELEVATED", tone: "warn", why: "half or more of the daily loss stop is used" };
  if (o.ddFrac >= o.ddWarnFrac) return { label: "ELEVATED", tone: "warn", why: "drawdown is at or beyond the warning level" };
  return { label: "NORMAL", tone: "pos", why: "daily loss under 50% of the stop, drawdown under the warning level, no kill latched" };
}

/** Intraday chart x-domain in session minutes: from the open to 30 min past "now" (at least 90 min wide),
 *  never past the close, so an early-session line is not squashed into the left edge of a 6¼-hour axis. */
export function chartWindow(nowMin: number | null): [number, number] {
  const open = 9 * 60 + 15;
  const close = 15 * 60 + 30;
  if (nowMin === null || !Number.isFinite(nowMin)) return [open, close];
  return [open, Math.min(close, Math.max(open + 90, nowMin + 30))];
}
