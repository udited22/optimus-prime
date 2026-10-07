// Operational layers opened by clicking a region of the cognitive connectome (docs/architecture/observability.md §17.5 v3.1, v3.2). Each layer is the
// real state behind that region; nothing here is new data. Confidence appears nowhere because nothing produces it.
import { columnOf, PIPELINE } from "../model/lifecycle";
import { deployedFrac, exposure, riskAtStop } from "../model/derive";
import { inr, inrSigned, istTime, num, pct, signed } from "../fmt";
import { REGION_INFO, type RegionId } from "../connectome/regions";
import { activityItem, kv } from "../ui/components";
import { esc } from "../ui/dom";
import type { DashState } from "../state";
import { KILL_ORDER, KILL_SHORT } from "./ctx";

const basis = (st: DashState) => `<p class="basis">The real state behind this region · as of <span class="num">${istTime(st.lastTs || null)}</span> · SIMULATED</p>`;
const logsBy = (st: DashState, actors: string[], n = 8) =>
  st.log
    .filter((e) => actors.some((a) => (e.data.activity?.actor ?? "").startsWith(a)))
    .slice(-n)
    .reverse()
    .map((e) => activityItem({ when: istTime(e.ts), actor: e.data.activity?.actor ?? "cio", text: e.data.activity?.text ?? e.data.text, tone: e.data.activity?.tone ?? "info" }))
    .join("");
const section = (title: string, body: string) => `<div><div class="eyebrow">${esc(title)}</div>${body}</div>`;
const none = (t: string) => `<p class="t3 small">${esc(t)}</p>`;
const REGIME_WORD: Record<string, string> = {
  TRENDING_UP: "Trending up", TRENDING_DOWN: "Trending down", MEAN_REVERTING: "Range-bound",
  VOLATILITY_EXPANSION: "Volatility expansion", VOLATILITY_COMPRESSION: "Volatility compression", EXPIRY_DAY: "Expiry day",
  VOLATILITY_NORMAL: "Normal volatility", OPENING_DRIVE: "Opening drive", OPENING_REVERSION: "Opening reversion", GAP_REGIME: "Gap day", EXPIRY_REGIME: "Expiry day", EVENT_REGIME: "Event day", NO_EDGE: "No edge (classifier warming up)", ABNORMAL_MARKET: "Abnormal market",
};

export function layerTitle(r: RegionId): { eyebrow: string; title: string } {
  return { eyebrow: `Connectome · ${REGION_INFO[r].title}`, title: REGION_INFO[r].layer };
}

export function layerHTML(st: DashState, r: RegionId): string {
  const t = st.tony;
  const sys = t?.system ?? {};
  const rows: any[] = t?.strategies ?? st.strategies?.rows ?? [];
  switch (r) {
    case "PERCEPTION": {
      const sig = t?.signals ?? {};
      const dqKill = st.latched().some((s: any) => s.id === "DATA_QUALITY_KILL");
      return `<p class="lead">${esc(t?.market?.text ?? "No market read yet.")}</p>
        ${kv([
          ["NIFTY", `${num(st.tick?.spot)} <span class="t3">${st.tick ? `${signed(Number(st.tick.change), 2)} (${signed(Number(st.tick.change_pct), 2)}%)` : ""}</span>`, ""],
          ["India VIX", num(st.tick?.vix), ""],
          ["Regime tags", esc((st.regime?.tags ?? []).map((x: string) => REGIME_WORD[x] ?? x).join(" · ") || "none yet"), ""],
          ["30-min return", st.regime ? `${signed(Number(st.regime.ret_30m_pct), 2)}%` : "unknown", ""],
          ["Classifier", st.regime?.classifier ? `${esc(st.regime.classifier)} <span class="t3">${esc(st.regime.status ?? "")}</span>` : "unknown", st.regime?.status === "UNVALIDATED" ? "warn" : ""],
          ["Voter agreement", st.regime?.classifier_agreement ? `${num(st.regime.classifier_agreement)} <span class="t3">share of voters agreeing, not a probability</span>` : "none yet", ""],
          ["Opening range", sig.or_high ? `${num(sig.or_low)} – ${num(sig.or_high)} <span class="t3">± ${num(sig.or_buffer, 0)} buffer</span>` : `building until ${esc(sig.range_end ?? "09:30")}`, ""],
          ["VWAP", sig.vwap ? `${num(sig.vwap)} <span class="t3">± ${num(sig.vwap_band, 0)}</span>` : "unknown", ""],
          ["Feed", sys.feed_age_s === null || sys.feed_age_s === undefined ? "no tick yet" : `last tick ${num(sys.feed_age_s, 0)} s ago`, Number(sys.feed_age_s) >= 5 ? "warn" : ""],
          ["Data-quality kill", dqKill ? "LATCHED" : "clear", dqKill ? "neg" : "pos"],
        ])}
        <p class="basis">${esc(t?.market?.basis ?? "deterministic regime classifier (UNVALIDATED)")}. Seeded synthetic prices; no real market data.</p>${basis(st)}`;
    }
    case "STRATEGY":
      return `<p class="t2">Each strategy is a small pattern inside this region. Its resting brightness follows its lifecycle stage, and it fires when the strategy proposes an intent. Brightness is activity, not expected profit. No strategy produces a calibrated confidence, so none is shown.</p>
        <div class="steps">${rows
          .map((s) => {
            const col = PIPELINE[columnOf(s.stage)]?.label ?? "Unknown";
            const ld = s.last_decision;
            return `<button class="layer-row" data-strategy="${esc(s.id)}"><span class="num">${esc(s.id)}</span><span>${esc(s.name)} <span class="t3">· ${esc(s.hypothesis ?? "")}</span></span><span class="t2">${esc(col)} · ${esc(s.stage)}</span><span>${esc(s.status ?? "")}</span><span class="t3">${ld ? `${String(ld.verdict).startsWith("APPROVE") ? "approved" : "rejected"} ${istTime(ld.at)}` : "no intent yet"}</span></button>`;
          })
          .join("")}</div>${basis(st)}`;
    case "RISK":
      return riskLayer(st);
    case "PORTFOLIO":
      return portfolioLayer(st, rows);
    case "EXECUTION":
    case "BROKER":
      return executionLayer(st, sys);
    case "RESEARCH":
      return researchLayer(st, rows);
    case "CORE":
      return `<p class="lead">${esc(t?.now ?? "Waiting for Tony.")}</p>
        ${section("Why", `<p>${esc(t?.why ?? "unknown")}</p>`)}${section("Next", `<p>${esc(t?.next ?? "unknown")}</p>`)}
        ${kv([["Current thought", `${esc(t?.cognition?.verb ?? "unknown")} · ${esc(t?.cognition?.subject ?? "")}`, ""], ["Attention", esc(t?.attention?.headline ?? "unknown"), ""]])}${basis(st)}`;
  }
}

function riskLayer(st: DashState): string {
  const rk = st.risk ?? {};
  const decs = st.decisions.slice(-10).reverse();
  const latched = st.latched();
  const items = decs
    .map((d) => {
      const ok = d.verdict.startsWith("APPROVE");
      const why = d.explain.length ? `: ${d.explain.join("; ")}` : d.reasons.length ? `: ${d.reasons.join(", ")}` : "";
      const risk = d.risk_at_stop ? ` · risk ₹${num(d.risk_at_stop)} of ₹${num(d.budget)}` : "";
      return activityItem({ when: istTime(d.ts), actor: "risk_governor", text: `${ok ? "Approved" : "Rejected"} ${d.strategy} · ${d.symbol}${d.simulate_only ? " (simulate-only)" : ""}${why}${risk}`, tone: ok ? "ok" : "reject" });
    })
    .join("");
  return `${kv([
    ["Per-trade budget", `${inr(rk.per_trade_budget)} <span class="t3">risk at the stop, 2% of NAV</span>`, ""],
    ["Daily loss used", `${inr(rk.daily_loss)} <span class="t3">of ${inr(rk.daily_stop)}</span>`, ""],
    ["Drawdown", `${pct(Number(rk.dd_frac ?? 0), 2)} <span class="t3">warning ${pct(Number(rk.dd_warning_frac ?? 0.1), 0)} · suspend ${pct(Number(rk.dd_suspend_frac ?? 0.125), 1)} · ceiling ${pct(Number(rk.dd_hard_ceiling_frac ?? 0.15), 0)}</span>`, ""],
    ["Kill switches", latched.length ? `${latched.length} latched · ${latched.map((s: any) => esc(KILL_SHORT[s.id] ?? s.id)).join(", ")}` : `all ${KILL_ORDER.length} clear`, latched.length ? "neg" : "pos"],
  ])}
    ${section("Decisions and rejection rationale", items ? `<div class="steps">${items}</div>` : none("No intent has reached the Governor today."))}
    <p class="basis">A rejection is final for the day (docs/risk/risk-engine.md): no retry with changed parameters.</p>${basis(st)}`;
}

function portfolioLayer(st: DashState, rows: any[]): string {
  const rk = st.risk ?? {};
  const p = st.position;
  const ras = riskAtStop(p);
  const ex = exposure(p);
  return `${kv([
    ["NAV", inr(rk.nav), ""],
    ["Deployed", pct(deployedFrac(p, Number(rk.nav)) ?? 0, 1), ""],
    ["Exposure", ex ? `${inr(ex)} premium` : "₹0 · flat", ""],
    ["Available", inr(rk.cash), ""],
    ["Open position", p?.open ? `${esc(p.symbol)} · ${p.qty} qty · ${esc(p.strategy ?? "")}` : "flat", ""],
    ["Risk at the stop", ras === null ? "—" : inr(ras), ""],
    ["Entries today", `${rk.entries_today ?? 0} · ${rk.trades_closed ?? 0} closed`, ""],
    ["Sizing rule", "one lot max; the Governor checks the risk at the stop against the budget", ""],
  ])}
    ${section("Allocation by strategy", `<div class="steps">${rows.map((s) => `<div class="layer-row static"><span class="num">${esc(s.id)}</span><span class="t2">${esc(s.stage)}</span><span>${esc(s.allocation ?? "")}</span><span>${s.net === null || s.net === undefined ? "<span class='t3'>—</span>" : inrSigned(s.net)}</span></div>`).join("")}</div>`)}${basis(st)}`;
}

function executionLayer(st: DashState, sys: any): string {
  const p = st.position;
  const fills = st.fills.slice(-8).reverse();
  const bk = st.latched().some((s: any) => s.id === "BROKER_CONNECTIVITY_KILL");
  const fillItems = fills.map((f) => activityItem({ when: istTime(f.ts), actor: "broker", text: `${f.data.kind} ${f.data.side} ${f.data.qty} ${f.data.symbol} @ ₹${num(f.data.price)} (charges ₹${num(f.data.charges)})`, tone: "ok" })).join("");
  return `${kv([
    ["Broker", sys.broker_connected === false ? "link down (fake broker)" : "fake broker · connected", sys.broker_connected === false ? "warn" : "pos"],
    ["Broker-link kill", bk ? "LATCHED" : "clear", bk ? "neg" : "pos"],
    ["Protective stop", p?.open ? (p.protective_confirmed ? "resting at the broker" : "not confirmed yet") : "no position", p?.open && !p.protective_confirmed ? "warn" : ""],
  ])}
    ${section("Fills", fillItems ? `<div class="steps">${fillItems}</div>` : none("No fills today."))}
    ${section("Orders and broker messages", logsBy(st, ["execution", "broker"]) || none("No orders today."))}
    <p class="basis">The broker is the fake broker (SIMULATED). No real broker is connected and no real order can be placed.</p>${basis(st)}`;
}

function researchLayer(st: DashState, rows: any[]): string {
  const cand = rows.filter((s) => ["RESEARCH", "BACKTESTED", "VALIDATED", "PAPER", "SHADOW"].includes(s.stage));
  const list = cand.map((s) => `<button class="layer-row" data-strategy="${esc(s.id)}"><span class="num">${esc(s.id)}</span><span>${esc(s.name)}</span><span class="t2">${esc(PIPELINE[columnOf(s.stage)]?.label ?? "")} · ${esc(s.stage)}</span><span>${esc(s.status ?? "")}</span></button>`).join("");
  return `${section("Candidate strategies", list ? `<div class="steps">${list}</div>` : none("No candidates."))}
    ${section("Experiments and evidence", logsBy(st, ["post_trade", "validation", "strategy_factory"]) || none("No post-trade evidence or validation run yet today."))}
    ${st.economics?.headline ? section("Economics (post-trade, advisory, not a kill switch)", `<p class="t2">${esc(st.economics.headline)}</p>`) : ""}
    <p class="basis">Stages are SIMULATED on this dashboard; every real StrategySpec is still RESEARCH (docs/research/strategy-hypotheses.md).</p>${basis(st)}`;
}
