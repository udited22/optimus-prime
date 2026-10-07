// Command palette model (⌘K / Ctrl+K). The dashboard is READ-ONLY: control actions are listed so the operator
// can see they exist in the design, but they are disabled with a reason and have no handler. The only mutating
// action reachable from here is opening the two-step MANUAL_MASTER_KILL panel, which still needs arm + typed confirm.

export const READ_ONLY_REASON = "control actions not enabled in read-only mode";

export type CommandGroup = "Ask Tony" | "Navigate" | "Connectome" | "Mode" | "Controls";

export interface Command {
  id: string;
  group: CommandGroup;
  title: string;
  hint?: string;
  keywords?: string;
  /** set ⇒ shown greyed out, cannot run */
  disabled?: string;
  icon?: string;
}

export interface PaletteContext {
  questions: { id: string; text: string }[];
  replay: boolean;
  voiceOn: boolean;
  hasPosition: boolean;
  replayDays: { name: string; title: string }[];
}

export const CONTROL_ACTIONS: { id: string; title: string }[] = [
  { id: "ctl:pause-strategy", title: "Pause strategy" },
  { id: "ctl:resume-strategy", title: "Resume strategy" },
  { id: "ctl:flatten", title: "Flatten open position" },
  { id: "ctl:limits", title: "Change risk limits" },
  { id: "ctl:promote", title: "Promote / demote a strategy" },
];

export function buildCommands(ctx: PaletteContext): Command[] {
  const out: Command[] = ctx.questions.map((q) => ({ id: `ask:${q.id}`, group: "Ask Tony", title: q.text, icon: "?", hint: "answer from system state" }));
  out.push(
    { id: "nav:nifty", group: "Navigate", title: "View NIFTY", icon: "→", keywords: "market spot chart index" },
    { id: "nav:chain", group: "Navigate", title: "Show option chain", icon: "→", keywords: "strikes atm iv" },
    { id: "nav:positions", group: "Navigate", title: "Show open positions", icon: "→", keywords: "trade thesis lifecycle", hint: ctx.hasPosition ? "opens the trade thesis" : "flat right now" },
    { id: "nav:risk", group: "Navigate", title: "Show risk", icon: "→", keywords: "drawdown daily loss limits exposure" },
    { id: "nav:kills", group: "Navigate", title: "Show kill switches", icon: "→", keywords: "safeguards halt latched" },
    { id: "nav:activity", group: "Navigate", title: "Show system activity", icon: "→", keywords: "events log" },
    { id: "nav:raw", group: "Navigate", title: "Show raw logs", icon: "→", keywords: "engineer debug log" },
    { id: "nav:attention", group: "Navigate", title: "Why is the system in this attention state?", icon: "→", keywords: "normal attention intervention" },
  );
  // the cognitive connectome's operational layers (v3.1, v3.2): focus a region and open what is behind it
  for (const [r, t, k] of [
    ["PERCEPTION", "Market perception: signals and data", "regime feed ticks vix"],
    ["STRATEGY", "Strategy: active hypotheses", "ideas strategies patterns"],
    ["RISK", "Risk: constraints and rejection rationale", "governor limits rejected"],
    ["PORTFOLIO", "Portfolio: allocation", "allocator capital sizing"],
    ["EXECUTION", "Execution: broker link, protective stop and fills", "fills broker execution"],
    ["RESEARCH", "Learning / research: experiments and candidates", "validation post-trade research"],
  ] as const)
    out.push({ id: `layer:${r}`, group: "Connectome", title: `Focus ${t}`, icon: "◎", keywords: `${k} connectome neural region zoom` });
  if (ctx.replay) out.push({ id: "mode:stream", group: "Mode", title: "Back to the stream", icon: "◉", keywords: "live simulation" });
  else out.push({ id: "mode:replay", group: "Mode", title: "Enter replay", icon: "↺", keywords: "history recorded day" });
  for (const d of ctx.replayDays) out.push({ id: `mode:replay:${d.name}`, group: "Mode", title: `Replay ${d.title}`, icon: "↺", keywords: d.name });
  out.push({ id: "mode:voice", group: "Mode", title: ctx.voiceOn ? "Turn Tony's voiceover off" : "Turn Tony's voiceover on", icon: "♪", keywords: "speech audio speak" });
  out.push({ id: "kill:open", group: "Controls", title: "Master kill switch…", icon: "⏻", hint: "two-step: arm, then type MANUAL_MASTER_KILL", keywords: "emergency stop halt" });
  for (const c of CONTROL_ACTIONS) out.push({ ...c, group: "Controls", icon: "·", disabled: READ_ONLY_REASON });
  return out;
}

const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
const STOP = new Set(["the", "a", "an", "is", "are", "we", "our", "what", "why", "did", "do", "to", "of", "me", "show", "currently"]);

/** score: every query word must prefix-match a word of the title/keywords; stop-words are optional */
export function score(c: Command, query: string): number {
  const q = norm(query);
  if (!q) return 1;
  const hay = norm(`${c.title} ${c.keywords ?? ""} ${c.group}`).split(" ");
  let s = 0;
  for (const w of q.split(" ")) {
    const hit = hay.some((h) => h.startsWith(w) || (w.length > 3 && h.startsWith(w.slice(0, -1))));
    if (hit) s += STOP.has(w) ? 0.25 : 1;
    else if (!STOP.has(w)) return 0;
  }
  if (norm(c.title).startsWith(q)) s += 2;
  return s;
}

export function filterCommands(cmds: Command[], query: string): Command[] {
  const scored = cmds.map((c, i) => ({ c, i, s: score(c, query) })).filter((x) => x.s > 0);
  if (query.trim()) scored.sort((a, b) => b.s - a.s || a.i - b.i);
  return scored.map((x) => x.c);
}
