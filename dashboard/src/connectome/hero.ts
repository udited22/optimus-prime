// The centre hero (docs/architecture/observability.md §17.5 v3.2): Tony's cognitive connectome. Wires the stream to the renderer through
// the pure mapping (mapping.ts), keeps the hero clock (frozen for screenshots), derives the cognitive state
// (WATCHING / EVALUATING / REJECTED / EXECUTING) from Tony's real cognition and the chain step playing, and draws
// annotations that emerge next to the assembly that is active (with a hairline leader), the current thought line,
// hover and click-to-focus. Falls back to the v3 SVG decision graph when WebGL 2 is unavailable.
import { DecisionGraph } from "../graph";
import type { DashState } from "../state";
import type { DashEvent, Topology } from "../types";
import { esc } from "../ui/dom";
import { type Anatomy, type V3, buildAnatomy, pathwayIndex } from "./geometry";
import {
  type Command, Choreographer, type CognitiveState, type FieldState, type Phase, type Scheduled, cognitionLine, cognitiveState,
  commandsFor, fieldState, transientCognition,
} from "./mapping";
import { ConnectomeRenderer, type Standing } from "./renderer";
import { COGNITIVE, REGIONS, REGION_INFO, type RegionId, type Tone, strategyAssembly } from "./regions";

export interface Hero {
  readonly kind: "connectome" | "svg";
  event(e: DashEvent, live: boolean, quiet?: boolean): void;
  sync(st: DashState, simNow: number): void;
  frame(now: number): void;
  setStages(stages: Record<string, string>): void;
  reset(): void;
  onRegionClick?: (r: RegionId) => void;
  focus(r: RegionId | null): void;
}

const LABEL_MS = 6500;
const TONE_CLASS: Record<Tone, string> = { cyan: "t-cyan", violet: "t-violet", amber: "t-amber", green: "t-green", red: "t-red", white: "t-white" };
const CODE = { normal: 0, degraded: 1, alarm: 2, quiet: 0 } as const;

interface LiveLabel { title: string; text: string; detail?: string; tone: Tone; until: number; assembly?: string }

export class ConnectomeHero implements Hero {
  readonly kind = "connectome" as const;
  private r: ConnectomeRenderer;
  private anat: Anatomy;
  private asmIndex = new Map<string, number>();
  private choreo = new Choreographer();
  private queue: Scheduled[] = [];
  private labels = new Map<RegionId, LiveLabel>();
  private labelEls = new Map<RegionId, HTMLElement>();
  private labelCache = new Map<RegionId, { html: string; cls: string; w: number; h: number; tf: string }>();
  private leaderSvg = "";
  private thoughtHtml = "";
  private layer: HTMLElement;
  private leaders: SVGSVGElement;
  private thought: HTMLElement;
  private latched = new Set<string>();
  private transient: { verb: string; subject: string } | null = null;
  private thoughts: { at: number; tc: { verb: string; subject: string } }[] = [];
  private phase: Phase | null = null;
  private cog: CognitiveState = "WATCHING";
  private fs: FieldState | null = null;
  private st: DashState | null = null;
  private simNow = 0;
  private standingSet = false;
  private heldTrail = false; // the standing trail waits until the live chain that caused it has finished playing
  private hover: { region: RegionId; asm: number | null } | null = null;
  private focused: RegionId | null = null;
  private hidden = document.hidden;
  private ro: ResizeObserver;
  /** the hero clock (ms): advances with real time unless frozen (identical frames with labels on and off) */
  private clock = 0;
  private lastReal = 0;
  private frozen = false;
  onRegionClick?: (r: RegionId) => void;

  constructor(private box: HTMLElement, strategyIds: string[], private occluder: HTMLElement | null, labelsOn = true) {
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    this.anat = buildAnatomy(strategyIds);
    this.anat.assemblies.forEach((a, i) => this.asmIndex.set(a.id, i));
    box.classList.add("nf");
    box.dataset.state = this.cog;
    this.r = new ConnectomeRenderer(box, this.anat, reduced);
    this.layer = document.createElement("div");
    this.layer.className = "nf-labels";
    this.leaders = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    this.leaders.setAttribute("class", "nf-leaders");
    this.layer.append(this.leaders);
    this.thought = document.createElement("div");
    this.thought.className = "nf-thought";
    this.thought.setAttribute("aria-live", "polite");
    box.append(this.layer, this.thought);
    for (const id of REGIONS) {
      const el = document.createElement("div");
      el.className = "nf-label";
      el.dataset.region = id;
      this.layer.append(el);
      this.labelEls.set(id, el);
    }
    this.setLabels(labelsOn);
    this.ro = new ResizeObserver(() => this.r.resize());
    this.ro.observe(box);
    document.addEventListener("visibilitychange", () => (this.hidden = document.hidden));
    const c = this.r.canvas;
    c.addEventListener("pointermove", (e) => this.onMove(e));
    c.addEventListener("pointerleave", () => this.setHover(null));
    c.addEventListener("click", () => this.hover && this.onRegionClick?.(this.hover.region));
  }

  /** ?labels=off: the visual alone (every label, the thought line, the hero eyebrow and legend hidden) */
  setLabels(on: boolean): void {
    this.box.closest(".hero")?.classList.toggle("labels-off", !on);
    this.box.classList.toggle("labels-off", !on);
  }
  /** stop / restart the hero clock (screenshots: the same frame with labels off and on) */
  freeze(on: boolean): void {
    this.frozen = on;
  }
  get cognitive(): CognitiveState {
    return this.cog;
  }

  // ---------------------------------------------------------------- stream
  event(e: DashEvent, live: boolean, quiet = false): void {
    if (live && !quiet) {
      const cmds = commandsFor(e, { latchedBefore: this.latched });
      const sched = this.choreo.schedule(e, cmds, this.clock);
      this.queue.push(...sched);
      // the thought (and the cognitive state) change when this event's step actually plays, not when it arrives
      const tc = transientCognition(e);
      if (tc) this.thoughts.push({ at: sched.length ? Math.min(...sched.map((q) => q.at)) : this.clock, tc });
    }
    if (e.kind === "KILLS") this.latched = new Set(((e.data?.switches ?? []) as any[]).filter((s) => s.latched).map((s) => s.id));
  }

  reset(): void {
    this.queue = [];
    this.labels.clear();
    this.choreo.reset();
    this.transient = null;
    this.thoughts = [];
    this.phase = null;
    this.latched = new Set();
    this.r.clearTransient();
  }

  setStages(): void {
    /* the strategy assemblies take their stage from fieldState on every sync */
  }

  focus(r: RegionId | null): void {
    this.focused = r;
    this.r.setFocus(r);
    this.box.classList.toggle("focused", !!r);
  }

  sync(st: DashState, simNow: number): void {
    this.st = st;
    this.simNow = simNow;
    if (!this.latched.size) for (const s of st.latched()) this.latched.add(s.id);
    const fs = fieldState(st, simNow);
    // the standing state waits until the live chain that caused it has finished playing (the kernel decides intent →
    // fill in one instant; without this an open position would glow while Risk is still shown evaluating)
    const playing = this.clock < this.choreo.busyUntil;
    this.heldTrail = playing && (!!fs.trail || this.standingSet);
    if (playing) fs.trail = null;
    if (playing && this.standingSet) {
      this.fs = { ...fs, assemblies: this.fs?.assemblies ?? fs.assemblies };
      this.renderThought();
      if (this.occluder) this.r.setOcclusion(this.occluder.offsetHeight + 24);
      return;
    }
    this.fs = fs;
    this.standingSet = true;
    const patterns: Standing["patterns"] = new Map();
    for (const p of fs.strategies) {
      const i = this.asmIndex.get(strategyAssembly(p.id));
      if (i !== undefined) patterns.set(i, { formation: p.formation, base: p.base, activity: p.activity * 0.5, tone: p.tone });
    }
    const asmSustain: Standing["asmSustain"] = new Map();
    for (const [id, v] of Object.entries(fs.assemblies)) {
      const i = this.asmIndex.get(id);
      if (i !== undefined) asmSustain.set(i, v);
    }
    // the last decision's trail (held TRAIL_S simulated seconds, fading); it is a faint memory, not a heatmap
    const trail: Standing["trail"] = [];
    if (fs.trail) {
      const amp = 0.8 * (1 - fs.trail.age) + 0.1;
      const rs = fs.trail.regions;
      for (let i = 0; i + 1 < rs.length; i++) {
        const pw = pathwayIndex(rs[i], rs[i + 1]);
        if (pw) trail.push({ path: pw.index, amp, tone: fs.trail.approved ? (i >= 2 ? "green" : "violet") : i === rs.length - 2 ? "red" : "violet" });
      }
    }
    const reg = fs.regions;
    const rejectCap = fs.trail && !fs.trail.approved ? 0.12 * (1 - fs.trail.age) : 0;
    this.r.setStanding({
      regionCode: REGIONS.map((id) => CODE[reg[id].state]),
      regionQuiet: REGIONS.map((id) => reg[id].state === "quiet"),
      regionSustain: REGIONS.map((id) => (id === "RISK" && rejectCap > reg[id].sustain ? rejectCap : reg[id].sustain)),
      regionSustainTone: REGIONS.map((id) => (id === "RISK" && rejectCap > reg[id].sustain ? "red" : reg[id].tone)),
      asmSustain,
      patterns,
      trail,
    });
    this.renderThought();
    if (this.occluder) this.r.setOcclusion(this.occluder.offsetHeight + 24);
  }

  /** "TONY · VERB · subject": the backend's cognition, or the step of a real chain that is playing right now */
  private renderThought(): void {
    const now = this.clock;
    while (this.thoughts.length && this.thoughts[0].at <= now) {
      this.transient = this.thoughts.shift()!.tc;
      const ph = this.transient.verb as Phase["state"];
      if (ph === "EVALUATING" || ph === "REJECTED" || ph === "EXECUTING") this.phase = { state: ph, at: now / 1000 };
    }
    const st = this.st;
    const cog = cognitionLine(st?.tony?.cognition, now < this.choreo.busyUntil + 2500 ? this.transient : null);
    const html = `<span class="nf-who">TONY</span><span class="nf-verb v-${cog.verb.toLowerCase()}">${esc(cog.verb)}</span><span class="nf-subj">${esc(cog.subject)}</span>`;
    if (this.thoughtHtml !== html) (this.thought.innerHTML = html), (this.thoughtHtml = html);
    const lvl = st?.tony?.attention?.level ?? "NORMAL";
    if (this.thought.dataset.level !== lvl) this.thought.dataset.level = lvl;
  }

  // ---------------------------------------------------------------- frame
  frame(): void {
    if (this.hidden) return; // nothing is drawn while the tab is hidden
    const real = performance.now();
    if (!this.frozen) this.clock += Math.min(250, this.lastReal ? real - this.lastReal : 16);
    this.lastReal = real;
    const now = this.clock;
    if (this.queue.length) {
      const due = this.queue.filter((q) => q.at <= now);
      if (due.length) {
        this.queue = this.queue.filter((q) => q.at > now);
        for (const q of due) this.apply(q.cmd, now);
      }
    }
    if (this.heldTrail && now >= this.choreo.busyUntil && this.st) this.sync(this.st, this.simNow);
    if (this.thoughts.length || now < this.choreo.busyUntil + 2600) this.renderThought();
    const cs = cognitiveState(this.st?.tony?.cognition?.verb, this.phase, now / 1000);
    if (cs !== this.cog) {
      this.cog = cs;
      this.box.dataset.state = cs;
    }
    this.r.setCognitive(cs);
    this.r.frame(now / 1000);
    this.placeLabels(now);
  }

  private asm(id: string | undefined): number {
    return id ? (this.asmIndex.get(id) ?? -1) : -1;
  }

  private apply(c: Command, now: number): void {
    const s = now / 1000;
    switch (c.type) {
      case "fire":
        if (c.assembly && this.asm(c.assembly) >= 0) {
          this.r.fireAssembly(this.asm(c.assembly), c.tone, c.strength, s);
          this.r.fireRegion(c.region, c.tone, c.strength * 0.3, s); // the firing spills into the region's tissue
        } else this.r.fireRegion(c.region, c.tone, c.strength, s);
        break;
      case "terminate":
        this.r.fireRegion("RISK", "red", 0.45, s);
        if (c.assembly) this.r.fireAssembly(this.asm(c.assembly), "red", 1.3, s);
        if (c.strategy) this.r.fireAssembly(this.asm(strategyAssembly(c.strategy)), "amber", 0.4, s);
        break;
      case "strategy":
        this.r.fireAssembly(this.asm(strategyAssembly(c.id)), c.tone, c.strength, s);
        break;
      case "path": {
        const pw = pathwayIndex(c.from, c.to);
        if (!pw) break;
        // a strategy's impulse leaves from (or reaches) its own assembly's filaments
        const filter = c.strategy && (c.from === "STRATEGY" || c.to === "STRATEGY") ? this.asm(strategyAssembly(c.strategy)) : -1;
        this.r.impulse(pw.index, pw.reverse, c.tone, c.strength, filter, s);
        break;
      }
      case "label":
        this.labels.set(c.region, { title: c.title, text: c.text, detail: c.detail, tone: c.tone, until: now + LABEL_MS, assembly: c.assembly });
        break;
    }
  }

  // ---------------------------------------------------------------- labels (annotations emerging from the tissue)
  private placeLabels(now: number): void {
    const fs = this.fs;
    const W = this.box.clientWidth;
    const H = this.box.clientHeight - (this.occluder ? this.occluder.offsetHeight + 24 : 0);
    const core = this.r.project(this.anat.centres.CORE);
    const placed: { x: number; y: number; w: number; h: number }[] = [];
    const lines: string[] = [];
    const order: RegionId[] = ["RISK", "EXECUTION", "STRATEGY", "PERCEPTION", "PORTFOLIO", "RESEARCH", "CORE", "BROKER"];
    const busy = (id: RegionId) => this.hover?.region === id || this.focused === id || (this.labels.get(id)?.until ?? 0) > now || (fs?.regions[id].state ?? "normal") === "alarm" || (fs?.regions[id].state ?? "normal") === "degraded" || fs?.trail?.terminal === id || (fs?.trail?.approved && id === "EXECUTION");
    const ordered = [...order.filter(busy), ...order.filter((id) => !busy(id))];
    for (const id of ordered) {
      const el = this.labelEls.get(id)!;
      const live = this.labels.get(id);
      const st = fs?.regions[id];
      let title = "";
      let text = "";
      let detail = "";
      let tone: Tone = "white";
      let kind = "";
      let anchor: V3 = this.anat.centres[id];
      if (this.hover?.region === id || this.focused === id) {
        const info = REGION_INFO[id];
        const sub = this.hover?.region === id && this.hover.asm !== null ? this.asmText(this.hover.asm) : null;
        title = sub ? sub.title : info.title;
        text = sub ? sub.text : info.role;
        detail = sub ? sub.detail : id === "CORE" || id === "BROKER" ? "" : `click: ${info.layer.toLowerCase()}`;
        if (sub && this.hover?.asm !== null && this.hover?.asm !== undefined) anchor = this.anat.asmCentres[this.hover.asm];
        kind = "hover";
      } else if (live && live.until > now) {
        ({ title, text, tone } = live);
        detail = live.detail ?? "";
        if (live.assembly && this.asm(live.assembly) >= 0) anchor = this.anat.asmCentres[this.asm(live.assembly)];
        kind = "live";
      } else if (st && (st.state === "alarm" || st.state === "degraded") && st.note) {
        title = REGION_INFO[id].title;
        text = st.note;
        tone = st.state === "alarm" ? "red" : "amber";
        kind = "state";
      } else if (fs?.trail && id === fs.trail.terminal && fs.trail.age < 0.85) {
        if (id === "BROKER") {
          this.hideLabel(el); // an approved trail ends at the broker port; it is annotated at EXECUTION
          continue;
        }
        title = fs.trail.approved ? "Last decision · approved" : "Last decision · rejected at Risk";
        text = fs.trail.text;
        tone = fs.trail.approved ? "green" : "red";
        kind = "trail";
      }
      if (fs?.trail?.approved && id === "EXECUTION" && !title) {
        title = "Last decision · approved";
        text = fs.trail.text;
        tone = "green";
        kind = "trail";
      }
      if (id === "BROKER" && kind !== "hover") {
        this.hideLabel(el);
        continue;
      }
      if (!title && !this.focused && (id === "CORE" || (COGNITIVE as string[]).includes(id))) {
        // at rest each region keeps only a whisper of its name, so the composition stays readable
        title = id === "CORE" ? "Tony · CIO" : REGION_INFO[id].title;
        kind = "anchor";
      }
      if (!title) {
        this.hideLabel(el);
        continue;
      }
      // the DOM is touched only when something changed (no per-frame layout reads or style invalidation)
      const html = `<span class="nf-l-t">${esc(title)}</span>${text ? `<span class="nf-l-x">${esc(text)}</span>` : ""}${detail ? `<span class="nf-l-d">${esc(detail)}</span>` : ""}`;
      const cache = this.labelCache.get(id) ?? { html: "", cls: "", w: 220, h: 40, tf: "" };
      if (cache.html !== html) {
        el.innerHTML = html;
        cache.html = html;
        cache.w = 0;
      }
      const cls = `nf-label on ${TONE_CLASS[tone]} k-${kind}`;
      if (cache.cls !== cls) (el.className = cls), (cache.cls = cls), (cache.w = 0);
      if (!cache.w) (cache.w = el.offsetWidth || 220), (cache.h = el.offsetHeight || 40);
      this.labelCache.set(id, cache);
      const p = this.r.project(anchor);
      const reg = this.r.project(this.anat.centres[id]);
      const lw = cache.w;
      const lh = cache.h;
      const isAnchor = kind === "anchor";
      // annotations sit just off the active assembly, away from Tony's core; idle names sit inside the region
      const off = isAnchor ? 0 : Math.max(26, reg.r * 0.55);
      let right = id === "CORE" ? true : p.x - core.x >= 0;
      if (right && p.x + off + lw > W - 8) right = false;
      else if (!right && p.x - off - lw < 8) right = true;
      let x = isAnchor ? reg.x - lw / 2 : right ? p.x + off : p.x - off - lw;
      let y = isAnchor ? reg.y + reg.r * (id === "CORE" ? 0.55 : 0.5) : p.y - lh * 0.8 - (p.y > core.y ? -12 : 12);
      x = Math.max(8, Math.min(W - lw - 8, x));
      y = Math.max(46, Math.min(H - lh - 4, y));
      const overlaps = (b: { x: number; y: number; w: number; h: number }) => x < b.x + b.w && x + lw > b.x && y < b.y + b.h + 6 && y + lh + 6 > b.y;
      if (isAnchor && placed.some(overlaps)) {
        this.hideLabel(el); // a resting name never pushes or covers a real annotation
        continue;
      }
      for (let guard = 0; guard < 6; guard++) {
        const hit = placed.find(overlaps);
        if (!hit) break;
        y = hit.y + hit.h + 8 > H - lh ? hit.y - lh - 8 : hit.y + hit.h + 8;
      }
      placed.push({ x, y, w: lw, h: lh });
      const tf = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
      if (cache.tf !== tf) (el.style.transform = tf), (cache.tf = tf);
      if (!isAnchor) {
        // a hairline leader from the annotation to the assembly it describes
        const lx = right ? x : x + lw;
        const ly = y + Math.min(lh - 2, 9);
        lines.push(`<line class="${TONE_CLASS[tone]}" x1="${Math.round(lx)}" y1="${Math.round(ly)}" x2="${Math.round(p.x)}" y2="${Math.round(p.y)}"/><circle class="${TONE_CLASS[tone]}" cx="${Math.round(p.x)}" cy="${Math.round(p.y)}" r="2"/>`);
      }
    }
    const svg = lines.join("");
    if (this.leaderSvg !== svg) (this.leaders.innerHTML = svg), (this.leaderSvg = svg);
  }

  private hideLabel(el: HTMLElement): void {
    if (el.classList.contains("on")) {
      el.classList.remove("on");
      const c = this.labelCache.get(el.dataset.region as RegionId);
      if (c) c.cls = el.className;
    }
  }

  private asmText(i: number): { title: string; text: string; detail: string } {
    const a = this.anat.assemblies[i];
    if (!a.id.startsWith("S:")) return { title: `${REGION_INFO[a.region].title} · ${a.title}`, text: REGION_INFO[a.region].role, detail: "lights only when a real event reaches it" };
    const p = this.fs?.strategies.find((s) => s.id === a.title);
    const row = (this.st?.tony?.strategies ?? []).find((s: any) => s.id === a.title);
    return { title: `${a.title} · ${p?.stage ?? "unknown stage"}`, text: `${p ? `${p.state} pattern` : "no pattern"} · ${row?.status ?? "no status"}${p?.killed ? " · kill-switched" : ""}`, detail: "brightness = activity, not expected profit" };
  }

  // ---------------------------------------------------------------- pointer
  private onMove(e: PointerEvent): void {
    const b = this.box.getBoundingClientRect();
    const x = e.clientX - b.left;
    const y = e.clientY - b.top;
    this.r.setPointer((x / b.width) * 2 - 1, -((y / b.height) * 2 - 1));
    // assemblies first (small), then the regions
    let best: { region: RegionId; asm: number | null; d: number } | null = null;
    this.anat.asmCentres.forEach((c, i) => {
      const p = this.r.project(c);
      const d = Math.hypot(x - p.x, y - p.y) / Math.max(9, p.r * 0.22);
      if (d < 1 && (!best || d < best.d)) best = { region: this.anat.assemblies[i].region, asm: i, d };
    });
    if (!best)
      for (const id of [...COGNITIVE, "CORE", "BROKER"] as RegionId[]) {
        const p = this.r.project(this.anat.centres[id]);
        const rad = Math.max(24, p.r * (id === "CORE" ? 0.8 : id === "BROKER" ? 0.6 : 1.5));
        const d = Math.hypot(x - p.x, (y - p.y) * 1.25) / rad;
        if (d < 1 && (!best || d < (best as { d: number }).d)) best = { region: id, asm: null, d };
      }
    const bb = best as { region: RegionId; asm: number | null } | null;
    this.setHover(bb ? { region: bb.region, asm: bb.asm } : null);
  }

  private setHover(h: { region: RegionId; asm: number | null } | null): void {
    this.hover = h;
    this.r.setHover(h?.region ?? null, h?.asm ?? null);
    this.r.canvas.style.cursor = h && h.region !== "BROKER" ? "pointer" : "default";
  }

  /** for the screenshot script and the perf note: frames drawn and mean render time */
  get stats(): { frames: number; meanRenderMs: number; points: number; segments: number } {
    const s = this.r.stats;
    return { frames: s.frames, meanRenderMs: s.frames ? s.renderMs / s.frames : 0, points: this.anat.points.count, segments: this.anat.lines.count / 2 };
  }

  /** deep-link hover (?hover=RISK or ?hover=STRATEGY:2) so a hover label can be captured without a pointer */
  hoverRegion(id: RegionId, sub: number | null = null): void {
    const asm = sub === null ? null : this.anat.assemblies.map((a, i) => ({ a, i })).filter((x) => x.a.region === id)[sub]?.i ?? null;
    this.setHover({ region: id, asm });
  }
}

/** the v3 SVG decision graph behind the same interface, for browsers without WebGL (or ?hero=svg) */
export class SvgHero implements Hero {
  readonly kind = "svg" as const;
  private g: DecisionGraph;
  onRegionClick?: (r: RegionId) => void;
  constructor(box: HTMLElement, topo: Topology) {
    box.classList.add("svg-hero");
    this.g = new DecisionGraph(box, topo);
  }
  event(e: DashEvent, live: boolean): void {
    if (live) e.flows.forEach(([a, b, k], i) => this.g.pulse(a, b, k, i * 380));
  }
  sync(st: DashState, simNow: number): void {
    this.g.update(st, simNow);
  }
  frame(now: number): void {
    this.g.frame(now);
  }
  setStages(stages: Record<string, string>): void {
    this.g.setStages(stages);
  }
  reset(): void {}
  focus(): void {}
}

export function createHero(box: HTMLElement, topo: Topology, strategyIds: string[], occluder: HTMLElement | null, force?: string | null, labelsOn = true): Hero {
  if (force !== "svg" && ConnectomeRenderer.supported()) {
    try {
      return new ConnectomeHero(box, strategyIds, occluder, labelsOn);
    } catch (err) {
      console.warn("connectome unavailable, using the SVG graph", err);
      box.querySelector("canvas")?.remove();
    }
  }
  return new SvgHero(box, topo);
}
