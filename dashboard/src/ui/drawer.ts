// Contextual drawer: answers, the trade thesis, attention reasons and strategy detail open here, beside the
// screen, never over all of it. Esc or the scrim closes it.
import { esc } from "./dom";

export class Drawer {
  private el: HTMLElement;
  private scrim: HTMLElement;
  private body: HTMLElement;
  private title: HTMLElement;
  private eyebrow: HTMLElement;
  key: string | null = null;
  /** called with the closing key (the connectome un-focuses when its layer closes) */
  onClose: ((key: string | null) => void) | null = null;
  private refresh: (() => string) | null = null;

  constructor(root: HTMLElement) {
    this.scrim = document.createElement("div");
    this.scrim.className = "scrim";
    this.el = document.createElement("aside");
    this.el.className = "drawer";
    this.el.setAttribute("role", "dialog");
    this.el.setAttribute("aria-modal", "false");
    this.el.innerHTML = `<div class="drawer-h"><div><div class="eyebrow" data-r="eb"></div><h2 data-r="t"></h2></div><button class="btn ghost icon" data-r="x" aria-label="Close (Esc)" title="Close (Esc)">✕</button></div><div class="drawer-b" data-r="b"></div>`;
    root.append(this.scrim, this.el);
    this.body = this.el.querySelector('[data-r="b"]')!;
    this.title = this.el.querySelector('[data-r="t"]')!;
    this.eyebrow = this.el.querySelector('[data-r="eb"]')!;
    this.el.querySelector<HTMLElement>('[data-r="x"]')!.onclick = () => this.close();
    this.scrim.onclick = () => this.close();
  }

  get isOpen(): boolean {
    return this.el.classList.contains("on");
  }

  /** `render` is re-run on every state change while the drawer is open, so its content stays current. */
  open(key: string, eyebrow: string, title: string, render: () => string): void {
    if (this.key !== null && this.key !== key) this.onClose?.(this.key);
    this.key = key;
    this.refresh = render;
    this.eyebrow.textContent = eyebrow;
    this.title.innerHTML = esc(title);
    this.body.innerHTML = render();
    this.body.scrollTop = 0;
    this.el.classList.add("on");
    this.scrim.classList.add("on");
  }

  update(): void {
    if (!this.isOpen || !this.refresh) return;
    const html = this.refresh();
    if (this.body.innerHTML !== html) this.body.innerHTML = html;
  }

  close(): void {
    const k = this.key;
    this.key = null;
    if (k !== null) this.onClose?.(k);
    this.refresh = null;
    this.el.classList.remove("on");
    this.scrim.classList.remove("on");
  }
}
