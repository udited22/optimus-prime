// The cognitive connectome's event → activation mapping and anatomy (docs/architecture/observability.md §17.5 v3.1, v3.2). Pure functions
// only: no WebGL here.
import { describe, expect, it } from "vitest";
import { BARELY, FILAMENTS, KIND, LKIND, PATHWAYS, TISSUE, buildAnatomy, cortexDensity, pathwayIndex } from "../src/connectome/geometry";
import {
  Choreographer, type Command, GLOW_TAU_S, HOLD_S, HOP_MS, PATTERN_LOOK, PATTERN_S, STAGE_PATTERN, TRAIL_S, afterglow, cognitionLine,
  cognitiveState, commandsFor, fieldState, phaseOf, rejectingAssemblies, strategyPattern, transientCognition,
} from "../src/connectome/mapping";
import {
  COGNITIVE, KILL_ASSEMBLY, KILL_REGION, MAX_STRATEGIES, REASON_ASSEMBLY, REGIONS, type RegionId, assemblies, assemblyRegion, regionOfNode,
} from "../src/connectome/regions";
import { DashState } from "../src/state";
import type { DashEvent, Kind } from "../src/types";
import governorPy from "../../src/project100c/kernel/governor.py?raw";

const TS = "2026-10-07T09:55:00+05:30";
let seq = 0;
const ev = (kind: Kind, data: any, flows: [string, string, string][] = [], ts = TS): DashEvent => ({ seq: ++seq, ts, kind, simulated: true, label: "SIMULATED", data, flows });
const STRATS = ["S-ORB-001", "S-VWAPC-001", "S-FBO-001", "S-VOLX-001", "S-EXPX-001"];

// the flows the backend really emits (observability/dashboard/stream.py), one line per event kind
const REAL_FLOWS: Record<string, [string, string, string][]> = {
  TICK: [["broker", "market_intel", "tick"], ["market_intel", "data_quality", "tick"], ["data_quality", "strat:S-ORB-001", "clean"], ["data_quality", "strat:S-VWAPC-001", "clean"], ["data_quality", "strat:S-FBO-001", "clean"]],
  REGIME: [["market_intel", "cio", "regime"], ["cio", "strategy_factory", "regime"], ["cio", "allocator", "budget"]],
  INTENT: [["strat:S-ORB-001", "allocator", "intent"], ["allocator", "risk_governor", "intent"]],
  APPROVE: [["risk_governor", "execution", "approve"]],
  REJECT: [["risk_governor", "allocator", "reject"]],
  ORDER: [["execution", "broker", "order"]],
  FILL: [["broker", "execution", "fill"], ["execution", "post_trade", "fill"]],
  ACK: [["broker", "execution", "ack"]],
  REPORT: [["post_trade", "validation", "report"]],
  PROMOTE: [["strategy_factory", "validation", "promote"]],
  KILL: [["risk_governor", "cio", "kill"], ["risk_governor", "execution", "kill"]],
};

const intent = ev("INTENT", { strategy: "S-ORB-001", side: "BUY", qty: 65, symbol: "NIFTY 06OCT26 24950 PE", limit: 29.45, stage: "CANARY", stop_trigger: 20, stop_limit: 19 }, REAL_FLOWS.INTENT);
const decision = (verdict: string, extra: any = {}) =>
  ev("DECISION", { strategy: "S-ORB-001", symbol: "NIFTY 06OCT26 24950 PE", side: "BUY", qty: 65, verdict, reasons: verdict === "APPROVE" ? [] : ["RISK_BUDGET_EXCEEDED"], explain: verdict === "APPROVE" ? [] : ["one lot would risk ₹3,698.97 at the stop, more than the ₹196.42 per-trade budget"], risk_at_stop: 165, budget: 196.42, ...extra }, verdict === "APPROVE" ? REAL_FLOWS.APPROVE : REAL_FLOWS.REJECT);
const order = ev("ORDER", { kind: "ENTRY", side: "BUY", qty: 65, price: 29.45, symbol: "NIFTY 06OCT26 24950 PE" }, REAL_FLOWS.ORDER);
const fill = (pnl: number) => ev("FILL", { kind: "ENTRY", side: "BUY", qty: 65, price: 29.45, charges: 41.2, symbol: "NIFTY 06OCT26 24950 PE", pnl }, REAL_FLOWS.FILL);
const regionsTouched = (cs: Command[]): Set<RegionId> => {
  const s = new Set<RegionId>();
  for (const c of cs) {
    if (c.type === "path") (s.add(c.from), s.add(c.to));
    else if (c.type === "fire" || c.type === "terminate" || c.type === "label") s.add(c.region);
  }
  return s;
};

describe("every real flow maps onto the organism", () => {
  it("every flow endpoint is a region and every cross-region flow has a pathway", () => {
    for (const flows of Object.values(REAL_FLOWS))
      for (const [a, b] of flows) {
        const ra = regionOfNode(a), rb = regionOfNode(b);
        expect(ra, a).not.toBeNull();
        expect(rb, b).not.toBeNull();
        if (ra !== rb) expect(pathwayIndex(ra!, rb!), `${a}→${b}`).not.toBeNull();
      }
  });
  it("there is no pathway the stream never uses", () => {
    const used = new Set<number>();
    for (const flows of Object.values(REAL_FLOWS))
      for (const [a, b] of flows) {
        const pw = pathwayIndex(regionOfNode(a)!, regionOfNode(b)!);
        if (pw) used.add(pw.index);
      }
    // the lifecycle pathway (research → strategy) carries validation → strategy promotions/demotions; the rest are flows
    const lifecycle = pathwayIndex("RESEARCH", "STRATEGY")!.index;
    for (let i = 0; i < PATHWAYS.length; i++) if (i !== lifecycle) expect(used.has(i), PATHWAYS[i].join("–")).toBe(true);
  });
  it("every kill switch has exactly one home region", () => {
    for (const id of Object.keys(KILL_REGION)) expect(REGIONS).toContain(KILL_REGION[id]);
    expect(KILL_REGION.BROKER_CONNECTIVITY_KILL).toBe("EXECUTION");
    expect(KILL_REGION.DATA_QUALITY_KILL).toBe("PERCEPTION");
    expect(KILL_REGION.DAILY_LOSS_KILL).toBe("RISK");
  });
});

describe("a rejection terminates at RISK", () => {
  const cs = commandsFor(decision("REJECT"));
  it("terminates at RISK, in red, and nothing reaches PORTFOLIO, EXECUTION or the broker", () => {
    expect(cs.filter((c) => c.type === "terminate")).toEqual([expect.objectContaining({ region: "RISK", tone: "red" })]);
    expect(cs.some((c) => c.type === "path")).toBe(false);
    const t = regionsTouched(cs);
    expect(t.has("EXECUTION")).toBe(false);
    expect(t.has("BROKER")).toBe(false);
  });
  it("the annotation gives the Governor's own reason, anchored on the check that produced it", () => {
    const l = cs.find((c) => c.type === "label" && c.region === "RISK") as any;
    expect(l.tone).toBe("red");
    expect(l.title).toBe("Risk · rejected");
    expect(l.text).toBe("Per-trade exposure exceeds the permitted budget");
    expect(l.detail).toContain("₹196.42 per-trade budget");
    expect(l.assembly).toBe("R.BUDGET");
    expect(cs.find((c) => c.type === "terminate")).toMatchObject({ assembly: "R.BUDGET" });
    expect(cs.some((c) => c.type === "fire" && c.assembly === "R.BUDGET" && c.tone === "red")).toBe(true);
  });
  it("Tony's thought is REJECTED while it plays", () => {
    expect(transientCognition(decision("REJECT"))).toEqual({ verb: "REJECTED", subject: "S-ORB-001 at Risk · per-trade exposure exceeds the permitted budget" });
  });
  it("on a rejection PORTFOLIO's allocation and position assemblies stay dark (only sizing ran)", () => {
    const all = [intent, decision("REJECT")].flatMap((e) => commandsFor(e));
    const pf = all.filter((c) => (c.type === "fire" || c.type === "label") && c.region === "PORTFOLIO");
    expect(pf.every((c: any) => c.assembly === "PF.SIZING")).toBe(true);
  });
});

describe("an approval propagates to EXECUTION and the broker confirmation returns", () => {
  const ch = new Choreographer();
  const t0 = 1000;
  const sched = [intent, decision("APPROVE"), order, fill(0)].flatMap((e) => ch.schedule(e, commandsFor(e), t0));
  const at = (pred: (c: Command) => boolean): number => {
    const s = sched.find((x) => pred(x.cmd));
    expect(s).toBeDefined();
    return s!.at;
  };
  const path = (from: RegionId, to: RegionId) => at((c) => c.type === "path" && c.from === from && c.to === to);
  it("plays strategy → portfolio → risk → execution → broker → execution, in that order", () => {
    const order_ = [path("STRATEGY", "PORTFOLIO"), path("PORTFOLIO", "RISK"), path("RISK", "EXECUTION"), path("EXECUTION", "BROKER"), path("BROKER", "EXECUTION")];
    for (let i = 1; i < order_.length; i++) expect(order_[i]).toBeGreaterThan(order_[i - 1]);
    expect(order_[0]).toBe(t0);
  });
  it("the approval and the fill are green; the strategy's own pattern fires first", () => {
    expect(sched.find((x) => x.cmd.type === "path" && x.cmd.kind === "approve")!.cmd).toMatchObject({ tone: "green" });
    expect(sched.find((x) => x.cmd.type === "path" && x.cmd.kind === "fill" && x.cmd.to === "EXECUTION")!.cmd).toMatchObject({ tone: "green" });
    expect(at((c) => c.type === "strategy" && c.id === "S-ORB-001")).toBe(t0);
  });
  it("events that are not part of the chain do not wait behind it", () => {
    const tick = ev("TICK", { delta: 2 }, REAL_FLOWS.TICK);
    const s = ch.schedule(tick, commandsFor(tick), t0 + 10);
    expect(Math.min(...s.map((x) => x.at))).toBe(t0 + 10);
    expect(ch.busyUntil).toBeGreaterThan(t0 + 6 * HOP_MS);
  });
  it("brightness is activity: a winning and a losing fill map to the same commands", () => {
    expect(commandsFor(fill(5000))).toEqual(commandsFor(fill(-5000)));
  });
  it("Tony is executing while the approved chain plays", () => {
    expect(transientCognition(decision("APPROVE"))?.verb).toBe("EXECUTING");
    expect(transientCognition(order)?.verb).toBe("EXECUTING");
  });
});

describe("a simulate-only (SHADOW / PAPER) approval never reaches EXECUTION", () => {
  const d = ev("DECISION", { strategy: "S-VWAPC-001", stage: "SHADOW", symbol: "X", verdict: "APPROVE", reasons: [], explain: [], simulate_only: true }, [["risk_governor", "allocator", "approve"]]);
  it("returns to PORTFOLIO, is labelled simulate-only, and Tony is not 'executing'", () => {
    const cs = commandsFor(d);
    const t = regionsTouched(cs);
    expect(t.has("EXECUTION")).toBe(false);
    expect(t.has("BROKER")).toBe(false);
    expect(cs.find((c) => c.type === "path")).toMatchObject({ from: "RISK", to: "PORTFOLIO" });
    expect((cs.find((c) => c.type === "label") as any).title).toContain("simulate-only");
    expect(transientCognition(d)?.verb).toBe("EVALUATING");
    const st = new DashState();
    st.apply(d);
    expect(fieldState(st, Date.parse(TS) + 1000).trail).toMatchObject({ approved: true, terminal: "RISK" });
  });
});

describe("a kill changes only the affected region", () => {
  const sw = (id: string, latched: boolean) => ({ id, latched, scope: null, reason: latched ? "stream/link down 15.0s > 10s" : "", at: latched ? TS : null });
  const kills = ev("KILLS", { switches: [sw("BROKER_CONNECTIVITY_KILL", true), sw("DAILY_LOSS_KILL", false)], halts: [] });
  it("a newly latched broker-link kill fires and labels EXECUTION only", () => {
    const cs = commandsFor(kills);
    expect([...regionsTouched(cs)]).toEqual(["EXECUTION"]);
    expect(cs.every((c) => c.type !== "fire" || c.tone === "red")).toBe(true);
  });
  it("a re-sent KILLS for a switch already latched does nothing", () => {
    expect(commandsFor(kills, { latchedBefore: new Set(["BROKER_CONNECTIVITY_KILL"]) })).toEqual([]);
  });
  it("standing state: EXECUTION is in alarm and every other region is not", () => {
    const st = new DashState();
    st.apply(kills);
    const fs = fieldState(st, Date.parse(TS));
    expect(fs.regions.EXECUTION.state).toBe("alarm");
    for (const id of REGIONS) if (id !== "EXECUTION") expect(fs.regions[id].state, id).not.toBe("alarm");
  });
  it("a scoped strategy kill dims only that strategy's pattern", () => {
    const st = new DashState();
    st.apply(ev("STRATEGIES", { rows: STRATS.map((id) => ({ id, stage: id === "S-ORB-001" ? "CANARY" : "PAPER" })) }));
    st.apply(ev("KILLS", { switches: [{ id: "STRATEGY_KILL", latched: true, scope: ["S-ORB-001"], reason: "3 losses", at: TS }], halts: [] }));
    const fs = fieldState(st, Date.parse(TS));
    for (const id of REGIONS) expect(fs.regions[id].state, id).not.toBe("alarm");
    const p = fs.strategies.find((s) => s.id === "S-ORB-001")!;
    expect(p.killed).toBe(true);
    expect(p.tone).toBe("red");
    expect(fs.strategies.filter((s) => s.killed).length).toBe(1);
  });
});

describe("quiet things stay quiet", () => {
  it("a tick touches only the broker port, perception and the strategies' feed, at low strength", () => {
    const cs = commandsFor(ev("TICK", { delta: 1.5 }, REAL_FLOWS.TICK));
    expect([...regionsTouched(cs)].sort()).toEqual(["BROKER", "PERCEPTION", "STRATEGY"]);
    for (const c of cs) if ("strength" in c) expect(c.strength).toBeLessThanOrEqual(0.4);
    expect(cs.some((c) => c.type === "label")).toBe(false);
  });
  it("a bigger move is more perception activity (capped)", () => {
    const s = (d: number) => (commandsFor(ev("TICK", { delta: d }, REAL_FLOWS.TICK))[0] as any).strength;
    expect(s(8)).toBeGreaterThan(s(1));
    expect(s(500)).toBe(0.3);
  });
  it("no command ever claims a confidence the system does not produce", () => {
    const all = [intent, decision("APPROVE"), decision("REJECT"), order, fill(1), ev("REGIME", { tags: ["VOLATILITY_EXPANSION"], ret_30m_pct: 0.2, vix: 13 }, REAL_FLOWS.REGIME)].flatMap((e) => commandsFor(e));
    for (const c of all) if (c.type === "label") expect(`${c.title} ${c.text}`.toLowerCase()).not.toContain("confiden");
  });
  it("a regime label is what Market Intelligence tagged", () => {
    const cs = commandsFor(ev("REGIME", { tags: ["VOLATILITY_EXPANSION", "TRENDING_UP"], ret_30m_pct: 0.31, vix: 13.2 }, REAL_FLOWS.REGIME));
    const l = cs.find((c) => c.type === "label") as any;
    expect(l.region).toBe("PERCEPTION");
    expect(l.text.toLowerCase()).toContain("volatility expansion · trending up");
    expect(l.title).toBe("Market perception");
    expect(l.assembly).toBe("P.REGIME");
  });
});

describe("standing state from the folded stream", () => {
  it("the last decision leaves a trail for TRAIL_S seconds: rejected ends at RISK, approved at the broker", () => {
    const st = new DashState();
    st.apply(decision("REJECT"));
    let fs = fieldState(st, Date.parse(TS) + 5000);
    expect(fs.trail).toMatchObject({ approved: false, terminal: "RISK", regions: ["STRATEGY", "PORTFOLIO", "RISK"] });
    expect(fieldState(st, Date.parse(TS) + (TRAIL_S + 1) * 1000).trail).toBeNull();
    st.apply(decision("APPROVE"));
    fs = fieldState(st, Date.parse(TS) + 1000);
    expect(fs.trail).toMatchObject({ approved: true, terminal: "BROKER" });
    expect(fs.trail!.regions).toContain("EXECUTION");
  });
  it("a closed session puts every region to quiet", () => {
    const st = new DashState();
    st.apply(ev("DAY_END", { summary: {} }));
    const fs = fieldState(st, Date.parse(TS));
    expect(fs.closed).toBe(true);
    for (const id of REGIONS) expect(fs.regions[id].state).toBe("quiet");
  });
  it("lifecycle stage sets a pattern's resting brightness (retired is nearly dark)", () => {
    const st = new DashState();
    st.apply(ev("STRATEGIES", { rows: [{ id: "A", stage: "RETIRED" }, { id: "B", stage: "RESEARCH" }, { id: "C", stage: "CANARY" }] }));
    const b = Object.fromEntries(fieldState(st, Date.parse(TS)).strategies.map((s) => [s.id, s.base]));
    expect(b.A).toBeLessThan(b.B);
    expect(b.B).toBeLessThan(b.C);
  });
});

describe("Tony's thought line", () => {
  it("comes from the backend's cognition, overridden only while a real chain plays", () => {
    const base = { verb: "WATCHING", subject: "Opening-range breakout / NIFTY" };
    expect(cognitionLine(base, null)).toEqual(base);
    expect(cognitionLine(base, { verb: "EXECUTING", subject: "X" }).verb).toBe("EXECUTING");
    expect(cognitionLine(null, null).verb).toBe("CONNECTING");
  });
});

describe("the four cognitive states come from Tony's real cognition and the chain step playing", () => {
  it("each chain step maps to its state, and nothing else does", () => {
    expect(phaseOf(intent)).toBe("EVALUATING");
    expect(phaseOf(decision("REJECT"))).toBe("REJECTED");
    expect(phaseOf(decision("APPROVE"))).toBe("EXECUTING");
    expect(phaseOf(order)).toBe("EXECUTING");
    expect(phaseOf(fill(0))).toBe("EXECUTING");
    expect(phaseOf(ev("TICK", { delta: 1 }, REAL_FLOWS.TICK))).toBeNull();
    expect(phaseOf(ev("DECISION", { strategy: "S", verdict: "APPROVE", simulate_only: true, stage: "SHADOW" }, []))).toBe("EVALUATING");
  });
  it("a state holds for its window after the step played, then the connectome returns to WATCHING", () => {
    expect(cognitiveState("WATCHING", null, 10)).toBe("WATCHING");
    expect(cognitiveState("WATCHING", { state: "REJECTED", at: 10 }, 12)).toBe("REJECTED");
    expect(cognitiveState("WATCHING", { state: "REJECTED", at: 10 }, 10 + HOLD_S.REJECTED + 0.1)).toBe("WATCHING");
    expect(cognitiveState("OBSERVING", { state: "EVALUATING", at: 10 }, 11)).toBe("EVALUATING");
    expect(cognitiveState("MANAGING", { state: "EXECUTING", at: 10 }, 21)).toBe("EXECUTING");
    expect(cognitiveState("MANAGING", { state: "EXECUTING", at: 10 }, 30)).toBe("WATCHING");
  });
  it("Tony's own verbs: EXECUTING (a working entry order) is EXECUTING, a latched kill is HALTED, the rest is WATCHING", () => {
    expect(cognitiveState("EXECUTING", null, 0)).toBe("EXECUTING");
    expect(cognitiveState("HALTED", { state: "EXECUTING", at: 0 }, 1)).toBe("HALTED");
    for (const v of ["OBSERVING", "WATCHING", "WAITING", "MANAGING", "RESTING", "UNKNOWN", undefined]) expect(cognitiveState(v, null, 0)).toBe("WATCHING");
  });
  it("a step not yet played does not change the state early", () => {
    expect(cognitiveState("WATCHING", { state: "EXECUTING", at: 20 }, 19)).toBe("WATCHING");
  });
});

describe("synaptic afterglow: 5–20 s of decay, driven only by real firings", () => {
  it("is visible after 5 s, faint after 15 s and gone (< 1 %) by 20 s", () => {
    expect(afterglow(1, 0)).toBeCloseTo(1.2, 5);
    expect(afterglow(1, 5)).toBeGreaterThan(0.05);
    expect(afterglow(1, 15)).toBeLessThan(0.03);
    expect(afterglow(1, 20)).toBeLessThan(0.01);
    expect(GLOW_TAU_S).toBeGreaterThanOrEqual(5);
  });
  it("is nothing without a firing, and monotone after one", () => {
    expect(afterglow(0, 3)).toBe(0);
    expect(afterglow(1, -1)).toBe(0);
    let prev = Infinity;
    for (let t = 0; t <= 20; t += 0.5) {
      expect(afterglow(0.8, t)).toBeLessThan(prev);
      prev = afterglow(0.8, t);
    }
  });
});

describe("strategies are thought patterns: eight assembly states", () => {
  const now = Date.parse(TS);
  const at = (s: number) => new Date(now - s * 1000).toISOString();
  it("each lifecycle stage maps to its resting pattern state", () => {
    const st = (stage: string) => strategyPattern({ id: "X", stage }, undefined, new Set(), now, false).state;
    expect(st("RETIRED")).toBe("dormant");
    for (const s of ["RESEARCH", "BACKTESTED", "VALIDATED"]) expect(st(s)).toBe("researching");
    for (const s of ["PAPER", "SHADOW"]) expect(st(s)).toBe("candidate");
    for (const s of ["CANARY", "PRODUCTION"]) expect(st(s)).toBe("live");
    for (const s of ["DEGRADED", "QUARANTINED"]) expect(st(s)).toBe("paused");
    expect(Object.keys(STAGE_PATTERN).length).toBe(10);
  });
  it("dormant is barely visible; formation grows researching → candidate → live; evaluating fires hardest", () => {
    const L = PATTERN_LOOK;
    expect(L.dormant.base).toBeLessThan(L.researching.base);
    expect(L.researching.formation).toBeLessThan(L.candidate.formation);
    expect(L.candidate.formation).toBeLessThan(L.live.formation);
    expect(L.evaluating.base).toBeGreaterThan(L.live.base);
    expect(L.live.base).toBeLessThanOrEqual(0.25); // persistent but restrained
  });
  it("a recent approved decision is evaluating; a recent rejection decays apart over PATTERN_S", () => {
    const row = { id: "X", stage: "CANARY" };
    expect(strategyPattern(row, { verdict: "APPROVE", ts: at(3) }, new Set(), now, false).state).toBe("evaluating");
    const r1 = strategyPattern(row, { verdict: "REJECT", ts: at(1) }, new Set(), now, false);
    const r2 = strategyPattern(row, { verdict: "REJECT", ts: at(PATTERN_S * 0.8) }, new Set(), now, false);
    expect(r1.state).toBe("rejected");
    expect(r1.tone).toBe("red");
    expect(r2.formation).toBeLessThan(r1.formation);
    expect(r2.base).toBeLessThan(r1.base);
    expect(strategyPattern(row, { verdict: "REJECT", ts: at(PATTERN_S + 1) }, new Set(), now, false).state).toBe("live");
  });
  it("a scoped kill wins over everything; P&L never enters", () => {
    expect(strategyPattern({ id: "X", stage: "CANARY" }, { verdict: "APPROVE", ts: at(1) }, new Set(["X"]), now, false).state).toBe("killed");
    expect(strategyPattern.toString()).not.toMatch(/pnl|profit/i);
  });
});

describe("meso: events address real assemblies", () => {
  const asm = assemblies(STRATS);
  it("one assembly per strategy (capped), and fixed assemblies in every region", () => {
    expect(asm.filter((a) => a.region === "STRATEGY").map((a) => a.title)).toEqual(STRATS);
    expect(assemblies(Array.from({ length: 20 }, (_, i) => `S${i}`)).filter((a) => a.region === "STRATEGY").length).toBe(MAX_STRATEGIES);
    for (const id of REGIONS) expect(asm.some((a) => a.region === id), id).toBe(true);
    expect(new Set(asm.map((a) => a.id)).size).toBe(asm.length);
  });
  it("every Governor reason code (kernel/governor.py) has a gating assembly inside RISK", () => {
    const py: string = governorPy;
    const body = py.slice(py.indexOf("class Reason"), py.indexOf("@dataclass", py.indexOf("class Reason")));
    const codes = [...body.matchAll(/^\s+([A-Z_]+) = "/gm)].map((m) => m[1]);
    expect(codes.length).toBeGreaterThan(30);
    for (const c of codes) expect(assemblyRegion(REASON_ASSEMBLY[c] ?? ""), c).toBe("RISK");
    expect(Object.keys(REASON_ASSEMBLY).sort()).toEqual([...codes].sort());
  });
  it("every kill's assembly lives in the kill's home region", () => {
    for (const [k, a] of Object.entries(KILL_ASSEMBLY)) expect(assemblyRegion(a), k).toBe(KILL_REGION[k]);
  });
  it("orders and fills land on the matching execution process; a reject with unknown codes fires the region only", () => {
    const o = (kind: string) => commandsFor(ev("ORDER", { kind, side: "SELL", qty: 65, price: 20, symbol: "X" }, REAL_FLOWS.ORDER)).find((c) => c.type === "fire" && c.region === "EXECUTION") as any;
    expect(o("ENTRY").assembly).toBe("E.ENTRY");
    expect(o("PROTECTIVE").assembly).toBe("E.STOP");
    expect(o("EXIT").assembly).toBe("E.EXIT");
    expect(rejectingAssemblies({ reasons: ["SOMETHING_NEW"] })).toEqual([]);
    expect(rejectingAssemblies({ reasons: ["COOLDOWN", "DAILY_HEADROOM", "MAX_POSITION"] })).toEqual(["R.POSITION", "R.HEADROOM"]);
  });
  it("a tick lands on the feed; every fed strategy's assembly receives the bar (ideas compete), only the proposer goes on", () => {
    const cs = commandsFor(ev("TICK", { delta: 1 }, REAL_FLOWS.TICK));
    expect(cs.some((c) => c.type === "fire" && c.assembly === "P.FEED")).toBe(true);
    expect(cs.filter((c) => c.type === "strategy").length).toBe(3);
    const ic = commandsFor(intent);
    expect(ic.filter((c) => c.type === "strategy").map((c: any) => c.id)).toEqual(["S-ORB-001"]);
    expect(ic.find((c) => c.type === "path" && c.to === "RISK")).toMatchObject({ tone: "amber" }); // the hypothesis under evaluation
  });
  it("a scoped strategy kill lands on that strategy's assembly only", () => {
    const cs = commandsFor(ev("KILLS", { switches: [{ id: "STRATEGY_KILL", latched: true, scope: ["S-FBO-001"], reason: "3 losses", at: TS }], halts: [] }));
    expect(cs.filter((c) => c.type === "fire" && c.assembly).map((c: any) => c.assembly)).toEqual(["S:S-FBO-001"]);
  });
  it("the standing state keeps the open position and the protective stop as assembly sustain", () => {
    const st = new DashState();
    st.apply(ev("POSITION", { open: true, symbol: "X", protective_confirmed: true }));
    const fs = fieldState(st, Date.parse(TS));
    expect(fs.assemblies["PF.POSITION"]).toMatchObject({ tone: "green" });
    expect(fs.assemblies["E.STOP"]).toMatchObject({ tone: "green" });
  });
});

describe("the anatomy", () => {
  const a = buildAnatomy(STRATS);
  const P = a.points;
  it("is deterministic and dense: thousands of cells and synapses in one organism", () => {
    expect(buildAnatomy(STRATS).points.position).toEqual(P.position);
    expect(P.count).toBeGreaterThan(10000);
    expect(a.lines.count / 2).toBeGreaterThan(8000);
    expect(P.a.length).toBe(P.count * 4);
  });
  it("three scales: every region has cells, every assembly has cells, and tissue joins them", () => {
    const reg = new Map<number, number>();
    const asm = new Map<number, number>();
    for (let i = 0; i < P.count; i++) {
      reg.set(P.a[i * 4], (reg.get(P.a[i * 4]) ?? 0) + 1);
      if (P.a[i * 4 + 1] >= 0) asm.set(P.a[i * 4 + 1], (asm.get(P.a[i * 4 + 1]) ?? 0) + 1);
    }
    for (let i = 0; i < REGIONS.length; i++) expect(reg.get(i) ?? 0, REGIONS[i]).toBeGreaterThan(60);
    a.assemblies.forEach((_, i) => expect(asm.get(i) ?? 0, a.assemblies[i].id).toBeGreaterThan(20));
    let tissue = 0;
    for (let i = 0; i < P.count; i++) if (P.c[i * 4 + 3] === KIND.tissue) tissue++;
    expect(tissue).toBeGreaterThan(4000);
  });
  it("axonal bundles of 5–30 filaments on every real pathway; some branch, some end early, some stay dormant", () => {
    expect(a.bundles.length).toBe(PATHWAYS.length);
    a.bundles.forEach((b, i) => {
      expect(b.filaments).toBe(FILAMENTS[i]);
      expect(b.filaments).toBeGreaterThanOrEqual(5);
      expect(b.filaments).toBeLessThanOrEqual(30);
      expect(b.dormant).toBeLessThan(b.filaments - 2);
    });
    const sum = (k: "branches" | "terminated" | "dormant") => a.bundles.reduce((s, b) => s + b[k], 0);
    expect(sum("branches")).toBeGreaterThan(30);
    expect(sum("terminated")).toBeGreaterThan(15);
    expect(sum("dormant")).toBeGreaterThan(15);
    let fib = 0;
    for (let v = 0; v < a.lines.count; v++) if (a.lines.c[v * 4 + 3] === LKIND.fibre) fib++;
    expect(fib / 2).toBeGreaterThan(4000);
  });
  it("it stays dark: 80–90 % of the cells rest below the barely-visible level", () => {
    let low = 0;
    for (let i = 0; i < P.count; i++) if (P.b[i * 4 + 1] < BARELY) low++;
    const f = low / P.count;
    expect(f).toBeGreaterThanOrEqual(0.8);
    expect(f).toBeLessThanOrEqual(0.92);
  });
  it("a bilateral cortex by density only: two lobes, a thinner but never empty midline", () => {
    const lobe = (cortexDensity(-1.0, 0.5) + cortexDensity(1.0, 0.5)) / 2;
    expect(cortexDensity(0.02, 0.35)).toBeLessThan(lobe);
    expect(cortexDensity(0.02, 0.35)).toBeGreaterThan(0);
    expect(cortexDensity(0.02, 0.0)).toBeGreaterThan(cortexDensity(0.02, 0.35) * 0.9); // the core bridges it
    expect(cortexDensity(2.6, 1.2)).toBe(0);
  });
  it("Tony's core is the hub: the densest region per volume, with the most pathways, and deep", () => {
    const deg = (id: RegionId) => PATHWAYS.filter(([x, y]) => x === id || y === id).length;
    for (const id of COGNITIVE) expect(deg("CORE")).toBeGreaterThanOrEqual(deg(id) - 1);
    let zmin = Infinity, zmax = -Infinity;
    const core = REGIONS.indexOf("CORE");
    for (let i = 0; i < P.count; i++) if (P.a[i * 4] === core) (zmin = Math.min(zmin, P.position[i * 3 + 2])), (zmax = Math.max(zmax, P.position[i * 3 + 2]));
    expect(zmax - zmin).toBeGreaterThan(0.5);
    let coreLines = 0;
    for (let v = 0; v < a.lines.count; v++) if (a.lines.c[v * 4 + 3] === LKIND.core) coreLines++;
    expect(coreLines / 2).toBeGreaterThan(400);
  });
  it("depth: the organism spans foreground to a deep, defocused layer", () => {
    let zmin = Infinity, zmax = -Infinity, deep = 0;
    for (let i = 0; i < P.count; i++) {
      zmin = Math.min(zmin, P.position[i * 3 + 2]);
      zmax = Math.max(zmax, P.position[i * 3 + 2]);
      if (P.c[i * 4 + 3] === KIND.deep) deep++;
    }
    expect(zmax - zmin).toBeGreaterThan(1.4);
    expect(deep).toBeGreaterThan(200);
  });
  it("fibres are not tied to regions (a region fire never lights a bundle; only an impulse does)", () => {
    for (let i = 0; i < P.count; i++) if (P.a[i * 4 + 2] >= 0) expect(P.a[i * 4]).toBe(TISSUE);
  });
});

describe("⌘K reaches every operational layer", () => {
  it("has a 'Focus …' command for each of the six regions, none order-like", async () => {
    const { buildCommands } = await import("../src/model/palette");
    const cmds = buildCommands({ questions: [], replay: false, voiceOn: false, hasPosition: false, replayDays: [] });
    const layers = cmds.filter((c) => c.id.startsWith("layer:"));
    expect(layers.map((c) => c.id.slice(6)).sort()).toEqual([...COGNITIVE].sort());
    for (const c of layers) expect(/order|buy|sell|flatten|pause|resume|limit|promote/i.test(c.title + c.id)).toBe(false);
  });
});
