// Self-hosted variable fonts (bundled by Vite into dist/assets; no CDN request at runtime).
import "@fontsource-variable/inter/index.css";
import "@fontsource-variable/jetbrains-mono/index.css";
import "./design/tokens.css";
import "./design/components.css";
import "./design/layout.css";
import { api } from "./api";
import { istTime } from "./fmt";
import { mountHud, renderHud } from "./hud";
import { type Hero, createHero } from "./connectome/hero";
import { REGIONS, type RegionId } from "./connectome/regions";
import { type DisplayMode, displayMode } from "./model/mode";
import { type Command, buildCommands } from "./model/palette";
import type { Ctx } from "./screens/ctx";
import { answerHTML, attentionHTML, strategyHTML, thesisHTML } from "./screens/drawers";
import { layerHTML, layerTitle } from "./screens/layers";
import { DashState } from "./state";
import type { DashEvent } from "./types";
import { $ } from "./ui/dom";
import { Drawer } from "./ui/drawer";
import { Palette } from "./ui/palette";
import { installTooltips } from "./ui/tooltip";
import { stepTweens } from "./ui/tween";
import { Voice } from "./voice";

const qs = new URLSearchParams(location.search);
const view = qs.get("view") ?? "full"; // full | graph

const state = new DashState();
const voice = new Voice();
let hero: Hero;
let drawer: Drawer;
let palette: Palette;
let mode: "live" | "replay" = "live";
let feed: Ctx["feed"] = "connecting";
let es: EventSource | null = null;
let serverLabel: string | null = null;
let replayDays: { name: string; day: string; title: string }[] = [];
const ui = { rawLogs: false, chainOpen: false, killsOpen: false };

function currentMode(): DisplayMode {
  return displayMode({ replay: mode === "replay", serverLabel, simulatedSeen: state.simulatedSeen, realSeen: state.realSeen });
}

function simNow(): number {
  if (mode === "replay" && player) return player.simT;
  return state.lastTs ? Date.parse(state.lastTs) : Date.now();
}

/** fold one event; `live` = it is happening now (pulse the graph, let the voice speak) */
function feedEvent(e: DashEvent, live: boolean, quiet = false): void {
  state.apply(e);
  if (e.kind === "STRATEGIES") syncStages();
  if (e.kind === "TONY") voice.observe(e.data, quiet || !live);
  // the hero maps the event to activations itself (connectome/mapping.ts); folded history only updates its state
  hero.event(e, live, quiet);
}

function syncStages(): void {
  const stages: Record<string, string> = {};
  for (const s of state.strategies?.rows ?? []) stages[`strat:${s.id}`] = s.stage;
  hero.setStages(stages);
}

// ---------------------------------------------------------------- stream
async function startLive(): Promise<void> {
  stopReplay();
  mode = "live";
  document.body.dataset.mode = "stream";
  state.reset();
  hero.reset();
  const snap = await api.snapshot();
  serverLabel = snap.label ?? serverLabel;
  // the day so far (chart, decisions, fills, activity) + the latest of every kind, folded once in order
  const bySeq = new Map<number, DashEvent>();
  for (const e of [...(snap.session ?? []), ...(Object.values(snap.latest).filter(Boolean) as DashEvent[]), ...snap.log]) bySeq.set(e.seq, e);
  for (const e of [...bySeq.values()].sort((a, b) => a.seq - b.seq)) feedEvent(e, false);
  voice.prime(state.tony);
  syncStages();
  es?.close();
  feed = "connecting";
  es = api.stream(
    snap.last_seq,
    (e) => mode === "live" && feedEvent(e, true),
    (s) => (feed = s === "open" ? "open" : "error"),
  );
}

// ---------------------------------------------------------------- replay
interface Player { name: string; events: DashEvent[]; times: number[]; idx: number; simT: number; playing: boolean; speed: number }
let player: Player | null = null;
const STEP_MS = 15_000;

function stopReplay(): void {
  if (player) player.playing = false;
}

async function startReplay(name?: string): Promise<void> {
  es?.close();
  es = null;
  mode = "replay";
  feed = "replay";
  document.body.dataset.mode = "replay";
  if (!replayDays.length) replayDays = (await api.replayList()).days;
  const sp = qs.get("speed");
  if (sp) $<HTMLSelectElement>("rb-speed").value = sp;
  const sel = $<HTMLSelectElement>("rb-day");
  if (!sel.options.length) sel.innerHTML = replayDays.map((d) => `<option value="${d.name}">${d.title.split(" · ")[0]} · ${d.name}</option>`).join("");
  const pick = name ?? (sel.value || replayDays[0].name);
  sel.value = pick;
  const day = await api.replayDay(pick);
  const times = day.events.map((e) => Date.parse(e.ts));
  player = { name: pick, events: day.events, times, idx: 0, simT: times[0] - 1, playing: false, speed: Number($<HTMLSelectElement>("rb-speed").value) };
  drawMarks();
  seek(times[0]);
  const at = qs.get("at"); // HH:MM or HH:MM:SS, IST
  if (at && /^\d\d:\d\d(:\d\d)?$/.test(at) && name === (qs.get("day") ?? undefined)) {
    const target = Date.parse(`${day.events[0].ts.slice(0, 10)}T${at.length === 5 ? at + ":00" : at}+05:30`);
    if (Number.isFinite(target)) seek(target);
  }
  if (qs.get("autoplay") !== "0") setPlaying(true);
}

function seek(t: number): void {
  if (!player) return;
  const p = player;
  state.reset();
  hero.reset();
  p.idx = 0;
  while (p.idx < p.events.length && p.times[p.idx] <= t) feedEvent(p.events[p.idx++], false);
  p.simT = t;
  voice.prime(state.tony);
  syncStages();
  updateScrub();
}

function advance(dtMs: number): void {
  if (!player) return;
  const p = player;
  p.simT += dtMs;
  const fast = p.speed > 300;
  while (p.idx < p.events.length && p.times[p.idx] <= p.simT) feedEvent(p.events[p.idx++], true, fast);
  if (p.idx >= p.events.length) setPlaying(false);
  updateScrub();
}

function setPlaying(on: boolean): void {
  if (!player) return;
  player.playing = on;
  $("rb-play").textContent = on ? "❚❚" : "▶";
}

function updateScrub(): void {
  if (!player) return;
  const p = player;
  const t0 = p.times[0];
  const t1 = p.times[p.times.length - 1];
  $<HTMLInputElement>("rb-scrub").value = String(Math.round(((p.simT - t0) / (t1 - t0)) * 1000));
  $("rb-time").textContent = `${istTime(new Date(Math.max(t0, p.simT)).toISOString())} IST`;
  state.dirty = true;
}

const isKill = (e: DashEvent) => e.kind === "KILLS";
const JUMPS: Record<string, (e: DashEvent) => boolean> = {
  any: (e) => ["INTENT", "DECISION", "FILL", "KILLS", "DAY_END"].includes(e.kind),
  dec: (e) => e.kind === "DECISION",
  fill: (e) => e.kind === "FILL",
  kill: isKill,
};

function jumpNext(which: keyof typeof JUMPS): void {
  if (!player) return;
  const p = player;
  setPlaying(false);
  for (let i = p.idx; i < p.events.length; i++) {
    if (JUMPS[which](p.events[i]) && p.times[i] > p.simT) {
      advance(p.times[i] - p.simT);
      return;
    }
  }
  toast(`No later ${which === "dec" ? "decision" : which === "any" ? "event" : which} in this recorded day`);
}

/** event markers on the scrubber: decisions (amber if rejected), fills (green), kills (red) */
function drawMarks(): void {
  if (!player) return;
  const p = player;
  const t0 = p.times[0];
  const t1 = p.times[p.times.length - 1];
  const html = p.events
    .map((e, i) => {
      let cls = "";
      if (e.kind === "DECISION") cls = String(e.data.verdict).startsWith("APPROVE") ? "m-ok" : "m-rej";
      else if (e.kind === "FILL") cls = "m-fill";
      else if (e.kind === "KILLS" && e.data.switches?.some((s: any) => s.latched)) cls = "m-kill";
      return cls ? `<i class="${cls}" style="left:${(((p.times[i] - t0) / (t1 - t0)) * 100).toFixed(2)}%" title="${istTime(e.ts)} ${e.kind}"></i>` : "";
    })
    .join("");
  $("rb-marks").innerHTML = html;
}

// ---------------------------------------------------------------- kill (the only control: arm, then typed confirm)
let armed: { nonce: string; until: number } | null = null;
function wireKill(): void {
  const pop = $("kill-pop");
  const input = $<HTMLInputElement>("kill-input");
  const go = $<HTMLButtonElement>("kill-go");
  const arm = $<HTMLButtonElement>("kill-arm");
  const err = $("kill-err");
  const reset = () => {
    armed = null;
    input.value = "";
    input.disabled = true;
    go.disabled = true;
    arm.disabled = false;
    err.textContent = "";
  };
  const close = () => {
    reset();
    pop.classList.add("hidden");
    $("kill-btn").setAttribute("aria-expanded", "false");
  };
  $("kill-btn").onclick = () => {
    const opening = pop.classList.contains("hidden");
    if (!opening) return close();
    reset();
    pop.classList.remove("hidden");
    $("kill-btn").setAttribute("aria-expanded", "true");
    arm.focus();
  };
  arm.onclick = async () => {
    err.textContent = "";
    try {
      const a = await api.killArm();
      armed = { nonce: a.nonce, until: Date.now() + a.expires_in_s * 1000 };
      arm.disabled = true;
      input.disabled = false;
      input.focus();
    } catch (e) {
      err.textContent = String(e instanceof Error ? e.message : e);
    }
  };
  input.oninput = () => (go.disabled = input.value !== "MANUAL_MASTER_KILL" || !armed);
  $("kill-cancel").onclick = close;
  go.onclick = async () => {
    if (!armed) return;
    try {
      const r = await api.killConfirm(armed.nonce, input.value, "owner pressed MANUAL_MASTER_KILL on the dashboard");
      close();
      toast(r.already_latched ? "MANUAL_MASTER_KILL was already latched (SIMULATED kernel)" : "MANUAL_MASTER_KILL LATCHED in the SIMULATED kernel");
      if (mode !== "live") void startLive();
    } catch (e) {
      err.textContent = String(e instanceof Error ? e.message : e);
      armed = null;
      go.disabled = true;
      arm.disabled = false;
      input.disabled = true;
    }
  };
  setInterval(() => {
    if (!armed) return ($("kill-ttl").textContent = "");
    const left = Math.max(0, Math.round((armed.until - Date.now()) / 1000));
    $("kill-ttl").textContent = `Prepared · expires in ${left}s`;
    if (left <= 0) {
      reset();
      err.textContent = "The preparation expired: prepare again.";
    }
  }, 250);
}
const openKill = (): void => {
  if ($("kill-pop").classList.contains("hidden")) $("kill-btn").click();
};

function toast(msg: string): void {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  setTimeout(() => t.classList.add("hidden"), 5000);
}

// ---------------------------------------------------------------- drawers + palette
function ask(qid: string): void {
  const q = state.tony?.questions?.find((x: any) => x.id === qid)?.text ?? "Ask Tony";
  drawer.open(`ask:${qid}`, "Tony · answer", q, () => answerHTML(state, qid));
}
const openThesis = () => drawer.open("thesis", "Open position · thesis & lifecycle", state.position?.open ? String(state.position.symbol) : "No open position", () => thesisHTML(state));
const openAttention = () => {
  const lvl = state.tony?.attention?.level ?? "UNKNOWN";
  drawer.open("attention", "Attention system", lvl === "INTERVENTION" ? "Intervention required" : lvl === "ATTENTION" ? "Attention" : lvl === "NORMAL" ? "Normal" : "Unknown", () => attentionHTML(state));
};
const openStrategy = (id: string) => drawer.open(`strat:${id}`, "Strategy", id, () => strategyHTML(state, id));
/** click a region of the cognitive connectome: zoom into it and open its operational layer */
function openLayer(r: RegionId): void {
  const t = layerTitle(r);
  drawer.open(`layer:${r}`, t.eyebrow, t.title, () => layerHTML(state, r));
  hero.focus(r);
}

function focusRegion(id: string): void {
  const el = $(id);
  el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
}

function runCommand(c: Command): void {
  if (c.disabled) return; // read-only: control actions have no handler at all
  const [kind, rest] = [c.id.split(":")[0], c.id.slice(c.id.indexOf(":") + 1)];
  if (kind === "ask") return ask(rest);
  if (c.id === "nav:nifty") return focusRegion("r-market");
  if (c.id === "nav:chain") return (ui.chainOpen = true), (state.dirty = true), focusRegion("r-chain");
  if (c.id === "nav:positions") return state.position?.open ? openThesis() : focusRegion("r-position");
  if (c.id === "nav:risk") return focusRegion("r-risk");
  if (c.id === "nav:kills") return (ui.killsOpen = true), (state.dirty = true), focusRegion("r-kills");
  if (c.id === "nav:activity") return setRaw(false), focusRegion("r-activity");
  if (c.id === "nav:raw") return setRaw(true), focusRegion("r-activity");
  if (c.id === "nav:attention") return openAttention();
  if (kind === "layer" && (REGIONS as string[]).includes(rest)) return openLayer(rest as RegionId);
  if (c.id === "mode:stream") return void startLive();
  if (c.id === "mode:replay") return void startReplay();
  if (c.id.startsWith("mode:replay:")) return void startReplay(c.id.slice("mode:replay:".length));
  if (c.id === "mode:voice") return toggleVoice();
  if (c.id === "kill:open") return openKill();
}

function setRaw(on: boolean): void {
  ui.rawLogs = on;
  for (const b of $("act-seg").querySelectorAll<HTMLButtonElement>("button")) b.classList.toggle("on", (b.dataset.v === "raw") === on);
  state.dirty = true;
}

function toggleVoice(): void {
  voice.set(!voice.on);
  toast(voice.supported ? `Tony's voiceover ${voice.on ? "on" : "off"}` : "This browser has no speech synthesis");
  state.dirty = true;
}

function commands(): Command[] {
  return buildCommands({
    questions: state.tony?.questions ?? [],
    replay: mode === "replay",
    voiceOn: voice.on,
    hasPosition: !!state.position?.open,
    replayDays: replayDays.map((d) => ({ name: d.name, title: d.title.split(" · ").slice(0, 2).join(" · ") })),
  });
}

function wireKeys(): void {
  document.addEventListener("keydown", (e) => {
    const typing = e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      return palette.isOpen ? palette.close() : palette.open();
    }
    if (e.key === "Escape") {
      if (drawer.isOpen) drawer.close();
      if (!$("kill-pop").classList.contains("hidden")) $("kill-cancel").click();
      return;
    }
    if (typing || palette.isOpen) return;
    if (e.key === "/") return e.preventDefault(), palette.open();
    if (mode === "replay" && player) {
      if (e.key === " ") return e.preventDefault(), setPlaying(!player.playing);
      if (e.key === "ArrowRight") return setPlaying(false), advance(STEP_MS);
      if (e.key === "ArrowLeft") return setPlaying(false), seek(player.simT - STEP_MS);
      if (e.key === "n") return jumpNext("any");
    }
  });
}

// ---------------------------------------------------------------- boot
async function boot(): Promise<void> {
  document.body.dataset.view = view;
  mountHud($("hud"));
  installTooltips(document.body);
  drawer = new Drawer(document.body);
  palette = new Palette(document.body, commands, runCommand);
  const topo = await api.topology();
  hero = createHero($("graph"), topo, topo.strategies.map((s) => s.id), $("r-brief"), qs.get("hero"), qs.get("labels") !== "off");
  document.body.dataset.hero = hero.kind;
  hero.onRegionClick = openLayer;
  drawer.onClose = (k) => k?.startsWith("layer:") && hero.focus(null);
  wireKill();
  wireKeys();
  $("m-live").onclick = () => {
    if (mode !== "live") void startLive();
  };
  $("m-replay").onclick = () => {
    if (mode !== "replay") void startReplay();
  };
  $("ask-btn").onclick = () => palette.open();
  $("voice-btn").onclick = toggleVoice;
  $("att-chip").onclick = openAttention;
  $("b-alert").onclick = openAttention;
  $("rb-day").onchange = (e) => void startReplay((e.target as HTMLSelectElement).value);
  $("rb-play").onclick = () => setPlaying(!player?.playing);
  $("rb-step").onclick = () => (setPlaying(false), advance(STEP_MS));
  $("rb-back").onclick = () => player && (setPlaying(false), seek(player.simT - STEP_MS));
  $("rb-restart").onclick = () => player && seek(player.times[0]);
  $("rb-next").onclick = () => jumpNext("any");
  $("rb-next-dec").onclick = () => jumpNext("dec");
  $("rb-next-fill").onclick = () => jumpNext("fill");
  $("rb-next-kill").onclick = () => jumpNext("kill");
  $("rb-speed").onchange = (e) => player && (player.speed = Number((e.target as HTMLSelectElement).value));
  $("rb-scrub").oninput = (e) => {
    if (!player) return;
    const f = Number((e.target as HTMLInputElement).value) / 1000;
    const t0 = player.times[0];
    seek(t0 + f * (player.times[player.times.length - 1] - t0));
  };
  $("chain-toggle").onclick = () => ((ui.chainOpen = !ui.chainOpen), ($("chain-toggle").textContent = ui.chainOpen ? "Hide strikes" : "Show strikes"), (state.dirty = true));
  $("kills-toggle").onclick = () => ((ui.killsOpen = !ui.killsOpen), ($("kills-toggle").textContent = ui.killsOpen ? "Hide" : "Details"), (state.dirty = true));
  $("act-seg").onclick = (e) => {
    const b = (e.target as Element).closest<HTMLButtonElement>("button");
    if (b) setRaw(b.dataset.v === "raw");
  };
  document.addEventListener("click", (e) => {
    const t = e.target as Element;
    if (t.closest("#pos-card")) openThesis();
    const sc = t.closest<HTMLElement>("[data-strategy]");
    if (sc?.dataset.strategy) openStrategy(sc.dataset.strategy);
    if (!t.closest("#kill-wrap") && !$("kill-pop").classList.contains("hidden") && !armed) $("kill-cancel").click();
  });

  let last = performance.now();
  let lastRender = 0;
  const loop = (now: number) => {
    const dt = now - last;
    last = now;
    if (mode === "replay" && player?.playing) advance(dt * player.speed);
    if (state.dirty && now - lastRender > 90) {
      state.dirty = false;
      lastRender = now;
      const ctx: Ctx = { st: state, simNow: simNow(), replay: mode === "replay", mode: currentMode(), feed, ...ui, voiceOn: voice.on, voiceSupported: voice.supported };
      renderHud(ctx);
      hero.sync(state, ctx.simNow);
      $("r-graph").style.setProperty("--occ", `${$("r-brief").offsetHeight + 24}px`);
      drawer.update();
    }
    hero.frame(now);
    stepTweens(now);
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
  setInterval(() => (state.dirty = true), 1000); // feed age and countdowns move even without events
  const cfg = await api.config();
  serverLabel = (cfg.label as string) ?? null;
  const startMode = qs.get("mode") ?? (cfg.mode as string);
  if (startMode === "replay") await startReplay(qs.get("day") ?? undefined);
  else await startLive();
  // deep links used by the screenshot script: ?open=palette[:question] | thesis | attention | strategy:<id>
  const open = qs.get("open");
  if (open) {
    await new Promise((r) => setTimeout(r, 400));
    if (open === "palette") palette.open(qs.get("q") ?? "");
    else if (open.startsWith("ask:")) {
      palette.open(qs.get("q") ?? "");
      ask(open.slice(4));
    } else if (open === "thesis") openThesis();
    else if (open === "attention") openAttention();
    else if (open.startsWith("strategy:")) openStrategy(open.slice(9));
    else if (open.startsWith("layer:") && (REGIONS as string[]).includes(open.slice(6))) openLayer(open.slice(6) as RegionId);
  }
  // ?hover=RISK or ?hover=STRATEGY:0 shows a hover label without a pointer (screenshots)
  const hv = qs.get("hover");
  if (hv && hero.kind === "connectome") {
    const [r, sub] = hv.split(":");
    if ((REGIONS as string[]).includes(r)) (hero as unknown as { hoverRegion(r: RegionId, s: number | null): void }).hoverRegion(r as RegionId, sub === undefined ? null : Number(sub));
  }
  if (qs.get("perf") === "1") (window as unknown as { __hero: Hero }).__hero = hero;
  document.body.classList.add("ready");
}

boot().catch((e) => {
  document.body.innerHTML = `<pre style="color:#f2626b;padding:2em;font-family:ui-monospace,monospace">dashboard failed to start: ${String(e)}</pre>`;
});
