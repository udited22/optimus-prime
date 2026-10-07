// LEFT RAIL · market context: index, intraday chart, volatility, regime, Tony's market read, the signals the
// rules are watching, an option-chain summary that expands, and the session's trading-window rules.
import { chainSummary, chartWindow, hm, n, nextMark, sessionMarks, trend, triggerDistance } from "../model/derive";
import { minToHM, num, signed } from "../fmt";
import { kv, pill, priceChart, regionHead, table } from "../ui/components";
import { esc, tip } from "../ui/dom";
import { type Ctx, PHASE_LABEL, regimeLabel } from "./ctx";

export function marketSkeleton(): string {
  return `
  <section class="region" id="r-market">
    ${regionHead("NIFTY 50", `<span id="mk-meta"></span>`, "NIFTY 50 index level from the SIMULATED feed (synthetic prices, not market data)")}
    <div class="price-row"><span class="price num" id="mk-spot">—</span><span class="chg num" id="mk-chg"></span></div>
    <div class="chart-box" id="mk-chart"></div>
    <div id="mk-vol"></div>
  </section>
  <section class="region" id="r-intel">
    ${regionHead("Market intelligence", "", "Deterministic regime classifier (K-11, UNVALIDATED, thresholds ASSUMED) on SIMULATED 1-minute bars. Not a forecast.")}
    <div id="mk-read"></div>
  </section>
  <section class="region" id="r-signals">
    ${regionHead("Key signals", "", "Levels the live rules are watching right now, published by Tony from the simulator state")}
    <div id="mk-signals"></div>
  </section>
  <section class="region" id="r-chain">
    <div class="region-h"><span class="eyebrow"${tip("ATM summary of the SIMULATED option chain (Black-Scholes prices around the synthetic spot). The simulator produces no open interest, so OI and PCR are not shown.")}>Option chain</span><button class="link" id="chain-toggle">Show strikes</button></div>
    <div id="mk-chain"></div>
  </section>
  <section class="region" id="r-session">
    ${regionHead("Session", `<span id="ss-meta"></span>`, "Trading-window rules (TW config): order activity 09:15-15:00, new entries 09:20-14:00, forced flatten from 14:50, hard flat 15:00")}
    <div id="mk-session"></div>
  </section>`;
}

export function renderMarket(c: Ctx, chartW: number, chartH: number): Record<string, string> {
  const st = c.st;
  const out: Record<string, string> = {};
  const s = st.session;
  out["mk-meta"] = s ? `expiry ${esc(istDay(s.expiry))} · lot ${s.lot}` : "";
  // chart: the day's line + the opening range and published triggers when the rules have them
  const sig = st.tony?.signals ?? {};
  const lv = st.tony?.levels ?? {};
  const orH = n(sig.or_high);
  const orL = n(sig.or_low);
  const lines: { y: number; label: string; tone: "accent" | "warn" | "muted" | "pos" | "neg" }[] = [];
  if (n(lv.trigger_up) !== null) lines.push({ y: n(lv.trigger_up)!, label: `▲ ${num(lv.trigger_up, 0)}`, tone: "accent" });
  if (n(lv.trigger_down) !== null) lines.push({ y: n(lv.trigger_down)!, label: `▼ ${num(lv.trigger_down, 0)}`, tone: "accent" });
  if (n(sig.vwap) !== null) lines.push({ y: n(sig.vwap)!, label: `VWAP ${num(sig.vwap, 0)}`, tone: "muted" });
  const [c0, c1] = chartWindow(st.series.length ? st.series[st.series.length - 1][0] : null);
  out["mk-chart"] = priceChart(st.series, chartW, chartH, {
    t0: c0,
    t1: c1,
    band: orH !== null && orL !== null ? { lo: orL, hi: orH, label: "OR" } : null,
    lines,
  });
  // volatility + regime
  const vt = trend(st.tick?.vix, st.vixOpen);
  const arrow = vt === "up" ? "↑" : vt === "down" ? "↓" : vt === "flat" ? "→" : "";
  const ret = n(st.regime?.ret_30m_pct);
  const tags: string[] = st.regime?.tags ?? [];
  out["mk-vol"] = `<div class="vol-row">
      <div class="metric md"${tip(`India VIX (SIMULATED). Arrow: change against the session's first reading (${num(st.vixOpen)}); ±2% counts as flat.`)}><span class="metric-label">VIX</span><span class="metric-value">${num(st.tick?.vix)} <span class="t2">${arrow}</span></span></div>
      <div class="metric md"${tip("NIFTY change over the last 30 simulated minutes")}><span class="metric-label">30 min</span><span class="metric-value ${ret === null ? "" : ret > 0 ? "tone-pos" : ret < 0 ? "tone-neg" : ""}">${ret === null ? "—" : `${signed(ret, 2)}%`}</span></div>
    </div>
    <div class="tags">${tags.map((t) => pill(regimeLabel(t), "ghost")).join("")}</div>`;
  // Tony's market read
  const m = st.tony?.market;
  out["mk-read"] = m ? `<p class="read">${esc(m.text)}</p><p class="basis">${esc(m.basis)}</p>` : `<p class="read t3">No market read yet.</p>`;
  // signals
  const rows: [string, string, ("pos" | "neg" | "warn" | "accent" | "muted" | "")?, string?][] = [];
  const spot = st.tick?.spot;
  if (orH !== null && orL !== null) rows.push(["Opening range", `${num(orL, 0)} – ${num(orH, 0)}`, "", `09:15-${sig.range_end ?? "09:30"} high and low (S-ORB-001)`]);
  else if (sig.orb_status === "BUILDING_RANGE") rows.push(["Opening range", `building until ${esc(sig.range_end ?? "09:30")}`, "muted"]);
  const dist = triggerDistance(spot, lv);
  if (dist) {
    rows.push(["Breakout trigger", `▲ ${num(lv.trigger_up, 0)} · ▼ ${num(lv.trigger_down, 0)}`, "", `opening range ± ${num(sig.or_buffer, 0)}-point buffer`]);
    rows.push(["Distance", `${dist.up > 0 ? `${num(dist.up, 1)} to ▲` : "above ▲"} · ${dist.down > 0 ? `${num(dist.down, 1)} to ▼` : "below ▼"}`, "accent", "points from the current spot to each trigger; checked every 5 minutes"]);
  }
  if (n(sig.vwap) !== null) rows.push(["VWAP", `${num(sig.vwap, 2)} ± ${num(sig.vwap_band, 0)}`, "", "session VWAP (synthetic volume) and the S-VWAPC-001 pullback band"]);
  if (sig.orb_status) rows.push(["S-ORB-001", ORB_STATE[sig.orb_status] ?? esc(sig.orb_status), sig.orb_status === "STOOD_DOWN" ? "warn" : ""]);
  out["mk-signals"] = rows.length ? kv(rows) : `<p class="t3 small">Waiting for the session.</p>`;
  // option chain
  const cs = chainSummary(st.chain);
  if (!cs) out["mk-chain"] = `<p class="t3 small">No chain yet.</p>`;
  else {
    const sum = `<div class="chain-sum">
      ${cell("ATM", num(cs.atm, 0))}${cell("CE", num(cs.ceMid))}${cell("PE", num(cs.peMid))}${cell("Straddle", num(cs.straddle))}${cell("ATM IV", cs.atmIv === null ? "—" : `${num(cs.atmIv, 1)}%`)}${cell("Skew", cs.skew === null ? "—" : `${signed(cs.skew, 1)}`, "PE IV minus CE IV at the ATM strike, in vol points")}
    </div>`;
    let strikes = "";
    if (c.chainOpen) {
      const rowsC = (st.chain.rows as any[])
        .filter((r) => Math.abs(r.strike - st.chain.atm) <= 200)
        .map((r) => {
          const itmC = r.strike < Number(st.chain.spot);
          return { cls: r.strike === st.chain.atm ? "atm" : "", cells: [num(r.ce.bid), num(r.ce.ask), num(r.ce.iv, 1), String(r.strike), num(r.pe.iv, 1), num(r.pe.bid), num(r.pe.ask)], cellCls: [itmC ? "itm" : "", itmC ? "itm" : "", "", "", "", !itmC ? "itm" : "", !itmC ? "itm" : ""] };
        });
      strikes = `<div class="chain-full">${table(["CE bid", "ask", "IV", "Strike", "IV", "PE bid", "ask"], rowsC, 3)}<p class="basis">Expiry ${esc(istDay(st.chain.expiry))} · ITM shaded brighter · no OI in the simulator</p></div>`;
    }
    out["mk-chain"] = sum + strikes;
  }
  // session line
  const w = s?.window;
  const marks = sessionMarks(w);
  const now = st.lastTs ? hmOf(st.lastTs) : hm("09:15");
  const t0 = hm("09:15");
  const t1 = hm("15:30");
  const X = (m: number) => `${(((Math.max(t0, Math.min(t1, m)) - t0) / (t1 - t0)) * 100).toFixed(2)}%`;
  const seg = (a: number, b: number, cls: string, label: string) => `<span class="seg-${cls}" style="left:${X(a)};width:calc(${X(b)} - ${X(a)})"${tip(label)}></span>`;
  const [, es, ec, fs, hf] = marks.map((x) => x.at);
  const nx = nextMark(marks, now);
  const left = nx ? Math.max(0, Math.round(nx.at - now)) : 0;
  out["ss-meta"] = esc(PHASE_LABEL[st.phase] ?? (st.phase || "—"));
  out["mk-session"] = `<div class="session-line">
      ${seg(t0, es, "pre", "09:15-09:20 order activity allowed, no new entries")}${seg(es, ec, "entry", "new entries allowed")}${seg(ec, fs, "exit", "exits only")}${seg(fs, hf, "flat", "forced flatten")}${seg(hf, t1, "closed", "no order activity")}
      <span class="now-mark" style="left:${X(now)}"></span>
    </div>
    <div class="session-ticks"><span style="left:0">09:15</span><span style="left:${X(ec)}">${minToHM(ec)}</span><span style="left:${X(fs)}">${minToHM(fs)}</span><span style="left:100%">15:30</span></div>
    <p class="small t3">Entries ${minToHM(es)}–${minToHM(ec)} · exits only to ${minToHM(fs)} · forced flatten ${minToHM(fs)}–${minToHM(hf)} · hard flat ${minToHM(hf)}</p>
    <p class="small t2">${nx ? `${esc(nx.label)} at ${minToHM(nx.at)} · in ${Math.floor(left / 60)}h ${String(left % 60).padStart(2, "0")}m` : "Session over"}</p>`;
  return out;
}

const ORB_STATE: Record<string, string> = {
  BUILDING_RANGE: "building the range",
  ARMED: "watching for a breakout",
  WORKING: "entry order working",
  IN_TRADE: "in a trade",
  STOOD_DOWN: "stood down (rejected)",
  DONE: "done for the day",
};

const cell = (k: string, v: string, t = ""): string => `<div class="cs"${t ? tip(t) : ""}><span class="metric-label">${k}</span><span class="num">${v}</span></div>`;
const hmOf = (ts: string): number => {
  const [h, m, s] = istHMS(ts).split(":").map(Number);
  return h * 60 + m + s / 60;
};
const istHMS = (ts: string) => new Date(Date.parse(ts) + 5.5 * 3600_000).toISOString().slice(11, 19);
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const istDay = (d: string | null | undefined): string => {
  if (!d) return "—";
  const [, m, dd] = d.slice(0, 10).split("-").map(Number);
  return `${String(dd).padStart(2, "0")} ${MON[m - 1]}`;
};
