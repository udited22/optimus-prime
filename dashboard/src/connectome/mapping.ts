// Event → activation mapping for the cognitive connectome (docs/architecture/observability.md §17.5 v3.2). Pure and unit-tested
// (test/connectome.test.ts). It is the single source of truth: the renderer owns no meaning.
//
// Three layers, all derived from the stream and nothing else:
//  1. commandsFor(event): the transient activations a real event causes: an axonal pathway carrying an impulse
//     between two regions, an assembly (meso) or region (macro) firing, a strategy assembly firing, a pathway
//     terminating at RISK, and an embedded annotation. Every pathway comes from a real flow [from, to, kind] on a
//     declared topology edge, so an impulse only travels where the architecture really sends data. Each firing
//     leaves a synaptic afterglow (afterglow(): 5–20 s), so recent attention stays faintly visible.
//  2. cognitiveState(): the four states the owner named (WATCHING / EVALUATING / REJECTED / EXECUTING, plus HALTED
//     for a latched kill), from Tony's real `cognition` and the chain step actually playing.
//  3. fieldState(state): the standing state of every region and assembly, the strategy assemblies' eight
//     pattern states (lifecycle stage + real decisions + kills), and the last-decision trail; recomputed on every
//     render, so a replay seek shows the same picture as a live stream at that moment.
// Brightness always means activity. No command or state reads P&L, and no confidence is invented.
import { receivesFeed } from "../model/lifecycle";
import type { DashState } from "../state";
import type { DashEvent } from "../types";
import { KILL_ASSEMBLY, KILL_REGION, assemblyRegion, REASON_ASSEMBLY, REASON_SHORT, type RegionId, type Tone, regionOfNode, strategyAssembly } from "./regions";

export type Command =
  | { type: "path"; from: RegionId; to: RegionId; tone: Tone; strength: number; kind: string; hop: number; strategy?: string }
  | { type: "fire"; region: RegionId; assembly?: string; tone: Tone; strength: number; hop: number }
  | { type: "terminate"; region: RegionId; tone: "red"; hop: number; strategy?: string; assembly?: string }
  | { type: "strategy"; id: string; tone: Tone; strength: number; hop: number }
  | { type: "label"; region: RegionId; assembly?: string; title: string; text: string; detail?: string; tone: Tone; hop: number };

export interface MapCtx {
  /** kill switch ids latched before this event (so a re-sent KILLS event does not fire again) */
  latchedBefore: Set<string>;
}

const FLOW: Record<string, { tone: Tone; strength: number }> = {
  tick: { tone: "cyan", strength: 0.12 },
  clean: { tone: "cyan", strength: 0.08 },
  regime: { tone: "cyan", strength: 0.4 },
  budget: { tone: "white", strength: 0.22 },
  intent: { tone: "violet", strength: 0.85 },
  approve: { tone: "green", strength: 0.9 },
  order: { tone: "cyan", strength: 0.8 },
  ack: { tone: "green", strength: 0.6 },
  fill: { tone: "green", strength: 0.9 },
  report: { tone: "white", strength: 0.35 },
  promote: { tone: "violet", strength: 0.35 },
  kill: { tone: "red", strength: 0.7 },
};

/** the assembly a flow lands on at its destination (meso addressing), where the flow says which one */
const LANDS: Record<string, string> = {
  "market_intel:tick": "P.FEED", "data_quality:tick": "P.DQ", "cio:regime": "C.POSTURE", "strategy_factory:regime": "RS.FACTORY",
  "allocator:budget": "PF.BUDGET", "allocator:intent": "PF.SIZING", "execution:approve": "E.ENTRY", "broker:order": "B.PORT",
  "execution:ack": "E.LINK", "post_trade:fill": "RS.POST", "validation:report": "RS.VALID", "validation:promote": "RS.VALID",
  "cio:kill": "C.ATTN", "allocator:approve": "PF.SIZING",
};

const REGIME_WORD: Record<string, string> = {
  TRENDING_UP: "trending up", TRENDING_DOWN: "trending down", MEAN_REVERTING: "range-bound",
  VOLATILITY_EXPANSION: "volatility expansion", VOLATILITY_COMPRESSION: "volatility compression", EXPIRY_DAY: "expiry day",
  VOLATILITY_NORMAL: "normal volatility", OPENING_DRIVE: "opening drive", OPENING_REVERSION: "opening reversion", GAP_REGIME: "gap day", EXPIRY_REGIME: "expiry day", EVENT_REGIME: "event day", NO_EDGE: "no edge (warming up)", ABNORMAL_MARKET: "abnormal market",
};
const KILL_WORD: Record<string, string> = {
  STRATEGY_KILL: "Strategy kill", PORTFOLIO_KILL: "Portfolio kill", DAILY_LOSS_KILL: "Daily-loss kill",
  DATA_QUALITY_KILL: "Data-quality kill", BROKER_CONNECTIVITY_KILL: "Broker-link kill", ABNORMAL_MARKET_KILL: "Abnormal-market kill",
  POSITION_RECONCILIATION_KILL: "Reconciliation kill", SYSTEM_INTEGRITY_KILL: "System-integrity kill", MANUAL_MASTER_KILL: "Manual master kill",
};
const strategyOf = (node: string): string | undefined => (node.startsWith("strat:") ? node.slice(6) : undefined);
const cap = (s: string): string => (s ? s[0].toUpperCase() + s.slice(1) : s);
const num = (x: unknown, dp = 2): string => {
  const v = Number(x);
  return Number.isFinite(v) ? v.toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp }) : "—";
};

/** the reasons a Governor rejection gives, in words (the kernel's own `explain` text first) */
export function rejectionText(d: any): string {
  const ex: string[] = Array.isArray(d?.explain) ? d.explain : [];
  if (ex.length) return ex[0] + (ex.length > 1 ? ` (+${ex.length - 1} more)` : "");
  const rs: string[] = Array.isArray(d?.reasons) ? d.reasons : [];
  if (!rs.length) return "reason not recorded";
  return (REASON_SHORT[rs[0]] ?? rs[0]) + (rs.length > 1 ? ` (+${rs.length - 1} more)` : "");
}
/** the gating assemblies a rejection's reason codes come from, first reason first (unknown codes: none) */
export function rejectingAssemblies(d: any): string[] {
  const rs: string[] = Array.isArray(d?.reasons) ? d.reasons : [];
  return [...new Set(rs.map((r) => REASON_ASSEMBLY[r]).filter(Boolean))];
}

export function commandsFor(e: DashEvent, ctx: MapCtx = { latchedBefore: new Set() }): Command[] {
  const out: Command[] = [];
  const d: any = e.data ?? {};
  // 1. every real flow is a pathway between two regions, in causal (hop) order; it lands on an assembly where the
  //    flow names one
  e.flows.forEach(([a, b, kind], hop) => {
    const ra = regionOfNode(a);
    const rb = regionOfNode(b);
    if (!ra || !rb) return;
    if (kind === "reject") {
      // the verdict goes back to the allocator inside the kernel; visually the thought dies at RISK
      out.push({ type: "terminate", region: "RISK", tone: "red", hop, strategy: d.strategy, assembly: rejectingAssemblies(d)[0] });
      return;
    }
    const f0 = FLOW[kind] ?? { tone: "white" as Tone, strength: 0.2 };
    // the hypothesis reaching the Governor is under evaluation: amber (the brief: amber = uncertainty)
    const f = kind === "intent" && rb === "RISK" ? { tone: "amber" as Tone, strength: f0.strength } : f0;
    let strength = f.strength;
    if (e.kind === "TICK" && kind === "tick") strength = Math.min(0.3, 0.08 + Math.abs(Number(d.delta) || 0) / 30); // bigger move, more activity
    const lands = LANDS[`${b}:${kind}`];
    if (ra === rb) out.push({ type: "fire", region: ra, assembly: lands, tone: f.tone, strength, hop });
    else {
      out.push({ type: "path", from: ra, to: rb, tone: f.tone, strength, kind, hop, strategy: strategyOf(a) ?? strategyOf(b) });
      // the impulse lands: on the assembly the flow names, else (if strong) on the destination region as a whole
      if (lands) out.push({ type: "fire", region: rb, assembly: lands, tone: f.tone, strength: strength * 0.8, hop: hop + 1 });
      else if (strength >= 0.3) out.push({ type: "fire", region: rb, tone: f.tone, strength: strength * 0.45, hop: hop + 1 });
    }
    const s = strategyOf(b) ?? strategyOf(a);
    // every fed strategy's assembly receives the bar and evaluates it (ideas compete); only a proposer goes on
    if (s && kind === "clean") out.push({ type: "strategy", id: s, tone: "violet", strength: 0.22, hop: hop + 1 });
  });
  // 2. what the event means at its destination
  switch (e.kind) {
    case "REGIME": {
      const tags: string[] = d.tags ?? [];
      out.push({ type: "fire", region: "PERCEPTION", assembly: "P.REGIME", tone: "cyan", strength: 0.5, hop: 0 });
      out.push({ type: "fire", region: "PERCEPTION", assembly: "P.VOL", tone: "cyan", strength: 0.25, hop: 0 });
      out.push({
        type: "label", region: "PERCEPTION", assembly: "P.REGIME", title: "Market perception", tone: "cyan", hop: 0,
        text: cap(tags.map((t) => REGIME_WORD[t] ?? t).join(" · ")) || "No regime tag",
        detail: `30 min ${Number(d.ret_30m_pct) >= 0 ? "+" : ""}${num(d.ret_30m_pct)}% · VIX ${num(d.vix)}`,
      });
      break;
    }
    case "CHAIN":
      out.push({ type: "fire", region: "PERCEPTION", assembly: "P.CHAIN", tone: "cyan", strength: 0.06, hop: 0 });
      break;
    case "INTENT":
      out.push({ type: "strategy", id: d.strategy, tone: "violet", strength: 1, hop: 0 });
      out.push({ type: "label", region: "STRATEGY", assembly: strategyAssembly(d.strategy), title: d.strategy, text: `Proposes ${d.side} ${d.qty} ${d.symbol} @ ${num(d.limit)}`, detail: d.stage === "SHADOW" || d.stage === "PAPER" ? "simulate-only" : undefined, tone: "violet", hop: 0 });
      out.push({ type: "fire", region: "PORTFOLIO", assembly: "PF.SIZING", tone: "white", strength: 0.45, hop: 1 });
      out.push({ type: "fire", region: "RISK", tone: "amber", strength: 0.6, hop: 2 });
      out.push({ type: "label", region: "RISK", title: "Risk · evaluating", text: `${d.strategy}`, detail: `stop ${num(d.stop_trigger)} / ${num(d.stop_limit)}`, tone: "amber", hop: 2 });
      break;
    case "DECISION":
      if (String(d.verdict).startsWith("APPROVE") && d.simulate_only) {
        // a SHADOW / PAPER approval goes back to the allocator as a simulated trade: nothing reaches EXECUTION
        out.push({ type: "fire", region: "RISK", assembly: "R.BUDGET", tone: "green", strength: 0.45, hop: 0 });
        out.push({ type: "label", region: "RISK", title: "Risk · approved, simulate-only", text: `${d.strategy} (${d.stage})`, detail: "no order is sent", tone: "green", hop: 0 });
      } else if (String(d.verdict).startsWith("APPROVE")) {
        out.push({ type: "fire", region: "RISK", tone: "green", strength: 0.7, hop: 0 });
        out.push({ type: "fire", region: "RISK", assembly: "R.BUDGET", tone: "green", strength: 0.8, hop: 0 });
        out.push({ type: "label", region: "RISK", assembly: "R.BUDGET", title: "Risk · approved", text: d.strategy, detail: `risk at stop ₹${num(d.risk_at_stop)} of ₹${num(d.budget)}`, tone: "green", hop: 0 });
        out.push({ type: "fire", region: "EXECUTION", tone: "green", strength: 0.5, hop: 1 });
      } else {
        const asm = rejectingAssemblies(d);
        asm.forEach((a, i) => out.push({ type: "fire", region: "RISK", assembly: a, tone: "red", strength: i === 0 ? 1 : 0.55, hop: 0 }));
        out.push({ type: "strategy", id: d.strategy, tone: "amber", strength: 0.35, hop: 0 }); // the idea decays
        const rs: string[] = Array.isArray(d.reasons) ? d.reasons : [];
        out.push({
          type: "label", region: "RISK", assembly: asm[0], title: "Risk · rejected", tone: "red", hop: 0,
          text: cap(REASON_SHORT[rs[0]] ?? (rs[0] ? rs[0].toLowerCase().replace(/_/g, " ") : "reason not recorded")),
          detail: `${d.strategy}: ${rejectionText(d)}`,
        });
      }
      break;
    case "ORDER": {
      const asm = d.kind === "PROTECTIVE" ? "E.STOP" : d.kind === "EXIT" ? "E.EXIT" : "E.ENTRY";
      const what = d.kind === "PROTECTIVE" ? "Protective stop sent" : d.kind === "EXIT" ? "Exit order sent" : "Entry order sent";
      out.push({ type: "fire", region: "EXECUTION", assembly: asm, tone: "cyan", strength: 0.7, hop: 0 });
      out.push({ type: "label", region: "EXECUTION", assembly: asm, title: "Execution", text: what, detail: `${d.side} ${d.qty} ${d.symbol} @ ${num(d.price)}`, tone: "cyan", hop: 0 });
      break;
    }
    case "FILL": {
      const asm = d.kind === "PROTECTIVE" ? "E.STOP" : d.kind === "EXIT" ? "E.EXIT" : "E.ENTRY";
      out.push({ type: "fire", region: "EXECUTION", assembly: asm, tone: "green", strength: 0.9, hop: 1 });
      out.push({ type: "label", region: "EXECUTION", assembly: asm, title: "Broker confirmed", text: d.kind === "ENTRY" ? "Filled" : d.kind === "PROTECTIVE" ? "Stop filled" : "Exit filled", detail: `${d.qty} @ ${num(d.price)}`, tone: "green", hop: 1 });
      out.push({ type: "fire", region: "PORTFOLIO", assembly: "PF.POSITION", tone: "green", strength: 0.45, hop: 2 });
      break;
    }
    case "POSITION":
      if (d.open) out.push({ type: "fire", region: "PORTFOLIO", assembly: "PF.POSITION", tone: "white", strength: 0.12, hop: 0 });
      break;
    case "KILLS":
      for (const s of (d.switches ?? []) as any[]) {
        if (!s.latched || ctx.latchedBefore.has(s.id)) continue;
        const region = KILL_REGION[s.id] ?? "CORE";
        const assembly = s.id === "STRATEGY_KILL" && Array.isArray(s.scope) && s.scope[0] && region === "STRATEGY" ? strategyAssembly(s.scope[0]) : KILL_ASSEMBLY[s.id];
        out.push({ type: "fire", region, tone: "red", strength: 0.8, hop: 0 });
        if (assembly && assemblyRegion(assembly) === region) out.push({ type: "fire", region, assembly, tone: "red", strength: 1, hop: 0 });
        out.push({ type: "label", region, assembly, title: KILL_WORD[s.id] ?? s.id, text: "Latched", detail: s.reason || "reason not recorded", tone: "red", hop: 0 });
      }
      break;
    case "LOG": {
      const actor: string = d.activity?.actor ?? "";
      const asm = actor === "post_trade" ? "RS.POST" : actor === "validation" ? "RS.VALID" : actor === "strategy_factory" ? "RS.FACTORY" : null;
      if (asm) {
        out.push({ type: "fire", region: "RESEARCH", assembly: asm, tone: "white", strength: 0.3, hop: 0 });
        out.push({ type: "label", region: "RESEARCH", assembly: asm, title: "Learning / research", text: String(d.activity?.text ?? d.text ?? ""), tone: "white", hop: 0 });
      }
      break;
    }
    case "ECONOMICS":
      out.push({ type: "fire", region: "RESEARCH", assembly: "RS.ECON", tone: "white", strength: 0.15, hop: 0 });
      break;
    case "TONY": {
      const lvl = d.attention?.level;
      // Tony re-assesses after every material change: the integration core takes it in
      out.push({ type: "fire", region: "CORE", assembly: "C.ATTN", tone: lvl === "INTERVENTION" ? "red" : lvl === "ATTENTION" ? "amber" : "cyan", strength: 0.18, hop: 0 });
      out.push({ type: "fire", region: "CORE", assembly: "C.VOICE", tone: "white", strength: 0.1, hop: 0 });
      break;
    }
    default:
      break;
  }
  return out;
}

// ---------------------------------------------------------------- synaptic afterglow (mirrored in the shader)
/** a firing is a fast response (τ 0.55 s) plus a faint memory (τ 6 s, 20 % weight): visible for ~5–20 s */
export const FAST_TAU_S = 0.55;
export const GLOW_TAU_S = 6;
export const GLOW_WEIGHT = 0.2;
export function afterglow(amp: number, dtS: number): number {
  if (dtS < 0 || amp <= 0) return 0;
  return amp * (Math.exp(-dtS / FAST_TAU_S) + GLOW_WEIGHT * Math.exp(-dtS / GLOW_TAU_S));
}

// ---------------------------------------------------------------- choreography (causal timing)
/** a pathway travels in HOP_MS; the kernel decides intent → verdict → order → fill in one simulated instant, so the
 *  hero spaces those real steps out in their causal order rather than drawing them on top of each other */
export const HOP_MS = 700;
const CHAIN_KINDS = new Set(["INTENT", "DECISION", "ORDER", "FILL"]);

export interface Scheduled { at: number; cmd: Command }

export class Choreographer {
  private cursor = 0;
  constructor(private hopMs = HOP_MS) {}
  isChain(e: DashEvent): boolean {
    return CHAIN_KINDS.has(e.kind) || e.flows.some(([, , k]) => k === "ack" || k === "kill");
  }
  schedule(e: DashEvent, cmds: Command[], now: number): Scheduled[] {
    if (!cmds.length) return [];
    const chain = this.isChain(e);
    const base = chain ? Math.max(now, this.cursor) : now;
    const maxHop = Math.max(...cmds.map((c) => c.hop));
    if (chain) this.cursor = base + (maxHop + 1) * this.hopMs;
    return cmds.map((cmd) => ({ at: base + cmd.hop * this.hopMs, cmd }));
  }
  /** when the last scheduled chain step ends (for "Tony is evaluating / executing") */
  get busyUntil(): number {
    return this.cursor;
  }
  reset(): void {
    this.cursor = 0;
  }
}

/** Tony's transient thought while a real chain step plays (else the backend's cognition stands) */
export function transientCognition(e: DashEvent): { verb: string; subject: string } | null {
  const d: any = e.data ?? {};
  if (e.kind === "INTENT") return { verb: "EVALUATING", subject: `${d.strategy} · ${d.side} ${d.symbol}` };
  if (e.kind === "DECISION") {
    if (!String(d.verdict).startsWith("APPROVE")) return { verb: "REJECTED", subject: `${d.strategy} at Risk · ${REASON_SHORT[(d.reasons ?? [])[0]] ?? "see the Governor's reasons"}` };
    return d.simulate_only ? { verb: "EVALUATING", subject: `${d.strategy} · approved simulate-only` } : { verb: "EXECUTING", subject: `${d.strategy} · ${d.symbol}` };
  }
  if (e.kind === "ORDER" || e.kind === "FILL") return { verb: "EXECUTING", subject: `${d.symbol}` };
  return null;
}

export function cognitionLine(base: { verb?: string; subject?: string } | null | undefined, transient: { verb: string; subject: string } | null): { verb: string; subject: string } {
  if (transient) return transient;
  if (base?.verb) return { verb: base.verb, subject: base.subject ?? "" };
  return { verb: "CONNECTING", subject: "waiting for Tony's first status" };
}

// ---------------------------------------------------------------- the four cognitive states
export type CognitiveState = "WATCHING" | "EVALUATING" | "REJECTED" | "EXECUTING" | "HALTED";
export const STATE_INDEX: Record<CognitiveState, number> = { WATCHING: 0, EVALUATING: 1, REJECTED: 2, EXECUTING: 3, HALTED: 4 };
/** how long a state holds after its real step played (real seconds; the afterglow carries it visually) */
export const HOLD_S: Record<"EVALUATING" | "REJECTED" | "EXECUTING", number> = { EVALUATING: 4, REJECTED: 15, EXECUTING: 12 };
export interface Phase { state: "EVALUATING" | "REJECTED" | "EXECUTING"; at: number }

/** the step of a real chain an event is (null: not a chain step) */
export function phaseOf(e: DashEvent): Phase["state"] | null {
  const t = transientCognition(e);
  return t && (t.verb === "EVALUATING" || t.verb === "REJECTED" || t.verb === "EXECUTING") ? t.verb : null;
}

/** WATCHING / EVALUATING / REJECTED / EXECUTING from Tony's real cognition verb and the chain step that played
 *  last (nowS and phase.at in real seconds). HALTED (a latched kill) overrides everything. Tony's own verbs map:
 *  EXECUTING (a working entry order) → EXECUTING; HALTED → HALTED; OBSERVING, WATCHING, WAITING, MANAGING (holding a
 *  position, no decision in flight), RESTING, UNKNOWN → WATCHING. */
export function cognitiveState(verb: string | null | undefined, phase: Phase | null, nowS: number): CognitiveState {
  if (verb === "HALTED") return "HALTED";
  if (phase && nowS >= phase.at && nowS - phase.at < HOLD_S[phase.state]) return phase.state;
  if (verb === "EXECUTING") return "EXECUTING";
  return "WATCHING";
}

// ---------------------------------------------------------------- standing state
export type RegionStateKind = "normal" | "degraded" | "alarm" | "quiet";
export interface RegionState { state: RegionStateKind; sustain: number; tone: Tone; note: string }
/** the eight strategy-assembly states (the owner's six plus paused and killed, which the lifecycle has) */
export type PatternState = "dormant" | "researching" | "candidate" | "live" | "paused" | "killed" | "evaluating" | "rejected";
export interface StrategyPattern {
  id: string; stage: string; state: PatternState;
  /** 0 = scattered cells, 1 = a fully formed assembly */
  formation: number;
  base: number; tone: Tone; activity: number; killed: boolean;
}
export interface Trail { approved: boolean; strategy: string; age: number; regions: RegionId[]; terminal: RegionId; text: string }
export interface FieldState {
  regions: Record<RegionId, RegionState>;
  /** standing sustain of single assemblies (an open position, the protective stop, a latched kill's assembly) */
  assemblies: Record<string, { sustain: number; tone: Tone }>;
  strategies: StrategyPattern[];
  trail: Trail | null;
  closed: boolean;
}

export const TRAIL_S = 120;
/** a rejected idea's assembly decays over this many simulated seconds; an approved one is "evaluating" as long */
export const PATTERN_S = 30;
/** lifecycle stage → resting pattern state (directive stages; see the pipeline columns in docs/architecture/observability.md) */
export const STAGE_PATTERN: Record<string, PatternState> = {
  RESEARCH: "researching", BACKTESTED: "researching", VALIDATED: "researching", PAPER: "candidate", SHADOW: "candidate",
  CANARY: "live", PRODUCTION: "live", DEGRADED: "paused", QUARANTINED: "paused", RETIRED: "dormant",
};
/** per state: formation, resting brightness, tone, and whether it shimmers (stage-encoded, not an event) */
export const PATTERN_LOOK: Record<PatternState, { formation: number; base: number; tone: Tone }> = {
  dormant: { formation: 0.12, base: 0.02, tone: "white" },
  researching: { formation: 0.5, base: 0.1, tone: "violet" },
  candidate: { formation: 0.75, base: 0.17, tone: "violet" },
  live: { formation: 1, base: 0.22, tone: "violet" },
  paused: { formation: 0.5, base: 0.08, tone: "amber" },
  killed: { formation: 0.3, base: 0.06, tone: "red" },
  evaluating: { formation: 1, base: 0.36, tone: "violet" },
  rejected: { formation: 0.9, base: 0.3, tone: "red" },
};

/** one strategy assembly's state from its lifecycle stage, its last real decision and the kills */
export function strategyPattern(row: { id: string; stage: string; killed?: boolean }, last: { verdict: string; ts: string } | undefined, killedScope: Set<string>, simNowMs: number, closed: boolean): StrategyPattern {
  const rest = STAGE_PATTERN[row.stage] ?? "researching";
  const killed = !!row.killed || killedScope.has(row.id);
  const ageS = last ? (simNowMs - Date.parse(last.ts)) / 1000 : Infinity;
  const recent = ageS >= 0 && ageS < PATTERN_S;
  let state: PatternState = rest;
  if (killed) state = "killed";
  else if (recent) state = String(last!.verdict).startsWith("APPROVE") ? "evaluating" : "rejected";
  const look = PATTERN_LOOK[state];
  let formation = look.formation;
  let base = look.base;
  if (state === "rejected") {
    // the rejected pattern comes apart: from nearly formed to below its resting formation over PATTERN_S
    const k = Math.min(1, Math.max(0, ageS / PATTERN_S));
    formation = look.formation + (Math.min(PATTERN_LOOK[rest].formation, 0.45) - look.formation) * k;
    base = look.base * (1 - k) + PATTERN_LOOK[rest].base * k;
  }
  if (closed) base *= 0.5;
  const feed = receivesFeed(row.stage) && !closed && !killed ? 0.05 : 0;
  const activity = state === "evaluating" ? 0.6 * (1 - ageS / PATTERN_S) + feed : state === "killed" ? 0 : feed;
  return { id: row.id, stage: row.stage, state, formation, base, tone: look.tone, activity: Math.min(1, activity), killed };
}

function blankRegions(): Record<RegionId, RegionState> {
  const r = {} as Record<RegionId, RegionState>;
  for (const id of ["PERCEPTION", "STRATEGY", "RISK", "PORTFOLIO", "EXECUTION", "RESEARCH", "CORE", "BROKER"] as RegionId[])
    r[id] = { state: "normal", sustain: 0, tone: "white", note: "" };
  return r;
}

export function fieldState(st: DashState, simNowMs: number): FieldState {
  const regions = blankRegions();
  const asm: FieldState["assemblies"] = {};
  const closed = !!st.dayEnd || st.phase === "CLOSED";
  const sys = st.tony?.system ?? {};
  // kills: only the affected region (and its kill's assembly) changes state
  for (const s of st.latched()) {
    if (s.id === "STRATEGY_KILL" && Array.isArray(s.scope) && s.scope.filter(Boolean).length) continue; // scoped: the patterns only
    const id = KILL_REGION[s.id] ?? "CORE";
    regions[id] = { state: "alarm", sustain: 0.3, tone: "red", note: `${KILL_WORD[s.id] ?? s.id} latched${s.reason ? ` · ${s.reason}` : ""}` };
    const a = KILL_ASSEMBLY[s.id];
    if (a && assemblyRegion(a) === id) asm[a] = { sustain: 0.6, tone: "red" };
  }
  for (const h of (st.kills?.halts ?? []) as any[]) {
    if (regions.RISK.state !== "alarm") regions.RISK = { state: "alarm", sustain: 0.3, tone: "red", note: `Halt ${h.kind ?? ""}`.trim() };
    asm["R.KILLS"] = { sustain: 0.5, tone: "red" };
  }
  if (sys.integrity_failed) regions.CORE = { state: "alarm", sustain: 0.3, tone: "red", note: "system-integrity failure" };
  if (sys.broker_connected === false) {
    if (regions.EXECUTION.state !== "alarm") regions.EXECUTION = { state: "degraded", sustain: 0.15, tone: "amber", note: "broker link down" };
    asm["E.LINK"] ??= { sustain: 0.45, tone: "amber" };
    regions.BROKER = { state: "quiet", sustain: 0, tone: "white", note: "link down" };
  }
  const age = sys.feed_age_s === null || sys.feed_age_s === undefined ? null : Number(sys.feed_age_s);
  if (age !== null && age >= 5 && regions.PERCEPTION.state === "normal") {
    regions.PERCEPTION = { state: "degraded", sustain: 0.08, tone: "amber", note: `no tick for ${Math.round(age)} s` };
    asm["P.FEED"] = { sustain: 0.35, tone: "amber" };
  }
  // an open position is standing activity in PORTFOLIO and EXECUTION (protective stop resting or not)
  const p = st.position;
  if (p?.open) {
    if (regions.PORTFOLIO.state === "normal") regions.PORTFOLIO = { state: "normal", sustain: 0.1, tone: "green", note: `holding ${p.symbol}` };
    asm["PF.POSITION"] = { sustain: 0.4, tone: "green" };
    if (regions.EXECUTION.state === "normal")
      regions.EXECUTION = p.protective_confirmed
        ? { state: "normal", sustain: 0.06, tone: "green", note: "protective stop resting at the broker" }
        : { state: "degraded", sustain: 0.1, tone: "amber", note: "protective stop not confirmed yet" };
    asm["E.STOP"] ??= p.protective_confirmed ? { sustain: 0.35, tone: "green" } : { sustain: 0.4, tone: "amber" };
  }
  if (closed) for (const id of Object.keys(regions) as RegionId[]) if (regions[id].state === "normal") regions[id] = { state: "quiet", sustain: 0, tone: "white", note: "session closed" };
  // strategy assemblies: lifecycle stage, the strategy's last real decision, and kills
  const rows: any[] = st.tony?.strategies ?? st.strategies?.rows ?? [];
  const killedScope = new Set<string>(st.latched().filter((s: any) => s.id === "STRATEGY_KILL").flatMap((s: any) => (s.scope ?? []).filter(Boolean)));
  const strategies = rows.map((r) => strategyPattern(r, st.lastDecision[r.id] ?? [...st.decisions].reverse().find((x) => x.strategy === r.id), killedScope, simNowMs, closed));
  // the last decision leaves a fading trail for TRAIL_S simulated seconds
  let trail: Trail | null = null;
  const ld = st.decisions[st.decisions.length - 1];
  if (ld) {
    const a = (simNowMs - Date.parse(ld.ts)) / 1000;
    if (a >= 0 && a < TRAIL_S) {
      const ok = String(ld.verdict).startsWith("APPROVE");
      trail = ok && ld.simulate_only
        ? { approved: true, strategy: ld.strategy, age: a / TRAIL_S, regions: ["STRATEGY", "PORTFOLIO", "RISK"], terminal: "RISK", text: `${ld.strategy} approved simulate-only (${ld.stage}) · no order sent` }
        : ok
        ? { approved: true, strategy: ld.strategy, age: a / TRAIL_S, regions: ["STRATEGY", "PORTFOLIO", "RISK", "EXECUTION", "BROKER"], terminal: "BROKER", text: `${ld.strategy} approved · risk ₹${num(ld.risk_at_stop)} of ₹${num(ld.budget)}` }
        : { approved: false, strategy: ld.strategy, age: a / TRAIL_S, regions: ["STRATEGY", "PORTFOLIO", "RISK"], terminal: "RISK", text: `${ld.strategy} rejected: ${rejectionText(ld)}` };
    }
  }
  return { regions, assemblies: asm, strategies, trail, closed };
}
