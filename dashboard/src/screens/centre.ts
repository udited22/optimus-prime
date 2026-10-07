// CENTRE · Tony's NOW / WHY / NEXT, the strategy pipeline and System Activity.
import { PIPELINE, columnOf, tradesCapital } from "../model/lifecycle";
import { triggerDistance } from "../model/derive";
import { inrSigned, istHM, istTime, num } from "../fmt";
import { activityItem, pill, regionHead } from "../ui/components";
import { esc, tip } from "../ui/dom";
import type { Ctx } from "./ctx";

export function centreSkeleton(): string {
  return `
  <section class="region graph-region hero" id="r-graph">
    <div class="hero-h"><span class="eyebrow"${tip("Tony's cognitive connectome. Each region is a real part of the SIMULATED system and each assembly a real strategy, check or process; an impulse travels only when a real event crosses a pathway, and leaves a few seconds of afterglow (docs/architecture/observability.md §17.5). Brightness is activity, never expected profit.")}>Cognitive connectome</span>
      <span class="legend nf-legend"><span><i class="swatch k-accent"></i>perception · system</span><span><i class="swatch k-violet"></i>strategy</span><span><i class="swatch k-warn"></i>evaluating</span><span><i class="swatch k-pos"></i>approved · executed</span><span><i class="swatch k-neg"></i>rejected · intervention</span></span></div>
    <div class="graph-box" id="graph"></div>
  </section>
  <section class="brief" id="r-brief" aria-live="polite">
    <button class="b-alert" id="b-alert" hidden></button>
    <div class="b-col b-now"><div class="eyebrow b-lbl">Now</div><div class="b-text" id="b-now">Connecting…</div></div>
    <div class="b-col"><div class="eyebrow b-lbl">Why</div><div class="b-text2" id="b-why"></div></div>
    <div class="b-col"><div class="eyebrow b-lbl">Next</div><div class="b-text2" id="b-next"></div><div class="b-live" id="b-live"></div></div>
    <div class="facts" id="b-facts"></div>
    <div class="b-foot" id="b-foot"></div>
  </section>
  <section class="region pipe-region" id="r-pipe">
    ${regionHead("Strategy pipeline", `<span${tip("Lifecycle stages are SIMULATED on this dashboard; every real StrategySpec is still RESEARCH (docs/research/strategy-hypotheses.md). Columns use the owner's labels with the directive stage underneath. Confidence is not shown: no strategy produces a calibrated confidence yet.")}>stages simulated · docs/research/strategy-hypotheses.md</span>`)}
    <div class="pipe-cols" id="pipe-cols"></div>
    <div class="pipe-cards" id="pipe-cards"></div>
  </section>
  <section class="region act-region" id="r-activity">
    <div class="region-h"><span class="eyebrow">System activity</span><div class="seg" id="act-seg"><button data-v="act" class="on">Activity</button><button data-v="raw">Raw logs</button></div></div>
    <div class="act-list" id="act-list"></div>
  </section>`;
}

export function renderBrief(c: Ctx): Record<string, string> {
  const t = c.st.tony;
  const out: Record<string, string> = {};
  if (!t) {
    out["b-now"] = "Waiting for Tony's first status";
    return out;
  }
  out["b-now"] = esc(t.now);
  // the attention headline sits above NOW whenever the level is not NORMAL, so "is anything abnormal?"
  // is answered without hovering the command-bar chip; the text is Tony's own headline, not new copy
  const a = t.attention;
  const lvl: string = a?.level ?? "NORMAL";
  const top = (a?.concerns ?? []).find((x: any) => x.level === lvl);
  out["b-alert"] =
    lvl === "NORMAL"
      ? ""
      : `<span class="ba-l">${lvl === "INTERVENTION" ? "Intervention required" : "Attention"}</span><span class="ba-t">${esc(top?.text ?? a?.headline ?? "")}</span><span class="ba-more">${(a?.concerns ?? []).length > 1 ? `+${a.concerns.length - 1} more · ` : ""}details →</span>`;
  out["b-why"] = esc(t.why);
  out["b-next"] = esc(t.next);
  const facts = (t.facts as { label: string; value: string }[]).map((f) => {
    const tone = f.label === "Risk state" ? (f.value === "NORMAL" ? "pos" : f.value === "ATTENTION" ? "warn" : "neg") : "";
    return `<div class="fact"${f.label === "Confidence" ? tip("Shown only when a model produces it. Today's strategies are rule-based, so there is no calibrated confidence to show.") : ""}><span class="metric-label">${esc(f.label)}</span><span class="fv ${tone ? `tone-${tone}` : ""}">${esc(f.value)}</span></div>`;
  });
  out["b-facts"] = facts.join("");
  const d = triggerDistance(c.st.tick?.spot, t.levels);
  out["b-live"] = d
    ? `<span class="num">NIFTY ${num(c.st.tick?.spot)}</span> · <span class="num tone-accent">${d.up > 0 ? `${num(d.up, 1)} pts below ▲` : "above ▲"}</span> · <span class="num tone-accent">${d.down > 0 ? `${num(d.down, 1)} pts above ▼` : "below ▼"}</span>`
    : "";
  out["b-foot"] = `<span>Tony · ${esc(t.generated_by)}</span><span class="num">as of ${istTime(t.as_of)}</span>`;
  return out;
}

export function renderPipeline(c: Ctx): Record<string, string> {
  const rows: any[] = c.st.tony?.strategies ?? c.st.strategies?.rows ?? [];
  const counts = PIPELINE.map((_, i) => rows.filter((r) => columnOf(r.stage) === i).length);
  const cols = PIPELINE.map(
    (p, i) => `<div class="pc ${counts[i] ? "has" : ""}"${tip(`${p.label}: ${p.meaning}. Directive stage${p.stages.length > 1 ? "s" : ""}: ${p.stages.join(", ")}.`)}><span class="pc-l">${esc(p.label)}</span><span class="pc-s">${p.stages.join(" · ")}</span><span class="pc-n num">${counts[i] || ""}</span></div>`,
  ).join("");
  const sorted = [...rows].sort((a, b) => columnOf(b.stage) - columnOf(a.stage));
  const cards = sorted
    .map((r) => {
      const col = columnOf(r.stage);
      const track = PIPELINE.map((_, i) => `<i class="${i < col ? "past" : i === col ? "here" : ""}"></i>`).join("");
      const ld = r.last_decision;
      const dec = ld
        ? `<span class="${String(ld.verdict).startsWith("APPROVE") ? "tone-pos" : "tone-warn"}">${String(ld.verdict).startsWith("APPROVE") ? "Approved" : "Rejected"}</span> <span class="num t3">${istHM(ld.at)}</span>`
        : `<span class="t3">no intent yet</span>`;
      const net = r.net === null || r.net === undefined ? `<span class="t3">—</span>` : `<span class="num ${Number(r.net) >= 0 ? "tone-pos" : "tone-neg"}">${inrSigned(r.net)}</span>`;
      const colLabel = PIPELINE[col]?.label ?? "Unknown";
      return `<button class="card scard ${tradesCapital(r.stage) ? "capital" : ""} ${r.killed ? "killed" : ""}" data-strategy="${esc(r.id)}"${tip(`${r.hypothesis} · ${r.name}. Click for the detail.`)}>
        <span class="sc-c"><span class="sc-id num">${esc(r.id)}${r.killed ? ` ${pill("kill-switched", "neg")}` : ""}</span><span class="sc-sub sc-name">${esc(r.name)}</span><span class="sc-sub sc-stage-n"><span class="t1">${esc(colLabel)}</span> · ${esc(r.stage)}</span></span>
        <span class="sc-c sc-stagecol"><span class="sc-track">${track}</span><span class="sc-sub"><span class="t1">${esc(colLabel)}</span> · ${esc(r.stage)}</span></span>
        <span class="sc-c"><span class="sc-main">${esc(r.status)}</span><span class="sc-sub">${esc(r.allocation)}</span></span>
        <span class="sc-c r"><span class="sc-main">${net}${r.trades ? ` <span class="t3">· ${r.trades} trade${r.trades === 1 ? "" : "s"}</span>` : ""}</span><span class="sc-sub"${ld ? tip(ld.text) : ""}>${dec}</span></span>
      </button>`;
    })
    .join("");
  return { "pipe-cols": cols, "pipe-cards": cards };
}

let seenSeq = 0;
export function renderActivity(c: Ctx): string {
  const logs = c.st.log.slice(-120).reverse();
  if (!logs.length) return `<p class="t3 small">Nothing has happened yet.</p>`;
  if (c.rawLogs)
    return `<div class="raw">${logs.map((e) => `<div><span class="t3">${istTime(e.ts)}</span> <span class="lv-${esc(e.data.level)}">${esc(e.data.level.padEnd(5))}</span> ${esc(e.data.text)}</div>`).join("")}</div>`;
  const newest = logs[0]?.seq ?? 0;
  const html = logs
    .map((e) => {
      const a = e.data.activity ?? { actor: "cio", text: e.data.text, tone: "info" };
      return activityItem({ when: istTime(e.ts), actor: a.actor, text: a.text, tone: a.tone, fresh: !c.replay && e.seq > seenSeq && seenSeq > 0 });
    })
    .join("");
  seenSeq = newest;
  return html;
}

export const decisionWhen = (ts: string): string => istHM(ts);
