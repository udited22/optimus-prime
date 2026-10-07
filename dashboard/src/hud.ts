// Screen composition for the v3 command centre (docs/architecture/observability.md §17.5): command bar, replay timeline, and the four
// zones (market context · decision graph + NOW/WHY/NEXT · strategy pipeline + System Activity · capital & risk).
// Pure rendering from DashState. The dashboard is READ-ONLY: its one control is MANUAL_MASTER_KILL (arm, then a
// typed confirmation), which lives behind the subtle ARMED item at the far right of the command bar.
//
// Simulation honesty: there are no per-card SIMULATED badges any more. ONE global mode indicator in the command
// bar says SIMULATION (or REPLAY) with the simulated date and time, and model/mode.ts makes LIVE impossible while
// any event is SIMULATED. Tooltips and drawers still say SIMULATED wherever a number could be mistaken for real.
import { modeText } from "./model/mode";
import { capitalSkeleton, renderCapital } from "./screens/capital";
import { centreSkeleton, renderActivity, renderBrief, renderPipeline } from "./screens/centre";
import { type Ctx, PHASE_LABEL } from "./screens/ctx";
import { marketSkeleton, renderMarket } from "./screens/market";
import { statusItem } from "./ui/components";
import { $, esc, setHTML, setText, tip } from "./ui/dom";
import { tweenNumber } from "./ui/tween";
import { inr, num, signed } from "./fmt";

const MODE_TIP = {
  SIMULATION: "SIMULATION: every number on this screen is SIMULATED (synthetic prices, a fake broker, simulated lifecycle stages). No real broker is connected and no real order can be placed.",
  REPLAY: "REPLAY: a recorded SIMULATED day, played back. Nothing on screen is happening now.",
  LIVE: "LIVE",
};

export function mountHud(root: HTMLElement): void {
  root.innerHTML = `
  <header class="cmdbar" id="cmdbar">
    <div class="brand"${tip("Optimus Prime · Trading Intelligence System (docs/architecture/observability.md). Read-only; SIMULATED.")}><svg viewBox="0 0 20 20" class="mark" aria-hidden="true"><circle cx="10" cy="10" r="8.5"/><circle cx="10" cy="10" r="2.6"/></svg><span class="b1">OPTIMUS PRIME</span><span class="b2">Trading Intelligence System</span></div>
    <div class="mode-ind" id="mode-ind"><i class="dot"></i><span id="mode-text" class="num">SIMULATION</span></div>
    <div class="statuses" id="statuses"></div>
    <div class="grow"></div>
    <button class="att-chip" id="att-chip" aria-label="Attention state"></button>
    <button class="ask" id="ask-btn"${tip("Ask Tony (⌘K / Ctrl+K). Answers come from system state by rules; no language model.")}><kbd>⌘K</kbd><span>Ask Tony…</span></button>
    <button class="btn ghost icon voice" id="voice-btn" aria-pressed="false"></button>
    <div class="seg" id="mode-seg"${tip("Stream: the running SIMULATED session. Replay: a recorded SIMULATED day.")}><button id="m-live">Stream</button><button id="m-replay">Replay</button></div>
    <div class="kill-wrap" id="kill-wrap">
      <button class="kill-btn" id="kill-btn" aria-haspopup="dialog" aria-expanded="false"${tip("Master kill switch: ready (armed). Opens the emergency controls.")}><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5 13.5 3.6v4.1c0 3.2-2.3 5.6-5.5 6.8-3.2-1.2-5.5-3.6-5.5-6.8V3.6Z"/></svg><span id="kill-lbl">ARMED</span></button>
      <div class="kill-pop hidden" id="kill-pop" role="dialog" aria-label="MANUAL_MASTER_KILL">
        <div class="eyebrow">Emergency control</div>
        <h3>Master kill switch</h3>
        <p>Latches <b>MANUAL_MASTER_KILL</b> in the SIMULATED kernel: working orders are cancelled, open positions flattened and new entries blocked. It stays latched until an owner reset, including across restarts.</p>
        <ol class="kill-steps">
          <li><span class="ks-n">1</span><div><div>Prepare</div><div class="t3 small">gets a one-time nonce, valid 30 s</div></div><button class="btn" id="kill-arm">Prepare kill</button></li>
          <li><span class="ks-n">2</span><div class="ks-in"><label for="kill-input">Type <code>MANUAL_MASTER_KILL</code></label><input id="kill-input" autocomplete="off" spellcheck="false" disabled/></div></li>
        </ol>
        <div class="kill-row"><span id="kill-ttl" class="small t3"></span><span id="kill-err" class="small tone-neg"></span></div>
        <div class="kill-row"><button class="btn ghost" id="kill-cancel">Cancel</button><button class="btn danger" id="kill-go" disabled>Execute kill</button></div>
        <p class="t3 tiny">The dashboard has no other control. It cannot place, modify or cancel an order.</p>
      </div>
    </div>
  </header>
  <div class="replaybar" id="replaybar">
    <select class="sel" id="rb-day" aria-label="Recorded day"></select>
    <div class="rb-btns">
      <button class="btn ghost icon" id="rb-restart"${tip("Restart the day")}>⏮</button>
      <button class="btn ghost" id="rb-back"${tip("Back 15 s")}>−15s</button>
      <button class="btn icon" id="rb-play"${tip("Play / pause (space)")}>▶</button>
      <button class="btn ghost" id="rb-step"${tip("Forward 15 s")}>+15s</button>
    </div>
    <div class="rb-btns"><span class="t3 small">Next</span>
      <button class="btn ghost" id="rb-next-dec"${tip("Jump to the next Risk Governor decision")}>Decision</button>
      <button class="btn ghost" id="rb-next-fill"${tip("Jump to the next fill")}>Fill</button>
      <button class="btn ghost" id="rb-next-kill"${tip("Jump to the next kill-switch change")}>Kill</button>
      <button class="btn ghost" id="rb-next"${tip("Jump to the next intent, decision, fill, kill or day end")}>Any ›</button>
    </div>
    <select class="sel" id="rb-speed" aria-label="Speed"><option value="10">10×</option><option value="30">30×</option><option value="60" selected>60×</option><option value="120">120×</option><option value="300">300×</option><option value="900">900×</option></select>
    <div class="scrub"><div class="scrub-marks" id="rb-marks"></div><input id="rb-scrub" type="range" min="0" max="1000" value="0" aria-label="Scrub the day"/></div>
    <span id="rb-time" class="num rb-time">--:--</span>
  </div>
  <main class="app" id="app">
    <aside class="rail rail-left" id="rail-left">${marketSkeleton()}</aside>
    ${centreSkeleton()}
    <aside class="rail rail-right" id="rail-right">${capitalSkeleton()}</aside>
  </main>
  <div id="toast" class="toast hidden"></div>`;
}

let lastAtt = "";
export function renderHud(c: Ctx): void {
  const st = c.st;
  const body = document.body;
  // ---- command bar
  setText($("mode-text"), modeText(c.mode, st.lastTs || null));
  const mi = $("mode-ind");
  mi.dataset.tip = MODE_TIP[c.mode];
  mi.className = `mode-ind m-${c.mode.toLowerCase()}`;
  const t = st.tony;
  const phase = st.phase || t?.system?.phase || "";
  const brokerUp = t?.system?.broker_connected;
  const tickAge = st.tickTs ? Math.max(0, (c.simNow - Date.parse(st.tickTs)) / 1000) : null;
  const feedTxt = c.feed === "replay" ? "Recorded day" : c.feed === "open" ? (tickAge !== null && tickAge > 5 ? `No tick for ${Math.round(tickAge)} s` : "Streaming") : c.feed === "error" ? "Reconnecting…" : "Connecting…";
  const feedTone = c.feed === "error" || (tickAge !== null && tickAge > 5 && phase !== "CLOSED" && phase !== "BEFORE_WINDOW") ? "warn" : c.feed === "replay" ? "replay" : "pos";
  const phaseTone = phase === "ENTRY_ALLOWED" ? "accent" : phase === "EXIT_ONLY" || phase === "FLATTENING" ? "warn" : "muted";
  setHTML(
    $("statuses"),
    [
      statusItem("Market", PHASE_LABEL[phase] ?? "—", phaseTone, "Trading-window phase of the SIMULATED session (TW config). Entries 09:20-14:00, flatten 14:50, hard flat 15:00 IST."),
      statusItem("Broker", brokerUp === undefined ? "—" : brokerUp ? "Fake · connected" : "Fake · link down", brokerUp === false ? "neg" : brokerUp ? "pos" : "muted", "A fake in-process broker. No real broker is connected; the Dhan token is never loaded."),
      statusItem("Data feed", feedTxt, feedTone as any, c.feed === "replay" ? "Replaying recorded SIMULATED events" : "Server-sent events from the SIMULATED session; synthetic ticks every 15 simulated seconds"),
      statusItem("Tony", t?.ai_status ?? "—", t?.attention?.level === "INTERVENTION" ? "neg" : "accent", "What Tony (the Trading CIO) is doing, from the rule-based status"),
    ].join(""),
  );
  const level: string = t?.attention?.level ?? "UNKNOWN";
  body.dataset.att = level;
  const chip = $("att-chip");
  setHTML(chip, `<i class="dot ${level === "NORMAL" ? "pos" : level === "ATTENTION" ? "warn" : level === "INTERVENTION" ? "neg" : "muted"} ${lastAtt && lastAtt !== level ? "pulse" : ""}"></i><span>${level === "INTERVENTION" ? "INTERVENTION REQUIRED" : esc(level)}</span>`);
  chip.dataset.tip = t?.attention?.headline ? `${t.attention.headline}. Click for the reasons and the rules.` : "Attention state unknown until Tony's first status";
  lastAtt = level;
  const latched = st.anyKill();
  const kb = $("kill-btn");
  kb.classList.toggle("latched", latched);
  setText($("kill-lbl"), latched ? "KILL LATCHED" : "ARMED");
  const vb = $("voice-btn");
  setHTML(vb, c.voiceOn ? "<span>♪</span>" : "<span class='t3'>♪</span>");
  vb.setAttribute("aria-pressed", String(c.voiceOn));
  vb.dataset.tip = c.voiceSupported ? `Tony's voiceover is ${c.voiceOn ? "on" : "off"} (browser speech synthesis, on this machine only). Speaks NOW updates and attention changes.` : "Voiceover unavailable: this browser has no speech synthesis.";
  $("m-live").classList.toggle("on", !c.replay);
  $("m-replay").classList.toggle("on", c.replay);

  // ---- zones
  const chartBox = $("mk-chart");
  for (const [id, html] of Object.entries(renderMarket(c, Math.max(120, Math.floor(chartBox.clientWidth)), Math.max(90, Math.floor(chartBox.clientHeight))))) setHTML($(id), html);
  const r = st.tick;
  tweenNumber($("mk-spot"), r ? Number(r.spot) : null, (v) => num(v));
  const chg = $("mk-chg");
  if (r) {
    setText(chg, `${signed(r.change)}  ${signed(r.change_pct)}%`);
    chg.className = `chg num ${Number(r.change) > 0 ? "tone-pos" : Number(r.change) < 0 ? "tone-neg" : "t2"}`;
  }
  for (const [id, html] of Object.entries(renderBrief(c))) setHTML($(id), html);
  const alert = $("b-alert");
  alert.hidden = alert.innerHTML === "";
  for (const [id, html] of Object.entries(renderPipeline(c))) setHTML($(id), html);
  setHTML($("act-list"), renderActivity(c));
  for (const [id, html] of Object.entries(renderCapital(c))) setHTML($(id), html);
  tweenNumber($("cap-nav"), st.risk ? Number(st.risk.nav) : null, (v) => inr(v));
}
