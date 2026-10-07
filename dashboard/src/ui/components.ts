// Reusable view components (docs/architecture/observability.md §17.5). Each returns markup built only from the classes in
// design/components.css, so every screen shares one typography, spacing, status and metric language.
import { esc, tip } from "./dom";

export type Tone = "pos" | "neg" | "warn" | "accent" | "muted" | "replay" | "";

export const toneOf = (x: number | null | undefined, eps = 1e-9): Tone =>
  x === null || x === undefined || Math.abs(x) < eps ? "" : x > 0 ? "pos" : "neg";

export function regionHead(title: string, meta = "", tipText = ""): string {
  return `<div class="region-h"><span class="eyebrow"${tipText ? tip(tipText) : ""}>${esc(title)}</span>${meta ? `<span class="meta">${meta}</span>` : ""}</div>`;
}

export function metric(o: { label: string; value: string; sub?: string; tone?: Tone; size?: "xl" | "lg" | "md" | ""; tipText?: string; id?: string }): string {
  return `<div class="metric ${o.size ?? ""}"${o.tipText ? tip(o.tipText) : ""}><span class="metric-label">${esc(o.label)}</span><span class="metric-value ${o.tone ? `tone-${o.tone}` : ""}"${o.id ? ` id="${o.id}"` : ""}>${o.value}</span>${o.sub ? `<span class="metric-sub">${o.sub}</span>` : ""}</div>`;
}

export function kv(rows: [string, string, Tone?, string?][]): string {
  return `<div class="kv">${rows
    .map(([k, v, t, tt]) => `<span class="k"${tt ? tip(tt) : ""}>${esc(k)}</span><span class="v ${t ? `tone-${t}` : ""}">${v}</span>`)
    .join("")}</div>`;
}

export const dot = (tone: Tone | "ring" = "", extra = ""): string => `<i class="dot ${tone} ${extra}"></i>`;
export const pill = (text: string, tone: Tone | "ghost" = "", tipText = ""): string =>
  `<span class="pill ${tone}"${tipText ? tip(tipText) : ""}>${esc(text)}</span>`;

export function statusItem(label: string, value: string, tone: Tone, tipText: string): string {
  return `<div class="status"${tip(tipText)}><span class="s-l">${esc(label)}</span><span class="s-v">${dot(tone)}${esc(value)}</span></div>`;
}

export interface BarMark { at: number; tone?: "warn" | "neg" | ""; label?: string }
/** horizontal micro-bar: 0..1 fill with optional threshold marks (fractions of the same scale) */
export function microBar(frac: number | null, tone: Tone, marks: BarMark[] = []): string {
  const f = frac === null ? 0 : Math.max(0, Math.min(1, frac));
  return `<div class="bar"><span class="fill ${tone}" style="width:${(f * 100).toFixed(2)}%"></span>${marks
    .map((m) => `<span class="mark ${m.tone ?? ""}" style="left:${(Math.max(0, Math.min(1, m.at)) * 100).toFixed(2)}%"${m.label ? ` title="${esc(m.label)}"` : ""}></span>`)
    .join("")}</div>`;
}

export function riskRow(o: { label: string; value: string; frac: number | null; tone: Tone; marks?: BarMark[]; sub?: string; tipText: string }): string {
  return `<div class="riskrow"${tip(o.tipText)}><span class="k">${esc(o.label)}</span><span class="v ${o.tone === "warn" || o.tone === "neg" ? `tone-${o.tone}` : ""}">${o.value}</span>${microBar(o.frac, o.tone, o.marks)}${o.sub ? `<span class="sub">${o.sub}</span>` : ""}</div>`;
}

export function table(head: string[], rows: { cells: string[]; cls?: string; cellCls?: string[] }[], keyCol = -1): string {
  return `<table class="tbl"><thead><tr>${head.map((h, i) => `<th class="${i === keyCol ? "k" : ""}">${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows
    .map((r) => `<tr class="${r.cls ?? ""}">${r.cells.map((c, i) => `<td class="${i === keyCol ? "k" : ""} ${r.cellCls?.[i] ?? ""}">${c}</td>`).join("")}</tr>`)
    .join("")}</tbody></table>`;
}

export interface ChartBand { lo: number; hi: number; label: string }
export interface ChartLine { y: number; label: string; tone: "accent" | "warn" | "muted" | "pos" | "neg" }
/** Intraday price line on the 09:15-15:30 axis, drawn as SVG (resolution-independent on any display). */
export function priceChart(series: [number, number][], w: number, h: number, o: { band?: ChartBand | null; lines?: ChartLine[]; t0: number; t1: number; nowT?: number | null }): string {
  if (series.length < 2 || w < 10) return `<svg class="chart" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}"></svg>`;
  const ys = series.map((p) => p[1]);
  const extra = [...(o.lines ?? []).map((l) => l.y), ...(o.band ? [o.band.lo, o.band.hi] : [])];
  let lo = Math.min(...ys);
  let hi = Math.max(...ys);
  for (const e of extra) if (Math.abs(e - (lo + hi) / 2) < (hi - lo) * 1.2 + 40) (lo = Math.min(lo, e)), (hi = Math.max(hi, e));
  const pad = (hi - lo) * 0.08 + 1;
  lo -= pad;
  hi += pad;
  const padR = 64;
  const X = (t: number) => ((t - o.t0) / (o.t1 - o.t0)) * (w - padR);
  const Y = (v: number) => h - 14 - ((v - lo) / (hi - lo)) * (h - 22);
  const d = series.map(([t, v], i) => `${i ? "L" : "M"}${X(t).toFixed(1)},${Y(v).toFixed(1)}`).join("");
  const last = series[series.length - 1];
  const area = `${d}L${X(last[0]).toFixed(1)},${h - 14}L${X(series[0][0]).toFixed(1)},${h - 14}Z`;
  const band = o.band
    ? `<rect x="0" y="${Y(o.band.hi).toFixed(1)}" width="${w - padR}" height="${Math.max(1, Y(o.band.lo) - Y(o.band.hi)).toFixed(1)}" class="c-band"/><text x="${w - padR + 6}" y="${(Y(o.band.hi) + 3).toFixed(1)}" class="c-lbl">${esc(o.band.label)}</text>`
    : "";
  const lines = (o.lines ?? [])
    .filter((l) => l.y > lo && l.y < hi)
    .map((l) => `<line x1="0" x2="${w - padR}" y1="${Y(l.y).toFixed(1)}" y2="${Y(l.y).toFixed(1)}" class="c-line ${l.tone}"/><text x="${w - padR + 6}" y="${(Y(l.y) + 3).toFixed(1)}" class="c-lbl ${l.tone}">${esc(l.label)}</text>`)
    .join("");
  const step = o.t1 - o.t0 <= 180 ? 30 : 60;
  const ticks: number[] = [];
  for (let t = Math.ceil((o.t0 + 1) / step) * step; t < o.t1; t += step) ticks.push(t);
  const hours = ticks
    .map((t) => `<text x="${X(t).toFixed(1)}" y="${h - 1}" class="c-ax">${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}</text>`)
    .join("");
  return `<svg class="chart" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">
    <defs><linearGradient id="c-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="var(--accent)" stop-opacity="0.10"/><stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>
    ${band}${lines}<path d="${area}" fill="url(#c-fill)"/><path d="${d}" class="c-price"/>
    <circle cx="${X(last[0]).toFixed(1)}" cy="${Y(last[1]).toFixed(1)}" r="2.6" class="c-now"/>${hours}</svg>`;
}

export const ACTOR_LABEL: Record<string, string> = {
  cio: "Tony",
  market_intel: "Market Intelligence",
  data_quality: "Data Quality",
  strategy_factory: "Strategy Factory",
  validation: "Validation",
  allocator: "Portfolio Allocator",
  risk_governor: "Risk Governor",
  execution: "Execution",
  broker: "Broker (fake)",
  post_trade: "Post-trade",
};
export const actorLabel = (a: string): string => ACTOR_LABEL[a] ?? (a.startsWith("strat:") ? a.slice(6) : a);
const TONE_DOT: Record<string, Tone> = { info: "accent", ok: "pos", warn: "warn", reject: "warn", bad: "neg", muted: "muted" };

export function activityItem(o: { when: string; actor: string; text: string; tone: string; fresh?: boolean }): string {
  return `<div class="act ${o.tone === "muted" ? "muted" : ""} ${o.fresh ? "fresh" : ""}"><span class="when">${esc(o.when)}</span>${dot(TONE_DOT[o.tone] ?? "", o.fresh ? "pulse" : "")}<div><div class="who">${esc(actorLabel(o.actor))}</div><div class="what">${esc(o.text)}</div></div></div>`;
}
