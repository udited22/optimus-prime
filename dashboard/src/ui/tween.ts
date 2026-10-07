// Numbers ease to their new value (~220 ms) instead of jumping; the final text is always the exact value.
interface Tw { from: number; to: number; t0: number; fmt: (v: number) => string }
const active = new Map<HTMLElement, Tw>();
const DUR = 220;
const reduce = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

export function tweenNumber(el: HTMLElement | null, value: number | null, fmt: (v: number) => string, empty = "—"): void {
  if (!el) return;
  if (value === null || !Number.isFinite(value)) {
    active.delete(el);
    el.textContent = empty;
    delete el.dataset.v;
    return;
  }
  const prev = el.dataset.v === undefined ? null : Number(el.dataset.v);
  el.dataset.v = String(value);
  if (prev === null || reduce || prev === value || Math.abs(value - prev) > Math.abs(prev) * 0.25 + 1e6) {
    active.delete(el);
    el.textContent = fmt(value);
    return;
  }
  const cur = active.get(el);
  const from = cur ? cur.from + (cur.to - cur.from) * Math.min(1, (performance.now() - cur.t0) / DUR) : prev;
  active.set(el, { from, to: value, t0: performance.now(), fmt });
}

export function stepTweens(now: number): void {
  for (const [el, t] of active) {
    const k = Math.min(1, (now - t.t0) / DUR);
    const e = 1 - Math.pow(1 - k, 3);
    el.textContent = t.fmt(k >= 1 ? t.to : t.from + (t.to - t.from) * e);
    if (k >= 1) active.delete(el);
  }
}
