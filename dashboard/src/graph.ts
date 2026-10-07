// The AI decision graph (docs/architecture/observability.md §17.5, v3). A calm SVG map of how Tony's agents and the kernel are wired.
// Rules: edges are drawn only where a real relationship exists; an edge lights only when a real SIMULATED event
// crosses it; nothing spins and nothing moves at random. A rejection lights the strategy → allocator → Risk
// Governor path in amber and ends at the Governor with a short red cap. SVG is resolution-independent, so the
// graph is sharp at any devicePixelRatio without a canvas.
import { receivesFeed } from "./model/lifecycle";
import { inrSigned, istHM, num } from "./fmt";
import type { DashState } from "./state";
import type { Topology } from "./types";

const NS = "http://www.w3.org/2000/svg";
type Pt = { x: number; y: number };
type LabelSide = "below" | "right" | "above";
interface NodeSpec { id: string; at: Pt; side: LabelSide; r: number; title: string }
interface Edge { key: string; a: string; b: string; path: SVGPathElement; glow: SVGPathElement; structural: boolean; curve?: Pt }
interface Pulse { edge: Edge; reverse: boolean; kind: string; t0: number; dur: number; dot: SVGCircleElement }

// positions as fractions of the graph box; the decision path runs left → right along the middle row
const ROW = 0.6;
const BASE: Record<string, { at: Pt; side: LabelSide; title: string }> = {
  market_intel: { at: { x: 0.07, y: ROW }, side: "below", title: "Market Intelligence" },
  data_quality: { at: { x: 0.19, y: ROW }, side: "below", title: "Data Quality" },
  validation: { at: { x: 0.19, y: 0.17 }, side: "below", title: "Validation" },
  strategy_factory: { at: { x: 0.34, y: 0.17 }, side: "below", title: "Strategy Factory" },
  cio: { at: { x: 0.5, y: 0.3 }, side: "below", title: "TONY · Trading CIO" },
  allocator: { at: { x: 0.5, y: ROW }, side: "below", title: "Portfolio Allocator" },
  risk_governor: { at: { x: 0.645, y: ROW }, side: "below", title: "Risk Governor" },
  execution: { at: { x: 0.785, y: ROW }, side: "below", title: "Execution" },
  broker: { at: { x: 0.925, y: ROW }, side: "below", title: "Broker" },
  post_trade: { at: { x: 0.785, y: 0.24 }, side: "below", title: "Post-trade" },
};
// short names when the graph is narrow (16-inch laptop), so row labels never collide
const SHORT: Record<string, string> = {
  market_intel: "Market Intel", data_quality: "Data Quality", allocator: "Allocator", risk_governor: "Risk Governor",
  execution: "Execution", broker: "Broker", post_trade: "Post-trade", strategy_factory: "Strategy Factory", validation: "Validation",
};
const STRAT_X = 0.335;
const STRAT_Y = [0.37, 0.455, 0.54, 0.625, 0.71];
const STAGE_ORDER = ["RESEARCH", "BACKTESTED", "VALIDATED", "PAPER", "SHADOW", "CANARY", "PRODUCTION", "DEGRADED", "QUARANTINED", "RETIRED"];
// edges that carry a standing relationship and so are always drawn (hairline); every other topology edge is
// invisible until a real flow crosses it
const STRUCTURAL = new Set([
  "broker>market_intel", "market_intel>data_quality", "market_intel>cio", "cio>allocator", "cio>strategy_factory",
  "strategy_factory>validation", "allocator>risk_governor", "risk_governor>execution", "risk_governor>cio",
  "execution>broker", "execution>post_trade", "post_trade>cio", "post_trade>validation",
]);
const CURVES: Record<string, Pt> = {
  "broker>market_intel": { x: 0.5, y: 1.1 },
  "post_trade>validation": { x: 0.49, y: -0.04 },
  "market_intel>cio": { x: 0.1, y: 0.28 },
};
// the two long loops say what they carry, so they read as relationships rather than ornament
const CURVE_LABEL: Record<string, string> = {
  "broker>market_intel": "market data · fake broker feed",
  "post_trade>validation": "post-trade evidence → lifecycle validation",
};
const KIND_CLASS: Record<string, string> = {
  intent: "k-accent", approve: "k-accent", order: "k-accent", ack: "k-accent", fill: "k-pos", reject: "k-warn",
  kill: "k-neg", regime: "k-soft", budget: "k-soft", report: "k-soft", promote: "k-soft",
};
const QUIET = new Set(["tick", "clean"]); // continuous data: shown as a steady state, never as pulses
const REGIME_WORD: Record<string, string> = {
  TRENDING_UP: "Trending up", TRENDING_DOWN: "Trending down", MEAN_REVERTING: "Range-bound",
  VOLATILITY_EXPANSION: "vol expanding", VOLATILITY_COMPRESSION: "vol compressed", EXPIRY_DAY: "expiry day",
  VOLATILITY_NORMAL: "vol normal", OPENING_DRIVE: "opening drive", OPENING_REVERSION: "opening reversion", GAP_REGIME: "gap day", EXPIRY_REGIME: "expiry day", EVENT_REGIME: "event day", NO_EDGE: "no edge", ABNORMAL_MARKET: "abnormal",
};

const el = <K extends keyof SVGElementTagNameMap>(tag: K, attrs: Record<string, string | number> = {}, cls = ""): SVGElementTagNameMap[K] => {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
  if (cls) e.setAttribute("class", cls);
  return e;
};

export class DecisionGraph {
  private svg: SVGSVGElement;
  private gEdges: SVGGElement;
  private gGlow: SVGGElement;
  private gOverlay: SVGGElement;
  private gNodes: SVGGElement;
  private gPulse: SVGGElement;
  private nodes = new Map<string, NodeSpec>();
  private nodeEls = new Map<string, { g: SVGGElement; sub: SVGTextElement; ring: SVGCircleElement }>();
  private edges = new Map<string, Edge>();
  private pulses: Pulse[] = [];
  private w = 0;
  private h = 0;
  private stages: Record<string, string> = {};
  private last: { st: DashState; simNow: number } | null = null;
  private reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  constructor(private host: HTMLElement, private topo: Topology) {
    this.svg = el("svg", { role: "img", "aria-label": "Decision graph: Tony, the agents and the kernel" }, "graph");
    this.gEdges = el("g");
    this.gGlow = el("g");
    this.gOverlay = el("g");
    this.gNodes = el("g");
    this.gPulse = el("g");
    this.svg.append(this.gEdges, this.gGlow, this.gOverlay, this.gNodes, this.gPulse);
    host.append(this.svg);
    for (const s of topo.strategies) this.stages[s.node] = s.stage;
    new ResizeObserver(() => this.layout()).observe(host);
    this.layout();
  }

  setStages(stages: Record<string, string>): void {
    const changed = Object.entries(stages).some(([k, v]) => this.stages[k] !== v);
    Object.assign(this.stages, stages);
    if (changed) this.layout();
  }

  private P(p: Pt): Pt {
    return { x: p.x * this.w, y: p.y * this.h };
  }

  private layout(): void {
    const w = Math.round(this.host.clientWidth);
    const h = Math.round(this.host.clientHeight);
    if (w < 50 || h < 50) return;
    this.w = w;
    this.h = h;
    this.svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    this.svg.setAttribute("width", String(w));
    this.svg.setAttribute("height", String(h));
    for (const g of [this.gEdges, this.gGlow, this.gOverlay, this.gNodes, this.gPulse]) g.replaceChildren();
    this.pulses = [];
    this.nodes.clear();
    this.nodeEls.clear();
    this.edges.clear();
    for (const [id, b] of Object.entries(BASE)) this.nodes.set(id, { id, at: b.at, side: b.side, r: id === "cio" ? 32 : 6, title: b.title });
    const strats = [...this.topo.strategies].sort((a, b) => STAGE_ORDER.indexOf(this.stages[a.node] ?? a.stage) - STAGE_ORDER.indexOf(this.stages[b.node] ?? b.stage));
    strats.forEach((s, i) => this.nodes.set(s.node, { id: s.node, at: { x: STRAT_X, y: STRAT_Y[Math.min(i, STRAT_Y.length - 1)] + Math.max(0, i - 4) * 0.08 }, side: "right", r: 4.5, title: s.id }));

    // edges (a pair a>b / b>a shares one geometry)
    for (const [a, b] of this.topo.edges) {
      const key = [a, b].sort().join("~");
      if (this.edges.has(key) || !this.nodes.has(a) || !this.nodes.has(b)) continue;
      const structural = STRUCTURAL.has(`${a}>${b}`) || STRUCTURAL.has(`${b}>${a}`) || this.strategyEdgeDrawn(a, b);
      const curve = CURVES[`${a}>${b}`] ?? CURVES[`${b}>${a}`];
      const d = this.pathD(a, b, curve);
      const path = el("path", { d }, `edge ${structural ? "" : "latent"}`);
      const glow = el("path", { d }, "glow");
      this.gEdges.append(path);
      this.gGlow.append(glow);
      this.edges.set(key, { key, a, b, path, glow, structural, curve });
      const lbl = CURVE_LABEL[`${a}>${b}`] ?? CURVE_LABEL[`${b}>${a}`];
      if (lbl && curve) {
        const A = this.P(this.nodes.get(a)!.at);
        const B = this.P(this.nodes.get(b)!.at);
        const C = this.P(curve);
        const t = el("text", { x: (0.25 * A.x + 0.5 * C.x + 0.25 * B.x).toFixed(1), y: (0.25 * A.y + 0.5 * C.y + 0.25 * B.y + (curve.y > 0.5 ? 14 : -7)).toFixed(1), "text-anchor": "middle" }, "elbl");
        t.textContent = lbl;
        this.gEdges.append(t);
      }
    }
    // nodes
    for (const n of this.nodes.values()) this.drawNode(n);
    if (this.last) this.update(this.last.st, this.last.simNow);
  }

  private strategyEdgeDrawn(a: string, b: string): boolean {
    const s = a.startsWith("strat:") ? a : b.startsWith("strat:") ? b : null;
    if (!s) return false;
    const other = s === a ? b : a;
    const stage = this.stages[s] ?? "RESEARCH";
    if (other === "data_quality" || other === "allocator") return receivesFeed(stage);
    if (other === "strategy_factory") return !receivesFeed(stage);
    return false;
  }

  private pathD(a: string, b: string, curve?: Pt): string {
    const A = this.P(this.nodes.get(a)!.at);
    const B = this.P(this.nodes.get(b)!.at);
    if (curve) {
      const C = this.P(curve);
      return `M${A.x.toFixed(1)},${A.y.toFixed(1)} Q${C.x.toFixed(1)},${C.y.toFixed(1)} ${B.x.toFixed(1)},${B.y.toFixed(1)}`;
    }
    return `M${A.x.toFixed(1)},${A.y.toFixed(1)} L${B.x.toFixed(1)},${B.y.toFixed(1)}`;
  }

  private drawNode(n: NodeSpec): void {
    const p = this.P(n.at);
    const g = el("g", { transform: `translate(${p.x.toFixed(1)},${p.y.toFixed(1)})` }, `node n-${n.id.replace(":", "-")} ${n.id.startsWith("strat:") ? "strat" : ""}`);
    const isTony = n.id === "cio";
    const ring = el("circle", { r: isTony ? n.r + 9 : n.r + 5 }, "ring");
    g.append(ring);
    if (isTony) {
      g.append(el("circle", { r: n.r + 18 }, "halo"));
      g.append(el("circle", { r: n.r }, "core tony"));
      const t = el("text", { y: 4.5, "text-anchor": "middle" }, "tony-name");
      t.textContent = "TONY";
      g.append(t);
    } else g.append(el("circle", { r: n.r }, "core"));
    let sub: SVGTextElement;
    if (n.side === "right") {
      const t = el("text", { x: 12, y: -1 }, "lbl-strat");
      t.textContent = n.title;
      sub = el("text", { x: 12, y: 12 }, "sub");
      g.append(t, sub);
    } else {
      const y0 = n.r + (isTony ? 26 : 19);
      const t = el("text", { y: y0, "text-anchor": "middle" }, isTony ? "lbl tony-lbl" : "lbl");
      t.textContent = isTony ? "TONY · Trading CIO" : (this.w < 1100 ? SHORT[n.id] ?? n.title : n.title).toUpperCase();
      sub = el("text", { y: y0 + 15, "text-anchor": "middle" }, isTony ? "sub tony-sub" : "sub");
      g.append(t, sub);
    }
    const blurb = this.topo.nodes.find((x) => x.id === n.id)?.blurb ?? "";
    const title = el("title");
    title.textContent = `${n.title}: ${blurb}`;
    g.append(title);
    this.gNodes.append(g);
    this.nodeEls.set(n.id, { g, sub, ring });
  }

  /** truncate a caption to the room its node has on the row (≈6 px per character at the caption size) */
  private fit(id: string, text: string): string {
    const n = this.nodes.get(id);
    if (!n || n.side !== "below" || id === "cio") return text;
    let room = Infinity;
    for (const m of this.nodes.values()) if (m.id !== id && m.side === "below" && Math.abs(m.at.y - n.at.y) < 0.05) room = Math.min(room, Math.abs(m.at.x - n.at.x) * this.w);
    const max = Math.max(8, Math.floor((room - 10) / 6));
    return text.length > max ? `${text.slice(0, max - 1)}…` : text;
  }

  private edge(a: string, b: string): { e: Edge; reverse: boolean } | null {
    const e = this.edges.get([a, b].sort().join("~"));
    return e ? { e, reverse: e.a !== a } : null;
  }

  /** A real event crossed a → b. `delay` lets one event's hops read in causal order. */
  pulse(a: string, b: string, kind: string, delay = 0): void {
    if (QUIET.has(kind) || !this.w) return;
    const hit = this.edge(a, b);
    if (!hit) return;
    if (this.pulses.length > 28) this.pulses.shift()?.dot.remove();
    const cls = KIND_CLASS[kind] ?? "k-soft";
    const dot = el("circle", { r: kind === "regime" || kind === "budget" ? 1.8 : 2.6, opacity: 0 }, `pdot ${cls}`);
    this.gPulse.append(dot);
    this.pulses.push({ edge: hit.e, reverse: hit.reverse, kind, t0: performance.now() + delay, dur: kind === "regime" || kind === "budget" ? 1800 : 1300, dot });
  }

  /** advance pulses; called from the app's single animation loop */
  frame(now: number): void {
    const glowLevel = new Map<string, { o: number; cls: string }>();
    this.pulses = this.pulses.filter((p) => {
      const k = (now - p.t0) / p.dur;
      if (k < 0) return true;
      const life = (now - p.t0) / (p.dur + 1400);
      if (life >= 1) {
        p.dot.remove();
        return false;
      }
      const cls = KIND_CLASS[p.kind] ?? "k-soft";
      const o = (1 - life) * (cls === "k-soft" ? 0.35 : 0.85);
      const prev = glowLevel.get(p.edge.key);
      if (!prev || prev.o < o) glowLevel.set(p.edge.key, { o, cls });
      if (k <= 1 && !this.reduce) {
        const e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
        const len = p.edge.path.getTotalLength();
        const pt = p.edge.path.getPointAtLength((p.reverse ? 1 - e : e) * len);
        p.dot.setAttribute("cx", pt.x.toFixed(1));
        p.dot.setAttribute("cy", pt.y.toFixed(1));
        p.dot.setAttribute("opacity", String(Math.min(1, 4 * Math.min(k, 1 - k) + 0.2)));
      } else p.dot.setAttribute("opacity", "0");
      return true;
    });
    for (const e of this.edges.values()) {
      const g = glowLevel.get(e.key);
      e.glow.setAttribute("class", `glow ${g ? g.cls : ""}`);
      e.glow.style.opacity = g ? g.o.toFixed(3) : "0";
    }
  }

  /** standing state from the folded events: feed health, the latest decision path, kills, node captions */
  update(st: DashState, simNow: number): void {
    this.last = { st, simNow };
    if (!this.w) return;
    const tony = st.tony;
    const killed = st.anyKill();
    const brokerUp = tony?.system?.broker_connected !== false;
    const feedFresh = st.tickTs ? simNow - Date.parse(st.tickTs) < 60_000 : false;
    // steady data feed: a quiet state, not motion
    for (const e of this.edges.values()) {
      const pair = [e.a, e.b];
      const feed = (pair.includes("broker") && pair.includes("market_intel")) || (pair.includes("market_intel") && pair.includes("data_quality")) || (pair.includes("data_quality") && pair.some((x) => x.startsWith("strat:")));
      e.path.classList.toggle("feed", feed && feedFresh && e.structural);
      e.path.classList.toggle("down", feed && !brokerUp && pair.includes("broker"));
      e.path.classList.toggle("blocked", killed && pair.includes("risk_governor") && pair.includes("execution"));
    }
    // the latest decision, held for two simulated minutes so a paused frame still explains itself
    this.gOverlay.replaceChildren();
    const last = st.decisions[st.decisions.length - 1];
    if (last && simNow - Date.parse(last.ts) < 120_000) {
      const s = `strat:${last.strategy}`;
      const ok = last.verdict.startsWith("APPROVE");
      const hops: [string, string][] = ok ? [[s, "allocator"], ["allocator", "risk_governor"], ["risk_governor", "execution"]] : [[s, "allocator"], ["allocator", "risk_governor"]];
      for (const [a, b] of hops) {
        const hit = this.edge(a, b);
        if (hit) this.gOverlay.append(el("path", { d: hit.e.path.getAttribute("d") ?? "" }, `trail ${ok ? "ok" : "rej"}`));
      }
      if (!ok) {
        // the path ends at the Governor: a short red cap across the incoming edge, just before the node
        const A = this.P(this.nodes.get("allocator")!.at);
        const B = this.P(this.nodes.get("risk_governor")!.at);
        const dx = B.x - A.x;
        const dy = B.y - A.y;
        const L = Math.hypot(dx, dy) || 1;
        const ux = dx / L;
        const uy = dy / L;
        const cx = B.x - ux * 14;
        const cy = B.y - uy * 14;
        this.gOverlay.append(el("line", { x1: cx - uy * 7, y1: cy + ux * 7, x2: cx + uy * 7, y2: cy - ux * 7 }, "cap"));
      }
      const fill = st.fills[st.fills.length - 1];
      if (ok && fill && simNow - Date.parse(fill.ts) < 120_000) {
        const hit = this.edge("execution", "broker");
        if (hit) this.gOverlay.append(el("path", { d: hit.e.path.getAttribute("d") ?? "" }, "trail fill"));
      }
    }
    // node states and captions (all from real fields)
    const decs = st.decisions;
    const approved = decs.filter((d) => d.verdict.startsWith("APPROVE")).length;
    const rejected = decs.length - approved;
    const recentRej = last && !last.verdict.startsWith("APPROVE") && simNow - Date.parse(last.ts) < 120_000;
    const tags: string[] = st.regime?.tags ?? [];
    const words = tags.map((t) => REGIME_WORD[t] ?? t.toLowerCase().replaceAll("_", " "));
    const econ = st.economics?.today;
    const caps: Record<string, string> = {
      cio: tony?.ai_status ?? "starting…",
      market_intel: words.length ? words.join(" · ") : "waiting for ticks",
      data_quality: feedFresh ? `clean · last tick ${istHM(st.tickTs)}` : st.tickTs ? `no tick since ${istHM(st.tickTs)}` : "waiting for the feed",
      strategy_factory: `${this.topo.strategies.filter((s) => !receivesFeed(this.stages[s.node] ?? s.stage)).length} in research`,
      validation: "lifecycle evidence",
      allocator: st.risk ? `1 lot max · ₹${num(st.risk.per_trade_budget, 0)} risk` : "1 lot max",
      risk_governor: killed ? "kill latched · entries blocked" : recentRej ? `rejected ${last.strategy} · ${istHM(last.ts)}` : `${approved} approved · ${rejected} rejected`,
      execution: st.risk ? `${st.risk.entries_today} entr${st.risk.entries_today === 1 ? "y" : "ies"} today` : "idle",
      broker: brokerUp ? "fake · connected" : "fake · link down",
      post_trade: econ ? `${econ.round_trips} round trip${econ.round_trips === 1 ? "" : "s"} · net ${inrSigned(econ.net, 0)}` : "no fills yet",
    };
    for (const s of this.topo.strategies) {
      const stage = this.stages[s.node] ?? s.stage;
      const d = st.lastDecision[s.id];
      const sw = (st.kills?.switches ?? []).find((x: any) => x.id === "STRATEGY_KILL" && x.latched && (x.scope ?? []).includes(s.id));
      caps[s.node] = sw ? `${stage} · kill-switched` : d ? `${stage} · ${d.verdict.startsWith("APPROVE") ? "approved" : "rejected"} ${istHM(d.ts)}` : stage;
    }
    const level = tony?.attention?.level ?? "NORMAL";
    for (const [id, ne] of this.nodeEls) {
      const txt = this.fit(id, caps[id] ?? "");
      if (ne.sub.textContent !== txt) ne.sub.textContent = txt;
      const stage = this.stages[id];
      const cls: string[] = ["node", `n-${id.replace(":", "-")}`];
      if (id.startsWith("strat:")) {
        cls.push("strat", receivesFeed(stage ?? "") ? "active" : "idle");
        if (stage === "CANARY" || stage === "PRODUCTION") cls.push("capital");
        if (killed) cls.push("dim");
      }
      if (id === "risk_governor" && (killed || recentRej)) cls.push(killed ? "alarm" : "warn");
      if (id === "broker" && !brokerUp) cls.push("warn");
      if (id === "data_quality" && !feedFresh && st.tickTs) cls.push("warn");
      if (id === "cio") cls.push(level === "INTERVENTION" ? "alarm" : level === "ATTENTION" ? "warn" : "calm", feedFresh && !killed ? "alive" : "still");
      const c = cls.join(" ");
      if (ne.g.getAttribute("class") !== c) ne.g.setAttribute("class", c);
    }
  }
}
