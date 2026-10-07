// Deterministic anatomy of the cognitive connectome (pure; no WebGL here, so it is unit-tested).
//
// Three scales in one organism (docs/architecture/observability.md §17.5 v3.2):
//  MACRO  six cognitive regions, Tony's integration core and the broker port, inside a faint bilateral cortical
//         envelope drawn only by neuron density (two lobes, a lower-density but never empty midline, curved gyral
//         density ridges; there is no outline anywhere).
//  MESO   neural assemblies inside each region (regions.ts `assemblies`): one per strategy, one per family of
//         Governor checks, one per execution process, ... Each region has its own morphology: long sensory
//         dendritic fans in PERCEPTION, dense competing assemblies in STRATEGY, a compact gating lattice in RISK,
//         an elongated allocation mesh in PORTFOLIO, few but clear outbound bundles in EXECUTION, a deep diffuse
//         sheet in RESEARCH, and highly interconnected sub-nuclei in CORE.
//  MICRO  thousands of cells, dendritic branches, synapse links, and axonal bundles of 5–30 filaments per real
//         pathway (some branch, some terminate early, some stay dormant; they converge on their assemblies).
// Every vertex carries what the shader needs to light it from state (aA, aB, aC; see Anatomy), so activation,
// decay and afterglow are computed on the GPU and no per-point work happens on the CPU after this build.
// Positions are world units: x right, y up, z towards the viewer.
import { type AssemblySpec, MAX_STRATEGIES, REGIONS, type RegionId, assemblies } from "./regions";

export type V3 = [number, number, number];
/** region index of the connective tissue and the fibres (never activated by a region fire on its own) */
export const TISSUE = REGIONS.length;

/** point kinds (aC.w) and line kinds */
export const KIND = { tissue: 0, deep: 1, neuron: 2, dendrite: 3, bead: 4, core: 5 } as const;
export const LKIND = { synapse: 0, dendrite: 1, fibre: 2, core: 3, lattice: 4, tract: 5 } as const;

/** pathways (undirected axonal bundles): exactly the region pairs that real flows connect */
export const PATHWAYS: [RegionId, RegionId][] = [
  ["BROKER", "PERCEPTION"], // tick: market data from the (fake) broker feed
  ["PERCEPTION", "CORE"], // regime → Tony
  ["PERCEPTION", "STRATEGY"], // clean ticks → strategies
  ["CORE", "RESEARCH"], // regime → strategy factory
  ["CORE", "PORTFOLIO"], // budget → allocator
  ["STRATEGY", "PORTFOLIO"], // intent → allocator (routed through Tony's core)
  ["PORTFOLIO", "RISK"], // intent → Governor, verdict back (routed around Tony's core)
  ["RISK", "EXECUTION"], // approve / kill → execution
  ["RISK", "CORE"], // kill → Tony
  ["EXECUTION", "BROKER"], // order out, ack / fill back
  ["EXECUTION", "RESEARCH"], // fill → post-trade
  ["RESEARCH", "STRATEGY"], // validation → strategies (lifecycle)
];
export function pathwayIndex(a: RegionId, b: RegionId): { index: number; reverse: boolean } | null {
  for (let i = 0; i < PATHWAYS.length; i++) {
    const [x, y] = PATHWAYS[i];
    if (x === a && y === b) return { index: i, reverse: false };
    if (x === b && y === a) return { index: i, reverse: true };
  }
  return null;
}
/** filaments per bundle (5–30): more where more real traffic flows */
export const FILAMENTS: number[] = [8, 16, 24, 8, 12, 26, 22, 20, 10, 12, 6, 10];
/** the spine of each bundle; STRATEGY–PORTFOLIO passes through Tony's core and PORTFOLIO–RISK around it, so the
 *  real order Strategy → Portfolio sizing → Risk reads as thought crossing the integration centre */
const VIA: V3[][] = [
  [[1.3, -0.82, -0.6], [-0.35, -0.9, -0.75], [-1.6, -0.62, -0.4]], // the feed runs deep, under and behind
  [[-0.95, -0.06, 0.12]],
  [[-1.4, 0.3, 0.05]],
  [[0.02, 0.48, -0.36]],
  [[0.05, -0.3, 0.06]],
  [[-0.42, 0.26, 0.02], [-0.07, 0.0, 0.0], [-0.03, -0.34, 0.1]],
  [[0.34, -0.42, 0.1], [0.24, -0.02, -0.06], [0.58, 0.14, 0.04]],
  [[1.22, -0.06, 0.06]],
  [[0.46, 0.22, -0.12]],
  [[2.0, -0.36, 0.05]],
  [[1.05, 0.55, -0.62]],
  [[-0.56, 0.76, -0.32]],
];

interface Morph { cells: number; sigma: V3; dend: number; dlen: [number, number]; dir?: V3; spread: number; tissue: number; tsig: V3 }
const MORPH: Record<RegionId, Morph> = {
  PERCEPTION: { cells: 70, sigma: [0.04, 0.05, 0.05], dend: 11, dlen: [0.25, 0.55], dir: [-1, 0, 0], spread: 1.25, tissue: 520, tsig: [0.26, 0.5, 0.3] },
  STRATEGY: { cells: 150, sigma: [0.04, 0.034, 0.04], dend: 7, dlen: [0.07, 0.15], spread: Math.PI, tissue: 620, tsig: [0.5, 0.28, 0.3] },
  RISK: { cells: 52, sigma: [0.026, 0.026, 0.03], dend: 3, dlen: [0.05, 0.09], spread: Math.PI, tissue: 340, tsig: [0.24, 0.24, 0.22] },
  PORTFOLIO: { cells: 84, sigma: [0.07, 0.026, 0.04], dend: 5, dlen: [0.08, 0.16], dir: [1, 0, 0], spread: 0.5, tissue: 360, tsig: [0.42, 0.13, 0.24] },
  EXECUTION: { cells: 60, sigma: [0.034, 0.03, 0.03], dend: 3, dlen: [0.1, 0.22], dir: [1, -0.1, 0], spread: 0.6, tissue: 260, tsig: [0.26, 0.2, 0.22] },
  RESEARCH: { cells: 90, sigma: [0.12, 0.04, 0.06], dend: 6, dlen: [0.1, 0.2], spread: Math.PI, tissue: 520, tsig: [0.62, 0.17, 0.26] },
  CORE: { cells: 55, sigma: [0.035, 0.022, 0.05], dend: 5, dlen: [0.06, 0.14], spread: Math.PI, tissue: 520, tsig: [0.17, 0.15, 0.22] },
  BROKER: { cells: 80, sigma: [0.024, 0.032, 0.024], dend: 0, dlen: [0, 0], spread: 0, tissue: 40, tsig: [0.05, 0.07, 0.05] },
};

/** where each assembly sits (pure; deterministic), by region morphology */
export function assemblyCentre(a: AssemblySpec, i: number, n: number): V3 {
  const f = n <= 1 ? 0.5 : i / (n - 1);
  switch (a.region) {
    case "PERCEPTION":
      return [-1.74 - 0.12 * Math.sin(f * Math.PI), 0.46 - f * 0.98, 0.16 * Math.cos(f * 5.1)];
    case "STRATEGY": // an irregular arc of competing assemblies
      return [-1.26 + f * 0.8 + 0.05 * Math.sin(i * 2.3), 0.5 + 0.13 * Math.sin(f * Math.PI) - 0.07 * Math.cos(i * 1.7), -0.06 + 0.2 * Math.cos(f * Math.PI * 1.7)];
    case "RISK": { // a compact lattice: 3 + 2 + 2
      const row = i < 3 ? 0 : i < 5 ? 1 : 2;
      const col = i < 3 ? i : i < 5 ? i - 3 + 0.5 : i - 5 + 0.25;
      return [0.74 + col * 0.13 + row * 0.03, 0.36 - row * 0.13, 0.04 + 0.05 * Math.sin(i * 1.9)];
    }
    case "PORTFOLIO":
      return [-0.36 + f * 0.74, -0.6 + 0.05 * Math.sin(i * 2.1), 0.16 + 0.05 * Math.cos(i * 1.3)];
    case "EXECUTION":
      return i < 3 ? [1.4 + 0.05 * i, -0.2 - i * 0.14, 0.08 - 0.05 * i] : [1.78, -0.4, 0.04];
    case "RESEARCH":
      return [-0.68 + f * 1.3, 0.88 - 0.06 * Math.sin(f * Math.PI * 2), -0.58 + 0.1 * Math.sin(i * 1.4)];
    case "CORE":
      return [[-0.12, 0.09, -0.12], [0.09, 0.14, 0.08], [0.08, -0.06, -0.24], [-0.04, -0.1, 0.15]][i % 4] as V3;
    default:
      return [2.38, -0.44, 0];
  }
}

export interface Anatomy {
  /** points: position xyz; aA = (region, assembly, pathway, pathT); aB = (seed, rest, delay, weight); aC = (scatter xyz | asmA, asmB, 0, kind) */
  points: { position: Float32Array; a: Float32Array; b: Float32Array; c: Float32Array; count: number };
  /** line segments (2 vertices each) with the same attributes; aC.w is the line kind */
  lines: { position: Float32Array; a: Float32Array; b: Float32Array; c: Float32Array; count: number };
  assemblies: AssemblySpec[];
  asmCentres: V3[];
  centres: Record<RegionId, V3>;
  strategyIds: string[];
  /** filaments per pathway actually built, and how many branch / terminate early / stay dormant */
  bundles: { filaments: number; branches: number; terminated: number; dormant: number }[];
}

export function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** the bilateral cortical density envelope (0..1): two lobes, a reduced midline, curved gyral ridges. Pure. */
export function cortexDensity(x: number, y: number): number {
  const e1 = ((x + 1.1) / 1.24) ** 2 + ((y - 0.06) / (y > 0 ? 1.02 : 1.12)) ** 2;
  const e2 = ((x - 1.08) / 1.17) ** 2 + ((y + 0.02) / (y > 0 ? 0.97 : 1.06)) ** 2;
  const e = Math.min(e1, e2) + 0.08 * Math.sin(5.3 * x + 1.1) * Math.sin(4.1 * y - 0.7); // no clean edge
  const t = Math.min(1, Math.max(0, (1.04 - e) / 0.42));
  let p = t * t * (3 - 2 * t);
  p *= 0.55 + 0.45 * Math.exp(-(((e - 0.8) / 0.16) ** 2)); // a denser cortical ribbon near each lobe's rim
  p *= 1 - 0.9 * Math.exp(-((x - 0.02) ** 2) / 0.02) * (1 - Math.exp(-(y * y) / 0.05)); // midline fissure, bridged at the core
  p *= 0.52 + 0.48 * (0.5 + 0.5 * Math.sin(8.6 * (x * 0.56 + y * 0.83) + 2.3 * Math.sin(2.2 * x - 1.6 * y))); // gyri
  return p;
}

/** resting brightness of a vertex (alpha units). Most of the connectome sits below BARELY (design: 80–90 %). */
export const BARELY = 0.15;
export function restingBrightness(kind: number, region: number, seed: number): number {
  const core = region === REGIONS.indexOf("CORE");
  switch (kind) {
    case KIND.tissue: return 0.07 + 0.12 * seed ** 2.5;
    case KIND.deep: return 0.1 + 0.05 * seed;
    case KIND.dendrite: return 0.06 + 0.07 * seed;
    case KIND.bead: return 0.05 + 0.05 * seed;
    case KIND.core: return 0.12 + 0.2 * seed * seed;
    default: return (core ? 0.12 : 0.08) + 0.24 * seed ** 3; // neurons: a few brighter cells per assembly
  }
}

export function buildAnatomy(strategyIds: string[], seed = 320): Anatomy {
  const r = rng(seed);
  const gauss = () => Math.sqrt(-2 * Math.log(Math.max(1e-9, r()))) * Math.cos(2 * Math.PI * r());
  const ids = strategyIds.slice(0, MAX_STRATEGIES);
  const asm = assemblies(ids);
  const RI = (id: RegionId) => REGIONS.indexOf(id);
  const byRegion = (id: RegionId) => asm.map((a, i) => ({ a, i })).filter((x) => x.a.region === id);
  const asmCentres: V3[] = asm.map((a) => {
    const list = byRegion(a.region);
    const j = list.findIndex((x) => x.a.id === a.id);
    return assemblyCentre(a, j, list.length);
  });
  const centres = {} as Record<RegionId, V3>;
  for (const id of REGIONS) {
    const l = byRegion(id).map((x) => asmCentres[x.i]);
    centres[id] = [0, 1, 2].map((k) => l.reduce((s, p) => s + p[k], 0) / Math.max(1, l.length)) as V3;
  }
  // ------------------------------------------------------------ point and line writers
  const P: number[] = [], PA: number[] = [], PB: number[] = [], PC: number[] = [];
  const L: number[] = [], LA: number[] = [], LB: number[] = [], LC: number[] = [];
  const point = (p: V3, region: number, a: number, path: number, pt: number, delay: number, w: number, kind: number, c: V3 = [0, 0, 0]) => {
    const s = r();
    P.push(p[0], p[1], p[2]);
    PA.push(region, a, path, pt);
    PB.push(s, restingBrightness(kind, region, s), delay, w);
    PC.push(c[0], c[1], c[2], kind);
    return P.length / 3 - 1;
  };
  const vtx = (p: V3, region: number, a: number, path: number, pt: number, delay: number, w: number, kind: number, c: V3, s: number, rest: number) => {
    L.push(p[0], p[1], p[2]);
    LA.push(region, a, path, pt);
    LB.push(s, rest, delay, w);
    LC.push(c[0], c[1], c[2], kind);
  };
  const seg = (p: V3, q: V3, region: number, a: number, delay: [number, number], w: number, kind: number, c: V3 = [0, 0, 0], path = -1, pt: [number, number] = [0, 0]) => {
    const s = r();
    const rest = kind === LKIND.fibre ? 0.016 + 0.018 * s : kind === LKIND.tract ? 0.035 + 0.04 * s : kind === LKIND.core ? 0.06 + 0.06 * s : kind === LKIND.synapse ? 0.035 + 0.035 * s : 0.045 + 0.05 * s;
    vtx(p, region, a, path, pt[0], delay[0], w, kind, c, s, rest);
    vtx(q, region, a, path, pt[1], delay[1], w, kind, c, s, rest);
  };
  const add = (a: V3, b: V3, k = 1): V3 => [a[0] + b[0] * k, a[1] + b[1] * k, a[2] + b[2] * k];
  const dist = (a: V3, b: V3) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
  // dendritic tree: a recursive branching chain of short segments with a node cell at every joint
  const tree = (root: V3, dir: V3, len: number, depth: number, region: number, a: number, d0: number, c: V3) => {
    let p = root;
    let d = dir;
    const steps = 3 + Math.floor(r() * 3);
    for (let s = 0; s < steps; s++) {
      d = [d[0] + gauss() * 0.2, d[1] + gauss() * 0.2, d[2] + gauss() * 0.15];
      const n = Math.hypot(...d) || 1;
      d = [d[0] / n, d[1] / n, d[2] / n];
      const q = add(p, d, len / steps);
      const dl: [number, number] = [d0 + dist(root, p) * 1.4, d0 + dist(root, q) * 1.4];
      seg(p, q, region, a, dl, 1, LKIND.dendrite, c);
      if (r() < 0.55) point(q, region, a, -1, 0, dl[1], 1, KIND.dendrite, c);
      if (depth > 0 && r() < 0.4) tree(q, [d[0] + gauss() * 0.7, d[1] + gauss() * 0.7, d[2]], len * 0.55, depth - 1, region, a, dl[1], c);
      p = q;
    }
  };
  // ------------------------------------------------------------ MESO: assemblies (cells + dendrites)
  const cellIdx: number[][] = asm.map(() => []);
  asm.forEach((A, ai) => {
    const M = MORPH[A.region];
    const ri = RI(A.region);
    const c = asmCentres[ai];
    const strat = A.id.startsWith("S:");
    const kind = A.region === "CORE" ? KIND.core : KIND.neuron;
    const rot = r() * Math.PI;
    for (let i = 0; i < M.cells; i++) {
      // each strategy assembly has its own micro-pattern: a dense knot with a few lobes at its own angles
      let o: V3 = [gauss() * M.sigma[0], gauss() * M.sigma[1], gauss() * M.sigma[2]];
      if (strat && r() < 0.45) {
        const lobes = 3 + (ai % 3);
        const th = rot + (Math.floor(r() * lobes) / lobes) * Math.PI * 2;
        const rr = 0.05 + 0.03 * r();
        o = [Math.cos(th) * rr + gauss() * 0.012, Math.sin(th) * rr * 0.8 + gauss() * 0.012, gauss() * 0.02];
      }
      if (A.region === "CORE") { const cs = Math.cos(rot), sn = Math.sin(rot); o = [o[0] * cs - o[1] * sn, o[0] * sn + o[1] * cs, o[2]]; }
      const p = add(c, o);
      // unformed strategy assemblies scatter outward (the shader moves cells by aScatter × (1 − formation))
      const sc: V3 = strat ? [o[0] * 2.2 + gauss() * 0.06, o[1] * 2.2 + gauss() * 0.05, gauss() * 0.08] : [0, 0, 0];
      cellIdx[ai].push(point(p, ri, ai, -1, 0, dist(p, c) * 1.6, 1, kind, sc));
    }
    for (let k = 0; k < M.dend; k++) {
      const th = (M.dir ? Math.atan2(M.dir[1], M.dir[0]) : 0) + (M.dir ? (r() - 0.5) * 2 * M.spread : r() * Math.PI * 2);
      const len = M.dlen[0] + (M.dlen[1] - M.dlen[0]) * r();
      tree(add(c, [gauss() * M.sigma[0], gauss() * M.sigma[1], 0]), [Math.cos(th), Math.sin(th), gauss() * 0.3], len, A.region === "PERCEPTION" ? 2 : 1, ri, ai, 0.05, [0, 0, 0]);
    }
  });
  // synapses inside each assembly (nearest of a few candidates), heavier inside Tony's core
  asm.forEach((A, ai) => {
    const ids2 = cellIdx[ai];
    const frac = A.region === "CORE" ? 0.9 : A.id.startsWith("S:") ? 0.5 : 0.45;
    const pos = (j: number): V3 => [P[j * 3], P[j * 3 + 1], P[j * 3 + 2]];
    for (const i of ids2) {
      if (r() > frac) continue;
      let best = -1, bd = Infinity;
      for (let t = 0; t < 10; t++) {
        const j = ids2[Math.floor(r() * ids2.length)];
        if (j === i) continue;
        const dd = dist(pos(i), pos(j));
        if (dd < bd) (bd = dd), (best = j);
      }
      if (best >= 0 && bd < 0.09) seg(pos(i), pos(best), PA[i * 4], ai, [PB[i * 4 + 2], PB[best * 4 + 2]], 1, A.region === "CORE" ? LKIND.core : LKIND.dendrite, [PC[i * 4], PC[i * 4 + 1], PC[i * 4 + 2]]);
    }
  });
  // CORE: three more unnamed sub-nuclei and dense cross-links between all nuclei (the integration nexus)
  const coreCells: number[] = byRegion("CORE").flatMap((x) => cellIdx[x.i]);
  const extra: V3[] = [[-0.21, -0.02, -0.3], [0.2, -0.05, -0.12], [0.01, 0.21, 0.2], [-0.17, 0.17, 0.08], [0.16, 0.2, -0.26], [-0.02, -0.18, -0.05], [0.0, 0.02, 0.0], [0.24, 0.08, 0.12]];
  for (const c of extra) {
    const ax = r() * Math.PI, el = 0.6 + r();
    for (let i = 0; i < 55; i++) {
      const u = gauss() * 0.045 * el, v = gauss() * 0.022;
      coreCells.push(point(add(c, [u * Math.cos(ax) - v * Math.sin(ax), u * Math.sin(ax) + v * Math.cos(ax), gauss() * 0.06]), RI("CORE"), -1, -1, 0, 0.15 + r() * 0.2, 1, KIND.core));
    }
  }
  for (let k = 0; k < 3200; k++) {
    const i = coreCells[Math.floor(r() * coreCells.length)];
    const j = coreCells[Math.floor(r() * coreCells.length)];
    const p: V3 = [P[i * 3], P[i * 3 + 1], P[i * 3 + 2]], q: V3 = [P[j * 3], P[j * 3 + 1], P[j * 3 + 2]];
    const d = dist(p, q);
    if (d > 0.015 && d < 0.13) seg(p, q, RI("CORE"), -1, [PB[i * 4 + 2], PB[j * 4 + 2]], 1, LKIND.core);
  }
  // RISK: the gating lattice joins neighbouring checks; PORTFOLIO: an allocation mesh across its assemblies
  const latt = (region: RegionId, maxD: number, strands: number) => {
    const l = byRegion(region);
    for (const x of l)
      for (const y of l) {
        if (x.i >= y.i || dist(asmCentres[x.i], asmCentres[y.i]) > maxD) continue;
        for (let s = 0; s < strands; s++) {
          const a0 = add(asmCentres[x.i], [gauss() * 0.015, gauss() * 0.015, gauss() * 0.015]);
          const b0 = add(asmCentres[y.i], [gauss() * 0.015, gauss() * 0.015, gauss() * 0.015]);
          // a slightly sagging, jittered strand of a few segments (never a ruled line)
          const sag: V3 = [gauss() * 0.025, gauss() * 0.03, gauss() * 0.025];
          let prev = a0;
          const K = 5;
          for (let q = 1; q <= K; q++) {
            const t = q / K, bell = Math.sin(Math.PI * t);
            const pt: V3 = [a0[0] + (b0[0] - a0[0]) * t + sag[0] * bell + gauss() * 0.006, a0[1] + (b0[1] - a0[1]) * t + sag[1] * bell + gauss() * 0.006, a0[2] + (b0[2] - a0[2]) * t + sag[2] * bell];
            seg(prev, pt, RI(region), -1, [0.05 + 0.2 * (t - 1 / K), 0.05 + 0.2 * t], 1, LKIND.lattice);
            if (r() < 0.3) point(pt, RI(region), -1, -1, 0, 0.05 + 0.2 * t, 1, KIND.dendrite);
            prev = pt;
          }
        }
      }
  };
  latt("RISK", 0.2, 3);
  latt("PORTFOLIO", 0.8, 2);
  // region tissue: the body of each region around its assemblies (weight 1, no assembly)
  for (const id of REGIONS) {
    const M = MORPH[id];
    const c = centres[id];
    for (let i = 0, guard = 0; i < M.tissue && guard < M.tissue * 20; guard++) {
      const p = add(c, [gauss() * M.tsig[0], gauss() * M.tsig[1], gauss() * M.tsig[2]]);
      // region tissue follows the cortical envelope too (so the midline and the silhouette come from density)
      if (id !== "BROKER" && id !== "CORE" && r() > 0.25 + 0.75 * cortexDensity(p[0], p[1])) continue;
      i++;
      point(p, RI(id), -1, -1, 0, Math.min(0.9, dist(p, c) * 1.3), 1, id === "CORE" ? KIND.core : KIND.tissue);
    }
  }
  // ------------------------------------------------------------ MICRO: cortical tissue that joins everything
  const nearest = (p: V3): { region: number; w: number; d: number } => {
    let best = 0, bd = Infinity;
    REGIONS.forEach((id, k) => {
      const t = MORPH[id].tsig;
      const d = Math.hypot((p[0] - centres[id][0]) / (t[0] + 0.25), (p[1] - centres[id][1]) / (t[1] + 0.25));
      if (d < bd) (bd = d), (best = k);
    });
    return { region: best, w: Math.exp(-bd * bd * 0.9) * 0.85, d: bd };
  };
  const tissueStart = P.length / 3;
  let made = 0;
  for (let guard = 0; made < 6800 && guard < 300000; guard++) {
    const x = -2.45 + r() * 4.9, y = -1.18 + r() * 2.36;
    if (r() > cortexDensity(x, y)) continue;
    const z = gauss() * 0.24 - 0.05;
    const nr = nearest([x, y, z]);
    point([x, y, z], nr.region, -1, -1, 0, Math.min(1.2, nr.d * 0.45), nr.w, KIND.tissue);
    made++;
  }
  for (let i = 0; i < 560; i++) { // the deep connective layer: far behind, defocused
    const x = -2.3 + r() * 4.6, y = -1.05 + r() * 2.1;
    if (r() > cortexDensity(x, y) + 0.08) continue;
    const z = -0.62 - r() * 0.45;
    const nr = nearest([x, y, z]);
    point([x, y, z], nr.region, -1, -1, 0, Math.min(1.2, nr.d * 0.5), nr.w * 0.5, KIND.deep);
  }
  // synapse links between neighbouring tissue cells (grid hash)
  const tissueEnd = P.length / 3;
  // micro-dendrites: about half the tissue cells grow a short curved neurite (biological texture, not dots)
  for (let i = tissueStart; i < tissueEnd; i++) {
    if (r() > 0.22) continue;
    const p0: V3 = [P[i * 3], P[i * 3 + 1], P[i * 3 + 2]];
    const th = r() * Math.PI * 2, len = 0.018 + 0.035 * r(), bend = gauss() * 0.6;
    const p1 = add(p0, [Math.cos(th) * len * 0.5, Math.sin(th) * len * 0.5, gauss() * 0.005]);
    const p2 = add(p1, [Math.cos(th + bend) * len * 0.5, Math.sin(th + bend) * len * 0.5, gauss() * 0.005]);
    const dl: [number, number] = [PB[i * 4 + 2], PB[i * 4 + 2]];
    seg(p0, p1, PA[i * 4], -1, dl, PB[i * 4 + 3], LKIND.synapse);
    seg(p1, p2, PA[i * 4], -1, dl, PB[i * 4 + 3], LKIND.synapse);
  }
  const cell = 0.075;
  const grid = new Map<number, number[]>();
  const gk = (gx: number, gy: number) => (gx + 200) * 1000 + gy + 200;
  const key = (x: number, y: number) => gk(Math.floor(x / cell), Math.floor(y / cell));
  for (let i = tissueStart; i < tissueEnd; i++) {
    const k = key(P[i * 3], P[i * 3 + 1]);
    const l = grid.get(k);
    if (l) l.push(i);
    else grid.set(k, [i]);
  }
  for (let i = tissueStart; i < tissueEnd; i++) {
    if (r() > 0.3) continue;
    const x = P[i * 3], y = P[i * 3 + 1], z = P[i * 3 + 2];
    let best = -1, bd = Infinity;
    for (let gx = -1; gx <= 1; gx++)
      for (let gy = -1; gy <= 1; gy++)
        for (const j of grid.get(gk(Math.floor(x / cell) + gx, Math.floor(y / cell) + gy)) ?? []) {
          if (j <= i) continue;
          const d = Math.hypot(P[j * 3] - x, P[j * 3 + 1] - y, P[j * 3 + 2] - z);
          if (d < bd) (bd = d), (best = j);
        }
    if (best >= 0 && bd < 0.085) seg([x, y, z], [P[best * 3], P[best * 3 + 1], P[best * 3 + 2]], PA[i * 4], -1, [PB[i * 4 + 2], PB[best * 4 + 2]], PB[i * 4 + 3], LKIND.synapse);
  }
  // association tracts: short faint streamlines that follow the cortical flow (white-matter texture). They belong
  // to the nearest region by weight, so a region firing bleeds a little into them; they never carry an impulse.
  // the flow mixes a radiation from Tony's core (everything integrates there) with the gyral direction
  const flow = (x: number, y: number): [number, number] => {
    const rx = x - centres.CORE[0], ry = y - centres.CORE[1];
    const rn = Math.hypot(rx, ry) || 1;
    const lx = x < 0.02 ? x + 1.1 : x - 1.08, ly = y - 0.02; // wrap around the nearer lobe (cortical folding)
    const ln = Math.hypot(lx, ly) || 1;
    const wob = 0.6 * Math.sin(2.3 * x - 1.4 * y);
    const dx = (rx / rn) * 0.3 + (-ly / ln) * 0.7 + wob * 0.2, dy = (ry / rn) * 0.3 + (lx / ln) * 0.7 - wob * 0.1;
    const n = Math.hypot(dx, dy) || 1;
    return [dx / n, dy / n];
  };
  for (let n = 0, guard = 0; n < 640 && guard < 20000; guard++) {
    let x = -2.3 + r() * 4.5, y = -1.05 + r() * 2.1;
    if (r() > cortexDensity(x, y)) continue;
    n++;
    let z = gauss() * 0.22 - 0.1;
    const len = 0.12 + r() * 0.22;
    const steps = 4;
    const curl = gauss() * 0.25;
    const nr = nearest([x, y, z]);
    const dir = r() < 0.5 ? 1 : -1;
    for (let q = 0; q < steps; q++) {
      const [fx0, fy0] = flow(x, y);
      const ca = Math.cos(curl * q), sa = Math.sin(curl * q);
      const fx = fx0 * ca - fy0 * sa, fy = fx0 * sa + fy0 * ca;
      const nx = x + fx * dir * (len / steps), ny = y + fy * dir * (len / steps), nz = z + gauss() * 0.008;
      if (Math.sign(nx - 0.02) !== Math.sign(x - 0.02) && Math.abs(ny) > 0.3) break; // tracts cross the midline only at the core
      seg([x, y, z], [nx, ny, nz], nr.region, -1, [Math.min(1.2, nr.d * 0.45), Math.min(1.2, nr.d * 0.45)], nr.w * 0.7, LKIND.tract);
      x = nx; y = ny; z = nz;
    }
  }
  // ------------------------------------------------------------ axonal bundles
  const catmull = (pts: V3[], t: number): V3 => {
    const n = pts.length - 1;
    const f = Math.min(n - 1e-6, Math.max(0, t * n));
    const i = Math.floor(f), u = f - i;
    const p0 = pts[Math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[Math.min(n, i + 2)];
    const o: V3 = [0, 0, 0];
    for (let k = 0; k < 3; k++)
      o[k] = 0.5 * (2 * p1[k] + (-p0[k] + p2[k]) * u + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * u * u + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * u * u * u);
    return o;
  };
  const bundles: Anatomy["bundles"] = [];
  const N = 28;
  PATHWAYS.forEach(([A, B], pi) => {
    const la = byRegion(A), lb = byRegion(B);
    const spine: V3[] = [centres[A], ...VIA[pi], centres[B]];
    const st = { filaments: 0, branches: 0, terminated: 0, dormant: 0 };
    let span = 0;
    for (let k = 1; k < spine.length; k++) span += dist(spine[k - 1], spine[k]);
    const loose = Math.min(2.6, Math.max(1, span / 1.3));
    for (let f = 0; f < FILAMENTS[pi]; f++) {
      const ea = la[(f + Math.floor(r() * 2)) % la.length].i;
      const eb = lb[(f * 3 + Math.floor(r() * 2)) % lb.length].i;
      const sa = add(asmCentres[ea], [gauss() * 0.02, gauss() * 0.02, gauss() * 0.02]);
      const sb = add(asmCentres[eb], [gauss() * 0.02, gauss() * 0.02, gauss() * 0.02]);
      const offA: V3 = [sa[0] - spine[0][0], sa[1] - spine[0][1], sa[2] - spine[0][2]];
      const offB: V3 = [sb[0] - spine[spine.length - 1][0], sb[1] - spine[spine.length - 1][1], sb[2] - spine[spine.length - 1][2]];
      // fasciculated: tight in the middle; long tracts loosen (defasciculate) so they read as tissue, not a cable
      const o: V3 = [gauss() * 0.03 * loose, gauss() * 0.03 * loose, gauss() * 0.045 * loose];
      const ph = r() * 6.28, fr = 6 + r() * 8, ph2 = r() * 6.28, mea = (loose - 1) * 0.03 * (0.5 + r());
      const u = r();
      const dormant = f >= 3 && u < 0.25; // a few filaments of every bundle always carry
      const tEnd = !dormant && u < 0.45 ? 0.45 + r() * 0.4 : 1;
      const w = dormant ? 0 : 0.8 + 0.2 * r();
      st.filaments++;
      if (dormant) st.dormant++;
      if (tEnd < 1) st.terminated++;
      const at = (t: number): V3 => {
        const s = catmull(spine, t);
        const bell = Math.pow(Math.sin(Math.PI * t), 0.7);
        const wig = 0.007 * Math.sin(t * fr + ph);
        const me = mea * bell * Math.sin(t * (5 + 4 * ((ph2 * 7) % 1)) + ph2); // a slow meander: filaments of a long tract cross and re-cross
        return [
          s[0] + (offA[0] * (1 - t) + offB[0] * t) * (1 - bell) + o[0] * bell + wig,
          s[1] + (offA[1] * (1 - t) + offB[1] * t) * (1 - bell) + o[1] * bell - wig + me,
          s[2] + (offA[2] * (1 - t) + offB[2] * t) * (1 - bell) + o[2] * bell + me * 1.4,
        ];
      };
      const asmTag: V3 = [ea, eb, 0];
      const steps = Math.max(2, Math.round(N * tEnd));
      let prev = at(0);
      for (let s = 1; s <= steps; s++) {
        const t = (s / steps) * tEnd;
        const q = at(t);
        seg(prev, q, TISSUE, -1, [0, 0], w, LKIND.fibre, asmTag, pi, [((s - 1) / steps) * tEnd, t]);
        if (r() < 0.28) point(add(q, [gauss() * 0.006, gauss() * 0.006, gauss() * 0.006]), TISSUE, -1, pi, t, 0, w, KIND.bead, asmTag);
        prev = q;
      }
      if (tEnd < 1) point(prev, TISSUE, -1, pi, tEnd, 0, w, KIND.bead, asmTag); // a terminal bouton
      // a collateral branch leaves the bundle into the surrounding tissue
      if (!dormant && r() < 0.42) {
        st.branches++;
        const tb = 0.25 + r() * 0.5;
        let p = at(tb);
        const dir: V3 = [gauss(), gauss(), gauss() * 0.5];
        const n = Math.hypot(...dir) || 1;
        const len = 0.1 + r() * 0.2;
        const bs = 6;
        for (let s = 1; s <= bs; s++) {
          const q = add(at(tb + (s / bs) * 0.04), [(dir[0] / n) * len * (s / bs), (dir[1] / n) * len * (s / bs), (dir[2] / n) * len * (s / bs)]);
          seg(p, q, TISSUE, -1, [0, 0], w * 0.6, LKIND.fibre, asmTag, pi, [tb + ((s - 1) / bs) * 0.12, tb + (s / bs) * 0.12]);
          p = q;
        }
        point(p, TISSUE, -1, pi, tb + 0.12, 0, w * 0.6, KIND.bead, asmTag);
      }
    }
    bundles.push(st);
  });
  const f32 = (a: number[]) => new Float32Array(a);
  return {
    points: { position: f32(P), a: f32(PA), b: f32(PB), c: f32(PC), count: P.length / 3 },
    lines: { position: f32(L), a: f32(LA), b: f32(LB), c: f32(LC), count: L.length / 3 },
    assemblies: asm, asmCentres, centres, strategyIds: ids, bundles,
  };
}
