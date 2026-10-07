// RIGHT RAIL · capital, risk, safeguards (the nine kill switches), the open position and economics.
import { deployedFrac, exposure, ladderFrac, n, rMultiple, riskAtStop, riskBudgetState } from "../model/derive";
import { inr, inrSigned, istHM, istTime, num, pct, pctSigned, signed } from "../fmt";
import { type Tone, dot, kv, regionHead, riskRow } from "../ui/components";
import { esc, tip } from "../ui/dom";
import { type Ctx, KILL_ORDER, KILL_SHORT } from "./ctx";

export function capitalSkeleton(): string {
  return `
  <section class="region" id="r-capital">
    ${regionHead("Capital", `<span id="cap-meta"></span>`, "Net asset value of the SIMULATED account (fake broker), marked to the bid; realised P&L is net of charges")}
    <div class="metric xl"><span class="metric-label">NAV</span><span class="metric-value" id="cap-nav">—</span></div>
    <div class="today" id="cap-today"></div>
    <div id="cap-kv"></div>
  </section>
  <section class="region" id="r-risk">
    ${regionHead("Risk", `<span id="risk-state"></span>`, "Limits from the risk-limits config (RL). The Risk Governor enforces them on every intent; these bars only report.")}
    <div class="risk-rows" id="risk-rows"></div>
  </section>
  <section class="region" id="r-kills">
    <div class="region-h"><span class="eyebrow"${tip("The nine kernel kill switches (docs/risk/risk-engine.md). Each latches until its reset rule is met; MANUAL_MASTER_KILL is the only one the dashboard can trigger.")}>Safeguards</span><button class="link" id="kills-toggle">Details</button></div>
    <div id="kills-body"></div>
  </section>
  <section class="region" id="r-position">
    ${regionHead("Open position", `<span id="pos-meta"></span>`)}
    <div id="pos-body"></div>
  </section>
  <section class="region" id="r-econ">
    ${regionHead("Economics · net of everything", `<span id="econ-meta"></span>`, "docs/risk/system-economics.md: gross P&L less charges, the day's share of fixed running costs and ASSUMED tax. Advisory only: it is not a kill switch.")}
    <div id="econ-body"></div>
  </section>`;
}

export function renderCapital(c: Ctx): Record<string, string> {
  const st = c.st;
  const r = st.risk;
  const out: Record<string, string> = {};
  if (!r) return out;
  const nav = n(r.nav) ?? 0;
  const sod = n(r.sod_nav) ?? 0;
  const day = nav - sod;
  const dayPct = sod ? day / sod : 0;
  const tone: Tone = day > 0.005 ? "pos" : day < -0.005 ? "neg" : "";
  out["cap-today"] = `<span class="metric-label">Today</span><span class="num today-v ${tone ? `tone-${tone}` : ""}">${inrSigned(day)}</span><span class="num today-p ${tone ? `tone-${tone}` : ""}">${pctSigned(dayPct)}</span>`;
  const dep = deployedFrac(st.position, r.nav);
  const cash = n(r.cash);
  out["cap-kv"] = `${kv([
    ["Realised", inrSigned(r.realised), toneOf(r.realised), "closed trades today, net of charges"],
    ["Unrealised", inrSigned(r.unrealised), toneOf(r.unrealised), "open position marked to the bid"],
    ["Available", cash === null ? "unknown · broker unreachable" : inr(cash), cash === null ? "warn" : "", "cash reported by the (fake) broker"],
    ["Deployed", dep === null ? "—" : pct(dep, 1), "", "option premium in the open position as a share of NAV"],
  ])}<div class="dep-bar">${bar(dep ?? 0, "accent")}</div>`;
  out["cap-meta"] = `start of day ${inr(r.sod_nav)}`;
  // risk
  const dl = n(r.daily_loss) ?? 0;
  const ds = n(r.daily_stop) ?? 0;
  const ddf = n(r.dd_frac) ?? 0;
  const ceil = n(r.dd_hard_ceiling_frac) ?? 0.15;
  const warnF = n(r.dd_warning_frac) ?? 0.1;
  const susF = n(r.dd_suspend_frac) ?? 0.125;
  const ras = riskAtStop(st.position);
  const budget = n(r.per_trade_budget) ?? 0;
  const ex = exposure(st.position);
  const dlF = ds ? dl / ds : 0;
  const rb = riskBudgetState({ dailyLossFrac: dlF, ddFrac: ddf, ddWarnFrac: warnF, ddSuspendFrac: susF, killLatched: st.anyKill(), halted: (st.kills?.halts ?? []).length > 0 });
  out["risk-state"] = `<span class="tone-${rb.tone}"${tip(`Risk budget ${rb.label}: ${rb.why}`)}>Risk budget ${rb.label}</span>`;
  out["risk-rows"] = [
    riskRow({ label: "Daily loss used", value: `${pct(dlF, 0)} <span class="t3">of ${inr(ds, 0)}</span>`, frac: dlF, tone: dlF >= 1 ? "neg" : dlF >= 0.5 ? "warn" : "accent", marks: [{ at: 0.5, tone: "warn" }], tipText: `Loss today ${inr(dl)} against the daily stop of ${inr(ds)} (4% of the start-of-day NAV). The DAILY_LOSS_KILL latches at 100%; Tony flags 50%.` }),
    riskRow({ label: "Drawdown", value: `${pct(ddf, 2)} <span class="t3">of ${pct(ceil, 0)}</span>`, frac: ddf / ceil, tone: ddf >= susF ? "neg" : ddf >= warnF ? "warn" : "accent", marks: [{ at: warnF / ceil, tone: "warn", label: "warning" }, { at: susF / ceil, tone: "neg", label: "suspend" }], tipText: `From the high-water mark ${inr(r.hwm)}. Warning ${pct(warnF, 1)}, suspension ${pct(susF, 1)}, hard ceiling ${pct(ceil, 1)}.` }),
    riskRow({ label: "Per-trade risk", value: ras === null ? `— <span class="t3">cap ${inr(budget, 0)}</span>` : `${inr(ras, 0)} <span class="t3">of ${inr(budget, 0)}</span>`, frac: ras === null || !budget ? 0 : ras / budget, tone: "accent", tipText: "Rupees lost if the open position's stop-limit fills, against the 2% per-trade budget the Governor sized it with" }),
    riskRow({ label: "Exposure", value: ex ? `${inr(ex, 0)} <span class="t3">premium</span>` : `₹0 <span class="t3">flat</span>`, frac: nav ? ex / nav : 0, tone: "accent", tipText: "Premium paid for open long options (the most they can lose)" }),
  ].join("") + kv([
    ["Open positions", st.position?.open ? "1 <span class='t3'>of 1 max</span>" : "0 <span class='t3'>of 1 max</span>", ""],
    ["Entries today", `${r.entries_today} <span class="t3">· ${r.trades_closed} closed</span>`, ""],
  ]);
  // kill switches
  const sw: any[] = st.kills?.switches ?? KILL_ORDER.map((id) => ({ id, latched: false }));
  const latched = sw.filter((s) => s.latched);
  const halts: any[] = st.kills?.halts ?? [];
  const dots = KILL_ORDER.map((id) => {
    const s = sw.find((x) => x.id === id) ?? { latched: false };
    return `<span class="kd ${s.latched ? "on" : ""}"${tip(`${id}: ${s.latched ? `LATCHED at ${istTime(s.at)} · ${s.reason}` : "clear"}`)}>${dot(s.latched ? "neg" : "ring")}</span>`;
  }).join("");
  const head = latched.length
    ? `<span class="tone-neg strong">${latched.length} latched</span> <span class="t2">· ${latched.map((s) => esc(KILL_SHORT[s.id] ?? s.id)).join(", ")}</span>`
    : `<span class="t2">All 9 kill switches clear</span>`;
  const list = c.killsOpen || latched.length
    ? `<div class="kill-list">${(c.killsOpen ? KILL_ORDER : latched.map((s) => s.id))
        .map((id) => {
          const s = sw.find((x) => x.id === id) ?? { latched: false };
          return `<div class="kl ${s.latched ? "on" : ""}">${dot(s.latched ? "neg" : "ring")}<span class="kl-n">${esc(KILL_SHORT[id])}</span><span class="kl-s">${s.latched ? `${esc(s.reason)} · ${istHM(s.at)}` : "clear"}</span></div>`;
        })
        .join("")}${halts.map((h) => `<div class="kl on">${dot("neg")}<span class="kl-n">Halt ${esc(h.kind)}</span><span class="kl-s">${istHM(h.at)}</span></div>`).join("")}</div>`
    : "";
  out["kills-body"] = `<div class="kill-dots">${dots}<span class="kd-l">${head}</span></div>${list}`;
  // position
  out["pos-body"] = positionHTML(st.position, st.tony);
  out["pos-meta"] = st.position?.open ? `opened ${istHM(st.position.opened_at)}` : "";
  // economics
  const e = st.economics;
  if (e) {
    const t = e.today ?? {};
    out["econ-meta"] = `${esc(e.month)} · SIMULATED`;
    out["econ-body"] = `<div class="econ-net"><span class="metric-label">Net today</span><span class="num ${Number(t.net) >= 0 ? "tone-pos" : "tone-neg"}">${inrSigned(t.net)}</span></div>
      ${kv([
        ["Gross", signed(t.gross), ""],
        ["Charges", signed(-Number(t.charges ?? 0)), ""],
        [`Fixed ÷ ${t.month_trading_days ?? "—"} days`, signed(-Number(t.fixed_share ?? 0)), ""],
        [`Tax · ${e.tax_status ?? ""}`, signed(-Number(t.tax ?? 0)), ""],
        ["Fixed costs a month", `${inr(e.fixed_monthly, 0)} <span class="t3">· ${pct(e.fixed_frac, 1)} of NAV</span>`, ""],
      ])}
      <p class="advisory ${e.raised ? "on" : ""}"${tip((e.recommendations ?? []).join("\n\n"))}>${esc(e.headline)} <span class="t3">· advisory, not a kill switch</span></p>`;
  } else out["econ-body"] = `<p class="t3 small">Waiting for the first snapshot.</p>`;
  return out;
}

function positionHTML(p: any, tony: any): string {
  if (!p?.open)
    return `<div class="flat"><span class="t2">Flat</span><span class="t3 small">One lot max. Every entry carries a broker-side stop.</span></div>`;
  const r = rMultiple(p);
  const lf = ladderFrac(p);
  const u = n(p.unrealised) ?? 0;
  const strat = (tony?.strategies ?? []).find((s: any) => s.id === p.strategy);
  return `<button class="card pos-card" id="pos-card"${tip("Open the trade thesis and lifecycle")}>
    <div class="pc-head"><span class="pc-sym">${esc(p.symbol)}</span><span class="num pc-pnl ${u >= 0 ? "tone-pos" : "tone-neg"}">${inrSigned(u)}</span></div>
    <div class="pc-sub"><span class="tone-accent">LONG</span> · ${p.qty} qty · ${p.lots} lot · <span class="num">${esc(p.strategy)}</span>${strat ? ` <span class="t3">${esc(strat.stage)}</span>` : ""}</div>
    <div class="pc-grid">
      ${cell("Entry", num(p.avg))}${cell("LTP (bid)", num(p.bid))}${cell("R", r === null ? "—" : `${signed(r, 2)}R`, "Unrealised move in units of the risk taken: 1R = entry to stop-limit")}
      ${cell("Stop", `${num(p.stop_trigger)}`, `Broker-side stop: trigger ${num(p.stop_trigger)}, limit ${num(p.stop_limit)}`, "neg")}${cell("Target", num(p.target), "", "pos")}${cell("Broker stop", p.protective_confirmed ? "resting" : "pending", "", p.protective_confirmed ? "pos" : "warn")}
    </div>
    ${lf ? `<div class="ladder"${tip("Price ladder: stop on the left, target on the right; the tick is the entry, the dot is the current bid")}><span class="ld-track"></span><span class="ld-entry" style="left:${(lf.entry * 100).toFixed(1)}%"></span><span class="ld-bid ${n(p.bid)! >= n(p.avg)! ? "up" : "dn"}" style="left:${(lf.bid * 100).toFixed(1)}%"></span><span class="ld-l num">SL ${num(p.stop_trigger)}</span><span class="ld-r num">TGT ${num(p.target)}</span></div>` : ""}
    <div class="pc-foot t3 small">Thesis &amp; lifecycle →</div>
  </button>`;
}

const cell = (k: string, v: string, t = "", tone = ""): string => `<div class="pcell"${t ? tip(t) : ""}><span class="metric-label">${k}</span><span class="num ${tone ? `tone-${tone}` : ""}">${v}</span></div>`;
const bar = (f: number, tone: string): string => `<div class="bar"><span class="fill ${tone}" style="width:${(Math.max(0, Math.min(1, f)) * 100).toFixed(1)}%"></span></div>`;
const toneOf = (x: unknown): Tone => {
  const v = Number(x);
  return !Number.isFinite(v) || Math.abs(v) < 0.005 ? "" : v > 0 ? "pos" : "neg";
};
