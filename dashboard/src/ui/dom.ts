// Tiny DOM helpers shared by every component.
export const $ = <T extends HTMLElement = HTMLElement>(id: string): T => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing #${id}`);
  return el as T;
};

const last = new WeakMap<Element, string>();
/** Replace innerHTML only when the markup changed, so hover state, focus and CSS transitions survive renders. */
export function setHTML(el: Element, html: string): boolean {
  if (last.get(el) === html) return false;
  last.set(el, html);
  el.innerHTML = html;
  return true;
}
export function setText(el: Element, text: string): void {
  if (el.textContent !== text) el.textContent = text;
}
export const esc = (s: unknown): string =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
/** attribute-safe tooltip */
export const tip = (s: string): string => ` data-tip="${esc(s)}"`;
