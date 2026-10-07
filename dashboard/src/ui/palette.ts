// ⌘K / Ctrl+K command palette view. Keyboard-first: type, ↑/↓, Enter, Esc. Disabled commands show their reason.
import { type Command, filterCommands } from "../model/palette";
import { esc } from "./dom";

export class Palette {
  private el: HTMLElement;
  private input: HTMLInputElement;
  private list: HTMLElement;
  private items: Command[] = [];
  private sel = 0;
  private scrim: HTMLElement;

  constructor(root: HTMLElement, private source: () => Command[], private run: (c: Command) => void) {
    this.scrim = document.createElement("div");
    this.scrim.className = "scrim";
    this.el = document.createElement("div");
    this.el.className = "palette";
    this.el.setAttribute("role", "dialog");
    this.el.innerHTML = `<input type="text" placeholder="Ask Tony, or jump to…" aria-label="Ask Tony or run a command" spellcheck="false" autocomplete="off"/><div class="pal-list" role="listbox"></div><div class="pal-foot"><span><kbd>↑</kbd> <kbd>↓</kbd> select · <kbd>↵</kbd> run · <kbd>esc</kbd> close</span><span>Tony answers from system state only · read-only</span></div>`;
    root.append(this.scrim, this.el);
    this.input = this.el.querySelector("input")!;
    this.list = this.el.querySelector(".pal-list")!;
    this.scrim.onclick = () => this.close();
    this.input.oninput = () => this.render(true);
    this.input.onkeydown = (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : -1;
        this.sel = (this.sel + step + this.items.length) % Math.max(1, this.items.length);
        this.render(false);
      } else if (e.key === "Enter") {
        e.preventDefault();
        const c = this.items[this.sel];
        if (c) this.choose(c);
      } else if (e.key === "Escape") {
        e.preventDefault();
        this.close();
      }
    };
    this.list.onclick = (e) => {
      const row = (e.target as Element).closest<HTMLElement>("[data-i]");
      if (row) this.choose(this.items[Number(row.dataset.i)]);
    };
  }

  get isOpen(): boolean {
    return this.el.classList.contains("on");
  }

  open(query = ""): void {
    this.input.value = query;
    this.el.classList.add("on");
    this.scrim.classList.add("on");
    this.render(true);
    requestAnimationFrame(() => this.input.focus());
  }

  close(): void {
    this.el.classList.remove("on");
    this.scrim.classList.remove("on");
    this.input.blur();
  }

  private choose(c: Command): void {
    if (c.disabled) {
      this.input.focus();
      return;
    }
    this.close();
    this.run(c);
  }

  private render(reset: boolean): void {
    const q = this.input.value;
    this.items = filterCommands(this.source(), q);
    if (reset) this.sel = Math.max(0, this.items.findIndex((c) => !c.disabled));
    let group = "";
    const html: string[] = [];
    this.items.forEach((c, i) => {
      if (c.group !== group && !q.trim()) {
        group = c.group;
        html.push(`<div class="pal-group eyebrow">${esc(group)}</div>`);
      }
      html.push(
        `<div class="pal-item ${i === this.sel ? "sel" : ""} ${c.disabled ? "disabled" : ""}" data-i="${i}" role="option" aria-disabled="${c.disabled ? "true" : "false"}"><span class="ic">${esc(c.icon ?? "·")}</span><span>${esc(c.title)}</span><span class="hint">${esc(c.disabled ?? c.hint ?? "")}</span></div>`,
      );
    });
    if (!this.items.length)
      html.push(`<div class="pal-item disabled"><span class="ic">?</span><span>Tony answers only the listed questions: answers are built from system state by rules, with no language model.</span><span></span></div>`);
    this.list.innerHTML = html.join("");
    this.list.querySelector(".sel")?.scrollIntoView({ block: "nearest" });
  }
}
