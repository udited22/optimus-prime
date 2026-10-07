// Client-side state folded from the event stream (live) or a recorded day (replay). No numbers are made up
// here: every field is a copy of, or arithmetic on, a SIMULATED event payload.
import { istMinutes } from "./fmt";
import type { DashEvent } from "./types";

export interface Decision {
  ts: string;
  intent_id: string;
  strategy: string;
  stage: string;
  symbol: string;
  verdict: string;
  reasons: string[];
  explain: string[];
  simulate_only: boolean;
  risk_at_stop: string | null;
  budget: string | null;
}

export interface FlowMark { kind: string; ts: number }

export class DashState {
  session: any = null;
  tick: any = null;
  tickTs = "";
  series: [number, number][] = [];
  vixOpen: number | null = null;
  chain: any = null;
  regime: any = null;
  strategies: any = null;
  position: any = null;
  risk: any = null;
  kills: any = null;
  phase = "";
  tony: any = null;
  log: DashEvent[] = [];
  lastTs = "";
  seq = 0;
  dayEnd: any = null;
  economics: any = null; // docs/risk/system-economics.md net-of-everything snapshot (advisory only)
  decisions: Decision[] = [];
  lastDecision: Record<string, Decision> = {};
  fills: DashEvent[] = [];
  /** last time each edge carried a flow (sim ms) — the graph lights only edges with real traffic */
  flowAt: Record<string, FlowMark> = {};
  counts: Record<string, number> = {};
  /** honesty counters for the global mode indicator (model/mode.ts) */
  simulatedSeen = 0;
  realSeen = 0;
  dirty = true;

  reset(): void {
    const keep = { simulatedSeen: this.simulatedSeen, realSeen: this.realSeen };
    Object.assign(this, new DashState(), keep);
  }

  apply(e: DashEvent): void {
    this.seq = Math.max(this.seq, e.seq);
    this.lastTs = e.ts;
    this.counts[e.kind] = (this.counts[e.kind] ?? 0) + 1;
    if (e.simulated === false) this.realSeen++;
    else this.simulatedSeen++;
    const t = Date.parse(e.ts);
    for (const [a, b, k] of e.flows) this.flowAt[`${a}>${b}`] = { kind: k, ts: t };
    const d = e.data;
    switch (e.kind) {
      case "SESSION":
        this.session = d;
        this.series = [];
        this.vixOpen = null;
        this.decisions = [];
        this.lastDecision = {};
        this.fills = [];
        this.dayEnd = null;
        this.economics = null;
        this.position = { open: false };
        break;
      case "TICK":
        this.tick = d;
        this.tickTs = e.ts;
        this.series.push([istMinutes(e.ts), Number(d.spot)]);
        if (this.series.length > 1600) this.series.shift();
        if (this.vixOpen === null && d.vix !== undefined) this.vixOpen = Number(d.vix);
        break;
      case "CHAIN":
        this.chain = d;
        break;
      case "REGIME":
        this.regime = d;
        break;
      case "STRATEGIES":
        this.strategies = d;
        break;
      case "DECISION": {
        const dec: Decision = {
          ts: e.ts,
          intent_id: d.intent_id,
          strategy: d.strategy,
          stage: d.stage,
          symbol: d.symbol,
          verdict: d.verdict,
          reasons: d.reasons ?? [],
          explain: d.explain ?? [],
          simulate_only: !!d.simulate_only,
          risk_at_stop: d.risk_at_stop ?? null,
          budget: d.budget ?? null,
        };
        this.decisions.push(dec);
        this.lastDecision[d.strategy] = dec;
        break;
      }
      case "FILL":
        this.fills.push(e);
        break;
      case "POSITION":
        this.position = d;
        break;
      case "RISK":
        this.risk = d;
        break;
      case "KILLS":
        this.kills = d;
        break;
      case "WINDOW":
        this.phase = d.phase;
        break;
      case "TONY":
        this.tony = d;
        break;
      case "LOG":
        this.log.push(e);
        if (this.log.length > 400) this.log.shift();
        break;
      case "DAY_END":
        this.dayEnd = d;
        break;
      case "ECONOMICS":
        this.economics = d;
        break;
      default:
        break;
    }
    this.dirty = true;
  }

  anyKill(): boolean {
    return !!this.kills?.switches?.some((s: any) => s.latched);
  }

  latched(): any[] {
    return (this.kills?.switches ?? []).filter((s: any) => s.latched);
  }
}
