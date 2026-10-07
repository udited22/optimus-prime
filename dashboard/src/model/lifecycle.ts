// Strategy pipeline (docs/architecture/observability.md §17.5): the brief's seven columns mapped honestly onto the directive's nine
// lifecycle stages (docs/research/strategy-hypotheses.md). The real stage is always printed under the column label; no state is invented.

export interface PipelineColumn {
  /** the label from the owner's brief */
  label: string;
  /** directive stages that sit in this column */
  stages: string[];
  /** one line on what the column means in the directive */
  meaning: string;
}

export const PIPELINE: PipelineColumn[] = [
  { label: "Discovered", stages: ["RESEARCH"], meaning: "hypothesis registered; spec authored, not yet backtested" },
  { label: "Researching", stages: ["BACKTESTED", "VALIDATED"], meaning: "backtest and walk-forward evidence collected, then validated (docs/research/strategy-hypotheses.md)" },
  { label: "Paper", stages: ["PAPER", "SHADOW"], meaning: "simulate-only: paper fills, or shadowing live intents" },
  { label: "Approved", stages: ["CANARY"], meaning: "owner-approved for canary capital (1 lot max)" },
  { label: "Live", stages: ["PRODUCTION"], meaning: "full allocation under the Risk Governor" },
  { label: "Paused", stages: ["DEGRADED", "QUARANTINED"], meaning: "demoted by evidence or a kill; no new entries" },
  { label: "Retired", stages: ["RETIRED"], meaning: "permanently withdrawn" },
];

export const ALL_STAGES = PIPELINE.flatMap((c) => c.stages);

/** Column index for a directive stage; -1 for a stage the directive does not define (shown as unknown). */
export function columnOf(stage: string): number {
  return PIPELINE.findIndex((c) => c.stages.includes(stage));
}

/** Does this stage put capital at risk? (CANARY and PRODUCTION only; everything else is simulate-only or idle.) */
export const tradesCapital = (stage: string): boolean => stage === "CANARY" || stage === "PRODUCTION";

/** Does this stage receive the cleaned market feed (and so propose intents)? */
export const receivesFeed = (stage: string): boolean =>
  ["PAPER", "SHADOW", "CANARY", "PRODUCTION", "DEGRADED"].includes(stage);
