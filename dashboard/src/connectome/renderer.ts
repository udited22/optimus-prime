// WebGL renderer for the cognitive connectome (three.js). It owns no meaning: every activation is written from
// outside by hero.ts, which takes it from the pure event mapping (mapping.ts), into one small float state texture.
// The vertex shaders read that texture (texelFetch) and compute firing, the spreading wave inside an assembly,
// impulses travelling along axonal filaments, synaptic afterglow and decay per vertex on the GPU; the CPU only
// writes a texel when a real event (or the standing state) changes. Resting motion is a very slow breathing, a
// rare faint tissue flicker and a slow parallax drift; all of it stops under prefers-reduced-motion. Rendering
// pauses while the tab is hidden (hero.ts).
import {
  AdditiveBlending, BufferAttribute, BufferGeometry, Color, DataTexture, FloatType, LineSegments, NearestFilter,
  PerspectiveCamera, Points, RGBAFormat, Scene, ShaderMaterial, Vector3, WebGLRenderer,
} from "three";
import { type Anatomy, KIND, PATHWAYS, TISSUE, type V3 } from "./geometry";
import { FAST_TAU_S, GLOW_TAU_S, GLOW_WEIGHT, STATE_INDEX, type CognitiveState } from "./mapping";
import { MAX_ASSEMBLIES, REGIONS, REGION_INDEX, type RegionId, TONE_RGB, type Tone } from "./regions";

const TONES: Tone[] = ["cyan", "violet", "amber", "green", "red", "white"];
const TI = (t: Tone) => Math.max(0, TONES.indexOf(t));
const W = 64;
/** state texture rows */
const ROW = { asmFire: 0, asmTone: 1, regFire: 2, regTone: 3, regStatus: 4, slot: 5, slotTone: 6, trail: 7, pattern: 8 } as const;
const ROWS = 9;
const SLOTS = 4; // concurrent impulses per pathway
const NP = PATHWAYS.length;
const FAR = -1e4;

const COMMON = /* glsl */ `
precision highp sampler2D;
uniform sampler2D uState; uniform float uNow, uAmb, uFocus, uHover, uHoverAsm, uState2, uArousal, uDist;
uniform vec3 uPal[6]; uniform vec3 uCoreTone;
vec4 S(int x, int row) { return texelFetch(uState, ivec2(x, row), 0); }
vec3 pal(float i) { return uPal[int(clamp(i, 0.0, 5.0) + 0.5)]; }
// a firing: fast response plus synaptic afterglow (mapping.afterglow), arriving after the cell's spread delay
float fired(float amp, float t0, float delay) {
  float d = uNow - t0 - delay;
  if (d < 0.0 || amp <= 0.0) return 0.0;
  return amp * min(1.0, d / 0.05) * (exp(-d / ${FAST_TAU_S.toFixed(3)}) + ${GLOW_WEIGHT.toFixed(3)} * exp(-d / ${GLOW_TAU_S.toFixed(3)}));
}
float memory(float amp, float t0) { float d = uNow - t0; return d < 0.0 ? 0.0 : amp * exp(-d / ${GLOW_TAU_S.toFixed(3)}); }
float hash(float n) { return fract(sin(n) * 43758.5453); }
const vec3 BASE = vec3(0.62, 0.71, 0.84);
// returns colour (premultiplied by brightness) in rgb and brightness in a; act = transient activation
vec4 shade(inout vec3 pos, vec4 A, vec4 B, vec4 C, bool isLine, out float act) {
  int r = int(A.x + 0.5); int a = int(A.y + 0.5); bool hasA = A.y > -0.5; bool fibre = A.z > -0.5;
  float seed = B.x, rest = B.y, delay = B.z, w = B.w;
  float b = rest * (1.0 + uAmb * 0.09 * sin(uNow * 0.31 + seed * 6.2831));
  vec3 col = BASE * b;
  act = 0.0;
  if (!isLine && C.w < 1.5 && uAmb > 0.5) b += 0.1 * step(0.9994, hash(floor(uNow * 0.5 + seed * 91.0) * 12.9898 + seed * 78.233)); // rare faint cell
  if (fibre) {
    int p = int(A.z + 0.5);
    for (int k = 0; k < ${SLOTS}; k++) {
      vec4 s = S(p * ${SLOTS} + k, ${ROW.slot});
      if (s.z <= 0.0) continue;
      vec4 s2 = S(p * ${SLOTS} + k, ${ROW.slotTone});
      float u = s.w > 0.0 ? A.w : 1.0 - A.w;
      float d = uNow - (s.x + (u + (seed - 0.5) * 0.1) * s.y);
      float front = d < 0.0 ? exp(d / 0.04) * step(-0.25, d) : exp(-d / 0.18);
      float tail = d > 0.0 ? ${GLOW_WEIGHT.toFixed(3)} * exp(-d / ${GLOW_TAU_S.toFixed(3)}) : 0.0;
      float m = (s2.y < -0.5 || abs(C.x - s2.y) < 0.5 || abs(C.y - s2.y) < 0.5) ? 1.0 : 0.2;
      float g = s.z * m * w * (front * 1.7 + tail * 1.2);
      act += g; col += pal(s2.x) * g;
    }
    vec4 tr = S(p, ${ROW.trail});
    float tg = tr.x * w * 0.12; b += tg; col += pal(tr.y) * tg;
    if (uFocus > -0.5 && tr.z < 0.5) { b *= 0.3; col *= 0.3; act *= 0.4; }
  } else {
    vec4 rf = S(r, ${ROW.regFire}); vec4 rt = S(r, ${ROW.regTone}); vec4 rs = S(r, ${ROW.regStatus});
    float g = w * fired(rf.x, rf.y, delay) * (0.55 + 0.6 * seed);
    act += g; col += pal(rt.x) * g;
    float m = w * memory(rf.z, rf.w) * 0.8; act += m; col += pal(rt.y) * m;
    float su = w * rt.z * (0.45 + 0.4 * seed); b += su; col += pal(rt.w) * su;
    if (hasA) {
      vec4 af = S(a, ${ROW.asmFire}); vec4 at = S(a, ${ROW.asmTone}); vec4 pt = S(a, ${ROW.pattern});
      float ga = fired(af.x, af.y, delay) * (0.5 + 0.9 * seed);
      act += ga; col += pal(at.x) * ga;
      float ma = memory(af.z, af.w) * (0.6 + 0.5 * seed); act += ma; col += pal(at.y) * ma;
      float sa = at.z * (0.5 + 0.5 * seed); b += sa; col += pal(at.w) * sa;
      if (pt.x < 0.999 || pt.y > 0.0) {
        // a strategy assembly: formation pulls its cells together; its lifecycle stage sets its resting level
        pos += C.xyz * (1.0 - pt.x);
        float pb = pt.y * (0.5 + 0.7 * seed) * (0.35 + 0.65 * pt.x);
        float sh = pt.z * (0.5 + 0.5 * sin(uNow * 2.6 + seed * 37.0)) * (uAmb > 0.5 ? 1.0 : 0.6);
        b = b * (0.35 + 0.65 * pt.x) + pb + sh * 0.5;
        col = col * (0.35 + 0.65 * pt.x) + pal(pt.w) * (pb + sh * 0.5);
      }
      if (uHoverAsm > -0.5 && abs(A.y - uHoverAsm) < 0.5) b += 0.18;
    }
    if (r == ${REGION_INDEX.CORE}) {
      // Tony's integration core: its arousal follows the cognitive state (WATCHING: a slow resting pulse)
      float period = uState2 < 0.5 ? 4.8 : uState2 < 1.5 ? 1.5 : 2.6;
      float pulse = uAmb > 0.5 ? 0.55 + 0.45 * sin(uNow * 6.2831 / period - delay * 2.0 + seed * (uState2 < 0.5 ? 0.4 : 2.2)) : 0.75;
      float ca = w * uArousal * pulse * (0.4 + 0.8 * seed);
      act += ca * 0.6; col += uCoreTone * ca;
    }
    int code = int(rs.x + 0.5);
    if (code == 2) { col = mix(col, vec3(0.95, 0.38, 0.42) * (b + act), 0.8 * w); b += w * (0.05 + 0.03 * uAmb * sin(uNow * 0.9 + seed * 3.0)); }
    else if (code == 1) { col = mix(col, vec3(0.94, 0.71, 0.30) * (b + act), 0.5 * w); b += 0.025 * w; }
    if (rs.y > 0.5) { b *= 0.45; col *= 0.45; act *= 0.6; }
    if (uHover > -0.5 && abs(A.x - uHover) < 0.5) { b += 0.05 * w; col += BASE * 0.05 * w; }
    if (uFocus > -0.5) { if (abs(A.x - uFocus) > 0.5) { b *= 0.28; col *= 0.28; act *= 0.4; } else { b = b * 1.5 + 0.02; col *= 1.5; } }
  }
  return vec4(col, b + act);
}`;

const VERT = /* glsl */ `
${COMMON}
uniform float uPR, uScale, uBase;
attribute vec4 aA; attribute vec4 aB; attribute vec4 aC;
varying vec3 vColor; varying float vAlpha; varying float vSoft; varying float vBloom;
void main() {
  vec3 pos = position; float act;
  vec4 s = shade(pos, aA, aB, aC, false, act);
  vec4 mv = modelViewMatrix * vec4(pos, 1.0);
  float depth = -mv.z;
  // 2.5D: the far side recedes (opacity) and defocuses (larger, softer, fainter sprites); the near side is crisp
  float blur = clamp((depth - uDist - 0.2) / 0.85, 0.0, 1.0) + clamp((uDist - 0.55 - depth) / 0.8, 0.0, 0.6);
  float df = clamp((uDist + 1.6 - depth) / 1.6, 0.3, 1.0);
  int k = int(aC.w + 0.5);
  float ks = k == ${KIND.tissue} ? 0.78 : k == ${KIND.deep} ? 1.15 : k == ${KIND.neuron} ? 0.72 : k == ${KIND.dendrite} ? 0.48 : k == ${KIND.bead} ? 0.5 : 0.66;
  float a = clamp(act, 0.0, 1.4);
  gl_PointSize = min(uPR * 26.0, uPR * uScale * ks * (0.6 + 0.8 * aB.x) * (1.0 + a * 0.75) * (1.0 + 0.9 * blur * blur) * (uBase / depth));
  float bright = s.a;
  vAlpha = clamp(bright, 0.0, 1.6) * df / (1.0 + (k == ${KIND.deep} ? 0.8 : 2.2) * blur * blur);
  vColor = bright > 0.0001 ? s.rgb / bright : BASE;
  vSoft = blur; vBloom = clamp(a * 0.9, 0.0, 1.0);
  gl_Position = projectionMatrix * mv;
}`;
const FRAG = /* glsl */ `
varying vec3 vColor; varying float vAlpha; varying float vSoft; varying float vBloom;
void main() {
  vec2 c = gl_PointCoord * 2.0 - 1.0;
  float d2 = dot(c, c);
  if (d2 > 1.0) discard;
  float core = exp(-d2 * mix(4.6, 2.0, vSoft));
  float halo = vBloom * 0.16 * exp(-d2 * 2.0); // very restrained bloom, only on active cells
  gl_FragColor = vec4(vColor, (core + halo) * vAlpha);
}`;
const LINE_VERT = /* glsl */ `
${COMMON}
attribute vec4 aA; attribute vec4 aB; attribute vec4 aC;
varying vec3 vColor; varying float vAlpha;
void main() {
  vec3 pos = position; float act;
  vec4 s = shade(pos, aA, aB, aC, true, act);
  vec4 mv = modelViewMatrix * vec4(pos, 1.0);
  float depth = -mv.z;
  float df = clamp((uDist + 1.6 - depth) / 1.6, 0.22, 1.0);
  float bright = s.a;
  vAlpha = clamp(bright * 0.85, 0.0, 1.0) * df;
  vColor = bright > 0.0001 ? s.rgb / bright : BASE;
  gl_Position = projectionMatrix * mv;
}`;
const LINE_FRAG = /* glsl */ `
varying vec3 vColor; varying float vAlpha;
void main() { gl_FragColor = vec4(vColor, vAlpha); }`;

export interface Projected { x: number; y: number; r: number; depth: number }
export interface Standing {
  regionCode: number[]; regionQuiet: boolean[]; regionSustain: number[]; regionSustainTone: Tone[];
  asmSustain: Map<number, { sustain: number; tone: Tone }>;
  patterns: Map<number, { formation: number; base: number; activity: number; tone: Tone }>;
  trail: { path: number; amp: number; tone: Tone }[];
}

export class ConnectomeRenderer {
  readonly canvas: HTMLCanvasElement;
  private gl: WebGLRenderer;
  private scene = new Scene();
  private camera = new PerspectiveCamera(34, 1, 0.1, 40);
  private u: Record<string, { value: any }>;
  private data = new Float32Array(W * ROWS * 4);
  private tex: DataTexture;
  private dirty = true;
  private slotNext = new Array(NP).fill(0);
  private formTarget = new Float32Array(MAX_ASSEMBLIES).fill(1);
  private pointer = { x: 0, y: 0, tx: 0, ty: 0 };
  private focus: RegionId | null = null;
  private focusMix = 0;
  private look = new Vector3(0, 0, 0);
  private w = 1;
  private h = 1;
  private occlude = 0;
  private lastS = 0;
  private reduced: boolean;
  private baseDist = 5;
  private arousal = 0.12;
  private arousalTarget = 0.12;
  private coreTone = new Vector3(...TONE_RGB.cyan);
  private coreTarget = new Vector3(...TONE_RGB.cyan);
  stats = { frames: 0, renderMs: 0 };

  constructor(private host: HTMLElement, private anat: Anatomy, reducedMotion: boolean) {
    this.reduced = reducedMotion;
    this.gl = new WebGLRenderer({ antialias: false, alpha: true, powerPreference: "high-performance", preserveDrawingBuffer: false });
    this.gl.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.gl.setClearColor(new Color(0, 0, 0), 0);
    this.canvas = this.gl.domElement;
    this.canvas.className = "nf-canvas";
    host.prepend(this.canvas);
    // every firing starts "long ago"; formation 1 (fully formed) for non-strategy assemblies
    for (const row of [ROW.asmFire, ROW.regFire]) for (let x = 0; x < W; x++) (this.px(x, row)[1] = FAR), (this.px(x, row)[3] = FAR);
    for (let x = 0; x < W; x++) {
      this.px(x, ROW.pattern)[0] = 1;
      this.px(x, ROW.slotTone)[1] = -1;
      for (const row of [ROW.asmTone, ROW.regTone]) this.px(x, row).set([5, 5, 0, 5]);
      this.px(x, ROW.trail)[1] = 5;
    }
    this.tex = new DataTexture(this.data, W, ROWS, RGBAFormat, FloatType);
    this.tex.magFilter = this.tex.minFilter = NearestFilter;
    this.tex.needsUpdate = true;
    const pr = this.gl.getPixelRatio();
    this.u = {
      uState: { value: this.tex }, uNow: { value: 0 }, uAmb: { value: reducedMotion ? 0 : 1 }, uFocus: { value: -1 }, uHover: { value: -1 }, uHoverAsm: { value: -1 },
      uState2: { value: 0 }, uArousal: { value: 0.12 }, uDist: { value: 5 }, uPR: { value: pr }, uScale: { value: 2 }, uBase: { value: 5 },
      uPal: { value: TONES.map((t) => new Vector3(...TONE_RGB[t])) }, uCoreTone: { value: this.coreTone },
    };
    const common = { transparent: true, depthWrite: false, depthTest: false, blending: AdditiveBlending };
    const geo = (g: Anatomy["points"]) => {
      const b = new BufferGeometry();
      b.setAttribute("position", new BufferAttribute(g.position, 3));
      b.setAttribute("aA", new BufferAttribute(g.a, 4));
      b.setAttribute("aB", new BufferAttribute(g.b, 4));
      b.setAttribute("aC", new BufferAttribute(g.c, 4));
      return b;
    };
    const lines = new LineSegments(geo(anat.lines), new ShaderMaterial({ uniforms: this.u, vertexShader: LINE_VERT, fragmentShader: LINE_FRAG, ...common }));
    const points = new Points(geo(anat.points), new ShaderMaterial({ uniforms: this.u, vertexShader: VERT, fragmentShader: FRAG, ...common }));
    this.scene.add(lines, points);
    for (const o of this.scene.children) o.frustumCulled = false;
    this.resize();
  }

  static supported(): boolean {
    try {
      return !!document.createElement("canvas").getContext("webgl2"); // the shaders use texelFetch (WebGL 2)
    } catch {
      return false;
    }
  }

  private px(x: number, row: number): Float32Array {
    const i = (row * W + x) * 4;
    return this.data.subarray(i, i + 4);
  }

  /** px hidden at the bottom by the NOW / WHY / NEXT band: the organism is centred in what remains */
  setOcclusion(px: number): void {
    if (Math.abs(px - this.occlude) > 1) {
      this.occlude = px;
      this.applyView();
    }
  }

  resize(): void {
    const r = this.host.getBoundingClientRect();
    this.w = Math.max(10, r.width);
    this.h = Math.max(10, r.height);
    this.gl.setSize(this.w, this.h, true);
    this.u.uPR.value = this.gl.getPixelRatio();
    this.applyView();
  }

  private applyView(): void {
    const H = this.h + this.occlude;
    const visH = Math.max(80, this.h - this.occlude);
    const vt = Math.tan(((this.camera.fov / 2) * Math.PI) / 180);
    // fit the organism (half-extents ≈ 2.5 × 1.15 world units) into the visible area
    this.baseDist = Math.max((1.15 * H) / (vt * visH), (2.5 * H) / (vt * this.w));
    this.camera.aspect = this.w / H;
    this.camera.setViewOffset(this.w, H, 0, this.occlude, this.w, this.h);
    this.camera.updateProjectionMatrix();
    this.u.uScale.value = Math.max(2.2, Math.min(5, visH / 170));
  }

  setPointer(nx: number, ny: number): void {
    this.pointer.tx = this.reduced ? 0 : nx;
    this.pointer.ty = this.reduced ? 0 : ny;
  }

  setHover(region: RegionId | null, asm: number | null): void {
    this.u.uHover.value = region ? REGION_INDEX[region] : -1;
    this.u.uHoverAsm.value = asm ?? -1;
  }

  setFocus(region: RegionId | null): void {
    this.focus = region;
    PATHWAYS.forEach(([a, b], p) => (this.px(p, ROW.trail)[2] = region && (a === region || b === region) ? 1 : 0));
    this.dirty = true;
  }

  /** the cognitive state sets only Tony's core arousal (and its tint); everything else is event-driven */
  setCognitive(s: CognitiveState): void {
    this.u.uState2.value = STATE_INDEX[s];
    this.arousalTarget = { WATCHING: 0.1, EVALUATING: 0.36, REJECTED: 0.16, EXECUTING: 0.26, HALTED: 0.03 }[s];
    this.coreTarget.set(...TONE_RGB[({ WATCHING: "cyan", EVALUATING: "amber", REJECTED: "white", EXECUTING: "green", HALTED: "red" } as const)[s]]);
  }

  // ---------------------------------------------------------------- activations (called by hero.ts)
  private fireTexel(x: number, rowFire: number, rowTone: number, tone: Tone, amp: number, now: number): void {
    const f = this.px(x, rowFire);
    const t = this.px(x, rowTone);
    const dt = now - f[1];
    const fastRes = f[0] * Math.exp(-dt / FAST_TAU_S);
    const memOld = f[2] * Math.exp(-(now - f[3]) / GLOW_TAU_S);
    const glowOld = GLOW_WEIGHT * f[0] * Math.exp(-dt / GLOW_TAU_S);
    // the previous firing's afterglow moves to the memory slot (capped: afterglow never becomes a heatmap)
    if (glowOld > memOld) t[1] = t[0];
    // a decisive firing (stronger than everything remembered) recolours the memory too: a rejection is red, not
    // red over the amber of the evaluation that preceded it
    if (amp > (memOld + glowOld + fastRes) * 1.2) t[1] = TI(tone);
    f[2] = Math.min(0.5, memOld + glowOld);
    f[3] = now;
    if (amp >= fastRes * 0.6) t[0] = TI(tone);
    f[0] = Math.min(1.3, Math.max(amp, fastRes));
    f[1] = now;
    this.dirty = true;
  }
  fireRegion(region: RegionId, tone: Tone, amp: number, now: number): void {
    this.fireTexel(REGION_INDEX[region], ROW.regFire, ROW.regTone, tone, amp, now);
  }
  fireAssembly(i: number, tone: Tone, amp: number, now: number): void {
    if (i >= 0 && i < MAX_ASSEMBLIES) this.fireTexel(i, ROW.asmFire, ROW.asmTone, tone, amp, now);
  }
  /** an impulse travelling along pathway p (from its first region to its second unless reverse); filter: the
   *  assembly whose filaments carry it fully (the proposing strategy, the rejecting check), -1 for all */
  impulse(p: number, reverse: boolean, tone: Tone, amp: number, filter: number, now: number, durS = 0.72): void {
    const k = this.slotNext[p]++ % SLOTS;
    this.px(p * SLOTS + k, ROW.slot).set([now, this.reduced ? 0.25 : durS, amp, reverse ? -1 : 1]);
    this.px(p * SLOTS + k, ROW.slotTone).set([TI(tone), filter, 0, 0]);
    this.dirty = true;
  }

  /** standing state from mapping.fieldState (alarm / degraded / quiet / sustain / strategy patterns / trail) */
  setStanding(o: Standing): void {
    for (let i = 0; i < REGIONS.length; i++) {
      this.px(i, ROW.regStatus).set([o.regionCode[i], o.regionQuiet[i] ? 1 : 0, 0, 0]);
      const t = this.px(i, ROW.regTone);
      t[2] = o.regionSustain[i];
      t[3] = TI(o.regionSustainTone[i]);
    }
    this.px(TISSUE, ROW.regStatus).set([0, o.regionQuiet[REGION_INDEX.CORE] ? 1 : 0, 0, 0]);
    for (let a = 0; a < MAX_ASSEMBLIES; a++) {
      const t = this.px(a, ROW.asmTone);
      const s = o.asmSustain.get(a);
      t[2] = s?.sustain ?? 0;
      t[3] = TI(s?.tone ?? "white");
      const p = o.patterns.get(a);
      const q = this.px(a, ROW.pattern);
      this.formTarget[a] = p ? p.formation : 1;
      if (this.reduced) q[0] = this.formTarget[a];
      q[1] = p ? p.base : 0;
      q[2] = p ? p.activity : 0;
      q[3] = TI(p?.tone ?? "white");
    }
    for (let p = 0; p < NP; p++) this.px(p, ROW.trail)[0] = 0;
    for (const t of o.trail) {
      const q = this.px(t.path, ROW.trail);
      if (t.amp >= q[0]) (q[0] = t.amp), (q[1] = TI(t.tone));
    }
    this.dirty = true;
  }

  clearTransient(): void {
    for (const row of [ROW.asmFire, ROW.regFire]) for (let x = 0; x < W; x++) this.px(x, row).set([0, FAR, 0, FAR]);
    for (let x = 0; x < W; x++) this.px(x, ROW.slot).set([0, 1, 0, 1]);
    this.dirty = true;
  }

  /** screen position (px, in the host box) of a world point */
  project(p: V3): Projected {
    const v = new Vector3(...p).project(this.camera);
    const e = new Vector3(p[0] + 0.3, p[1], p[2]).project(this.camera);
    return { x: (v.x * 0.5 + 0.5) * this.w, y: (-v.y * 0.5 + 0.5) * this.h, r: Math.abs(e.x - v.x) * 0.5 * this.w, depth: v.z };
  }

  /** nowS: the hero clock (seconds; frozen for screenshots) */
  frame(nowS: number): void {
    const dt = Math.min(0.1, Math.max(0, this.lastS ? nowS - this.lastS : 0.016));
    this.lastS = nowS;
    const t0 = performance.now();
    this.u.uNow.value = nowS;
    // the only per-frame CPU work: a few scalars (formation, core arousal, camera); no per-point loop
    const kf = Math.min(1, dt * 1.6);
    for (let a = 0; a < MAX_ASSEMBLIES; a++) {
      const q = this.px(a, ROW.pattern);
      if (Math.abs(q[0] - this.formTarget[a]) > 0.002) (q[0] += (this.formTarget[a] - q[0]) * kf), (this.dirty = true);
    }
    this.arousal += (this.arousalTarget - this.arousal) * Math.min(1, dt * 1.5);
    this.u.uArousal.value = this.arousal;
    this.coreTone.lerp(this.coreTarget, Math.min(1, dt * 1.2));
    if (this.dirty) (this.tex.needsUpdate = true), (this.dirty = false);
    const P = this.pointer;
    P.x += (P.tx - P.x) * Math.min(1, dt * 2);
    P.y += (P.ty - P.y) * Math.min(1, dt * 2);
    this.focusMix += ((this.focus ? 1 : 0) - this.focusMix) * Math.min(1, dt * (this.reduced ? 30 : 3));
    const fc = this.focus ? this.anat.centres[this.focus] : ([0, 0, 0] as V3);
    const target = new Vector3(fc[0] * this.focusMix, fc[1] * this.focusMix, fc[2] * this.focusMix);
    this.look.lerp(target, Math.min(1, dt * (this.reduced ? 30 : 3)));
    const dist = this.baseDist * (1 - 0.42 * this.focusMix);
    // slow parallax: a drift of a few hundredths of a unit over a minute, plus a little pointer parallax
    const drift = this.reduced ? 0 : 1;
    const dx = drift * 0.06 * Math.sin(nowS * 0.071) + P.x * 0.22;
    const dy = drift * 0.035 * Math.sin(nowS * 0.053 + 1.1) + P.y * 0.12;
    this.camera.position.set(this.look.x + dx, this.look.y + 0.05 + dy, this.look.z + dist);
    this.camera.lookAt(this.look);
    this.u.uDist.value = dist;
    this.u.uBase.value = this.baseDist;
    this.u.uFocus.value = this.focus && this.focusMix > 0.05 ? REGION_INDEX[this.focus] : -1;
    this.gl.render(this.scene, this.camera);
    this.stats.frames++;
    this.stats.renderMs += performance.now() - t0;
  }

  dispose(): void {
    this.gl.dispose();
    this.canvas.remove();
  }
}
