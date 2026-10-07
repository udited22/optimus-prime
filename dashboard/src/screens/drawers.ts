// Drawer content: Tony's answers, the trade thesis / lifecycle, the attention state and strategy detail.
// Every sentence comes from a SIMULATED event payload; nothing here composes new claims.
import { columnOf, PIPELINE } from "../model/lifecycle";
import { rMultiple, riskAtStop } from "../model/derive";
import { inr, inrSigned, istTime, num, signed } from "../fmt";
import { activityItem, kv, pill } from "../ui/components";
import { esc } from "../ui/dom";
import type { DashState } from "../state";

const asOf = (st: DashState) => `<p class="basis">Tony · ${esc(st.tony?.generated_by ?? "deterministic rules")} · as of <span class="num">${istTime(st.tony?.as_of)}</span> · SIMULATED</p>`;

export function answerHTML(st: DashState, qid: string): string {
  const a = st.tony?.answers?.[qid];
  if (!a) return `<p class="t2">Tony has no answer for this yet: the session has not produced the state it needs. (Unknown is reported as unknown.)</p>`;
  const basis = (a.basis as string[]).length ? `<div><div class="eyebrow">Based on</div><p class="basis">${(a.basis as string[]).map(esc).join(" · ")}</p></div>` : "";
  return `<div class="answer">${(a.lines as string[]).map((l) => `<p>${esc(l)}</p>`).join("")}</div>${basis}${asOf(st)}`;
}

export function attentionHTML(st: DashState): string {
  const a = st.tony?.attention;
  if (!a) return `<p class="t2">Unknown: no status from Tony yet.</p>`;
  const tone = a.level === "NORMAL" ? "pos" : a.level === "ATTENTION" ? "warn" : "neg";
  const concerns = (a.concerns as any[]).map((c) => {
    const t = c.level === "INTERVENTION" ? "neg" : c.level === "ATTENTION" ? "warn" : "muted";
    return `<div class="concern">${pill(c.level === "INFO" ? "notice" : c.level, t as any)}<p>${esc(c.text)}</p></div>`;
  });
  return `<p class="lead tone-${tone}">${esc(a.headline)}</p>
    ${concerns.length ? `<div class="concerns">${concerns.join("")}</div>` : `<p class="t2">Nothing needs review.</p>`}
    <div><div class="eyebrow">How the state is decided</div>
    <ul class="rules">
      <li><b class="tone-neg">Intervention required</b>: any kill switch or halt latched, a system-integrity failure, or an open position without a confirmed broker-side stop for more than 60 s.</li>
      <li><b class="tone-warn">Attention</b>: three Risk Governor rejections in a row within 60 min; the cost-justification advisory below threshold; no tick for 5 s; broker link down; a position open within 30 min of the forced flatten; a stop unconfirmed for over 30 s; daily loss at 50% of the stop; drawdown at the warning level.</li>
      <li><b class="tone-pos">Normal</b>: none of the above. The structural cost notice at the canary NAV is shown but does not raise the level.</li>
    </ul></div>${asOf(st)}`;
}

export function thesisHTML(st: DashState): string {
  const p = st.position;
  if (!p?.open) {
    const closed = st.fills.filter((f) => f.data.kind !== "ENTRY");
    return `<p class="t2">No open position. ${closed.length ? `${closed.length} exit fill${closed.length === 1 ? "" : "s"} today; see System Activity for each round trip.` : "No trade has been opened today."}</p>${asOf(st)}`;
  }
  const dec = st.decisions.find((d) => d.intent_id === p.intent_id);
  const ras = riskAtStop(p);
  const r = rMultiple(p);
  const sym = String(p.symbol);
  const timeline = st.log
    .filter((e) => String(e.data.text).includes(sym) || String(e.data.activity?.text ?? "").includes(sym) || (p.intent_id && String(e.data.text).includes(p.intent_id)))
    .map((e) => activityItem({ when: istTime(e.ts), actor: e.data.activity?.actor ?? "cio", text: e.data.activity?.text ?? e.data.text, tone: e.data.activity?.tone ?? "info" }))
    .join("");
  const exitPlan = st.tony?.next ? `<p>${esc(st.tony.next)}</p>` : "";
  return `<div><div class="eyebrow">Thesis</div><p class="lead">${esc(p.thesis || "The thesis for this position was not recorded (unknown).")}</p></div>
    <div class="pos-now">
      <div class="metric lg"><span class="metric-label">Unrealised</span><span class="metric-value ${Number(p.unrealised) >= 0 ? "tone-pos" : "tone-neg"}">${inrSigned(p.unrealised)}</span><span class="metric-sub">${r === null ? "" : `${signed(r, 2)}R · `}marked to the bid ${num(p.bid)}</span></div>
      ${kv([
        ["Instrument", esc(sym), ""],
        ["Side · size", `LONG · ${p.qty} qty (${p.lots} lot)`, ""],
        ["Entry", num(p.avg), ""],
        ["Stop (trigger / limit)", `${num(p.stop_trigger)} / ${num(p.stop_limit)}`, "neg"],
        ["Target", num(p.target), "pos"],
        ["Risk at the stop", ras === null ? "unknown" : inr(ras), ""],
        ["Broker-side stop", p.protective_confirmed ? "resting at the broker" : "not confirmed yet", p.protective_confirmed ? "pos" : "warn"],
      ])}
    </div>
    ${dec ? `<div><div class="eyebrow">Risk Governor decision</div>${kv([["Verdict", `<span class="tone-pos">${esc(dec.verdict)}</span> at ${istTime(dec.ts)}`, ""], ["Risk at stop vs budget", `${inr(dec.risk_at_stop)} of ${inr(dec.budget)}`, ""], ["Intent", `<span class="num">${esc(dec.intent_id)}</span>`, ""]])}</div>` : ""}
    <div><div class="eyebrow">Exit plan</div>${exitPlan || `<p class="t2">Unknown.</p>`}</div>
    <div><div class="eyebrow">Lifecycle</div><div class="steps">${timeline || `<p class="t3 small">No events recorded for this instrument yet.</p>`}</div></div>
    <p class="basis">Confidence is not shown: the strategy is rule-based and produces none.</p>${asOf(st)}`;
}

export function strategyHTML(st: DashState, id: string): string {
  const s = (st.tony?.strategies ?? []).find((x: any) => x.id === id);
  if (!s) return `<p class="t2">Unknown strategy.</p>`;
  const col = columnOf(s.stage);
  const decs = st.decisions.filter((d) => d.strategy === id).slice(-8).reverse();
  return `<p class="lead">${esc(s.name)} <span class="t3">· ${esc(s.hypothesis)}</span></p>
    ${kv([
      ["Pipeline column", `${esc(PIPELINE[col]?.label ?? "Unknown")} <span class="t3">· directive stage ${esc(s.stage)}</span>`, ""],
      ["State", esc(s.status), ""],
      ["Allocation", esc(s.allocation), ""],
      ["Intents today", String(s.intents ?? 0), ""],
      ["Trades today", String(s.trades ?? 0), ""],
      ["Net today", s.net === null || s.net === undefined ? "—" : inrSigned(s.net), Number(s.net) >= 0 ? "pos" : "neg"],
      ["Confidence", "not produced (rule-based)", ""],
    ])}
    <div><div class="eyebrow">Risk Governor decisions today</div><div class="steps">${
      decs.length
        ? decs.map((d) => activityItem({ when: istTime(d.ts), actor: "risk_governor", text: `${d.verdict.startsWith("APPROVE") ? "Approved" : "Rejected"} ${d.symbol}${d.simulate_only ? " (simulate-only)" : ""}${d.explain.length ? `: ${d.explain.join("; ")}` : ""}`, tone: d.verdict.startsWith("APPROVE") ? "ok" : "reject" })).join("")
        : `<p class="t3 small">No intents yet.</p>`
    }</div></div>
    <p class="basis">Stages are SIMULATED on this dashboard; every real StrategySpec is still RESEARCH (docs/research/strategy-hypotheses.md).</p>`;
}
