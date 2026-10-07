// One floating tooltip for every [data-tip] element (quantitative metrics explain their basis on hover/focus).
export function installTooltips(root: HTMLElement): void {
  const t = document.createElement("div");
  t.className = "tip";
  t.setAttribute("role", "tooltip");
  root.append(t);
  let cur: HTMLElement | null = null;
  let timer = 0;
  const show = (el: HTMLElement) => {
    const text = el.dataset.tip;
    if (!text) return;
    cur = el;
    t.textContent = text;
    const r = el.getBoundingClientRect();
    const tw = Math.min(352, t.offsetWidth || 300);
    const left = Math.max(8, Math.min(window.innerWidth - tw - 8, r.left + r.width / 2 - tw / 2));
    const below = r.bottom + 8 + 80 < window.innerHeight;
    t.style.left = `${left}px`;
    t.style.top = below ? `${r.bottom + 8}px` : "";
    t.style.bottom = below ? "" : `${window.innerHeight - r.top + 8}px`;
    t.classList.add("on");
  };
  const hide = () => {
    cur = null;
    window.clearTimeout(timer);
    t.classList.remove("on");
  };
  const target = (e: Event) => (e.target instanceof Element ? (e.target.closest("[data-tip]") as HTMLElement | null) : null);
  document.addEventListener("pointerover", (e) => {
    const el = target(e);
    if (el === cur) return;
    hide();
    if (el) timer = window.setTimeout(() => show(el), 280);
  });
  document.addEventListener("focusin", (e) => {
    const el = target(e);
    if (el) show(el);
  });
  document.addEventListener("focusout", hide);
  document.addEventListener("scroll", hide, true);
}
