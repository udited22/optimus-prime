import { describe, expect, it } from "vitest";
import { chartWindow, riskBudgetState } from "../src/model/derive";
import { displayMode, modeText } from "../src/model/mode";
import { ALL_STAGES, PIPELINE, columnOf, receivesFeed, tradesCapital } from "../src/model/lifecycle";
import { CONTROL_ACTIONS, READ_ONLY_REASON, buildCommands, filterCommands } from "../src/model/palette";
import {
  chainSummary, deployedFrac, exposure, ladderFrac, nextMark, rMultiple, riskAtStop, sessionMarks, trend, triggerDistance,
} from "../src/model/derive";

describe("global mode indicator", () => {
  const base = { replay: false, serverLabel: "SIMULATED", simulatedSeen: 10, realSeen: 0 };
  it("is SIMULATION for the simulated backend and REPLAY for a recorded day", () => {
    expect(displayMode(base)).toBe("SIMULATION");
    expect(displayMode({ ...base, replay: true })).toBe("REPLAY");
  });
  it("can never say LIVE while anything is simulated or the server calls itself SIMULATED", () => {
    expect(displayMode({ ...base, realSeen: 5 })).toBe("SIMULATION");
    expect(displayMode({ ...base, serverLabel: "REAL", simulatedSeen: 1, realSeen: 5 })).toBe("SIMULATION");
    expect(displayMode({ ...base, serverLabel: null, simulatedSeen: 0, realSeen: 5 })).toBe("SIMULATION");
    expect(displayMode({ ...base, serverLabel: "REAL", simulatedSeen: 0, realSeen: 0 })).toBe("SIMULATION");
    expect(displayMode({ ...base, serverLabel: "simulation", simulatedSeen: 0, realSeen: 5 })).toBe("SIMULATION");
    // only positive evidence on every axis would allow it (no such backend exists in this project)
    expect(displayMode({ replay: false, serverLabel: "REAL", simulatedSeen: 0, realSeen: 1 })).toBe("LIVE");
  });
  it("prints the IST date and time regardless of the browser zone", () => {
    expect(modeText("REPLAY", "2026-10-08T11:44:06+05:30")).toBe("REPLAY · 08 OCT 2026 · 11:44:06");
    expect(modeText("SIMULATION", "2026-10-05T04:25:12+00:00")).toBe("SIMULATION · 05 OCT 2026 · 09:55:12");
    expect(modeText("SIMULATION", null)).toBe("SIMULATION");
  });
});

describe("lifecycle mapping", () => {
  it("covers each of the ten directive stages exactly once", () => {
    expect([...ALL_STAGES].sort()).toEqual(
      ["BACKTESTED", "CANARY", "DEGRADED", "PAPER", "PRODUCTION", "QUARANTINED", "RESEARCH", "RETIRED", "SHADOW", "VALIDATED"],
    );
    expect(new Set(ALL_STAGES).size).toBe(10);
    expect(PIPELINE.map((c) => c.label)).toEqual(["Discovered", "Researching", "Paper", "Approved", "Live", "Paused", "Retired"]);
  });
  it("maps stages to the brief's columns and refuses unknown stages", () => {
    expect(columnOf("RESEARCH")).toBe(0);
    expect(columnOf("SHADOW")).toBe(2);
    expect(columnOf("CANARY")).toBe(3);
    expect(columnOf("QUARANTINED")).toBe(5);
    expect(columnOf("MADE_UP")).toBe(-1);
    expect(tradesCapital("CANARY") && !tradesCapital("SHADOW")).toBe(true);
    expect(receivesFeed("PAPER") && !receivesFeed("RESEARCH")).toBe(true);
  });
});

describe("command palette", () => {
  const ctx = { questions: [{ id: "why_not_trading", text: "Why are we not trading?" }, { id: "last_rejection", text: "Why was the last trade rejected?" }], replay: false, voiceOn: false, hasPosition: false, replayDays: [{ name: "broker-drop", title: "Thu 08-Oct-2026" }] };
  const cmds = buildCommands(ctx);
  it("lists control actions disabled with the read-only reason, and nothing order-like is runnable", () => {
    const pause = cmds.find((c) => c.title === "Pause strategy");
    expect(pause?.disabled).toBe(READ_ONLY_REASON);
    expect(READ_ONLY_REASON).toBe("control actions not enabled in read-only mode");
    for (const c of CONTROL_ACTIONS) expect(cmds.find((x) => x.id === c.id)?.disabled).toBe(READ_ONLY_REASON);
    const runnable = cmds.filter((c) => !c.disabled);
    expect(runnable.filter((c) => /order|buy|sell|flatten|pause|resume|limit|promote/i.test(c.title + c.id))).toEqual([]);
    expect(runnable.find((c) => c.id === "kill:open")?.hint).toContain("MANUAL_MASTER_KILL");
  });
  it("matches natural questions to Tony's answers", () => {
    expect(filterCommands(cmds, "why not trading")[0].id).toBe("ask:why_not_trading");
    expect(filterCommands(cmds, "last rejected")[0].id).toBe("ask:last_rejection");
    expect(filterCommands(cmds, "enter replay")[0].id).toBe("mode:replay");
    expect(filterCommands(cmds, "replay").every((c) => c.id.startsWith("mode:replay"))).toBe(true);
    expect(filterCommands(cmds, "pause")[0].title).toBe("Pause strategy");
    expect(filterCommands(cmds, "zzzz")).toEqual([]);
    expect(filterCommands(cmds, "").length).toBe(cmds.length);
  });
  it("offers the way back from replay", () => {
    expect(buildCommands({ ...ctx, replay: true }).some((c) => c.id === "mode:stream")).toBe(true);
  });
});

describe("derived numbers", () => {
  const pos = { open: true, qty: 65, avg: "29.45", bid: "30.45", stop_limit: "27.45", stop_trigger: "27.95", target: "31.70" };
  it("computes exposure, deployment, risk at stop and R from the payload only", () => {
    expect(exposure(pos)).toBeCloseTo(1914.25);
    expect(deployedFrac(pos, "10000")).toBeCloseTo(0.191425);
    expect(deployedFrac({ open: false }, "10000")).toBe(0);
    expect(deployedFrac(pos, null)).toBeNull();
    expect(riskAtStop(pos)).toBeCloseTo(130);
    expect(rMultiple(pos)).toBeCloseTo(0.5);
    expect(rMultiple({ ...pos, stop_limit: null })).toBeNull();
    const l = ladderFrac(pos)!;
    expect(l.entry).toBeCloseTo((29.45 - 27.95) / (31.7 - 27.95));
    expect(ladderFrac({ ...pos, bid: "40" })!.bid).toBe(1);
  });
  it("measures distance to the published triggers and VIX direction", () => {
    expect(triggerDistance("25000", { trigger_up: "25058", trigger_down: "24942" })).toEqual({ up: 58, down: 58 });
    expect(triggerDistance("25000", {})).toBeNull();
    expect(trend("16.8", "16.0")).toBe("up");
    expect(trend("16.1", "16.0")).toBe("flat");
    expect(trend(null, "16")).toBeNull();
  });
  it("summarises the ATM strike of the chain", () => {
    const s = chainSummary({ atm: 25000, rows: [{ strike: 25000, ce: { bid: "100", ask: "102", iv: "13" }, pe: { bid: "90", ask: "92", iv: "15" } }] })!;
    expect(s.straddle).toBe(192);
    expect(s.atmIv).toBe(14);
    expect(s.skew).toBe(2);
    expect(chainSummary({ atm: 1, rows: [] })).toBeNull();
  });
  it("places the trading-window rules on the session line", () => {
    const m = sessionMarks({ entry_start: "09:20:00", entry_cutoff: "14:00:00", flatten_start: "14:50:00", hard_flat: "15:00:00" });
    expect(m.map((x) => x.label)).toEqual(["Open", "Entries", "Last entry", "Flatten", "Hard flat", "Close"]);
    expect(nextMark(m, 14 * 60 + 10)?.label).toBe("Flatten");
    expect(nextMark(m, 15 * 60 + 31)).toBeNull();
  });
});

describe("riskBudgetState", () => {
  const base = { dailyLossFrac: 0.1, ddFrac: 0.01, ddWarnFrac: 0.1, ddSuspendFrac: 0.125, killLatched: false, halted: false };
  it("is NORMAL when the numbers are small", () => expect(riskBudgetState(base).label).toBe("NORMAL"));
  it("is ELEVATED at half the daily stop or the drawdown warning", () => {
    expect(riskBudgetState({ ...base, dailyLossFrac: 0.5 }).label).toBe("ELEVATED");
    expect(riskBudgetState({ ...base, ddFrac: 0.1 }).label).toBe("ELEVATED");
  });
  it("is BREACHED at the daily stop or the suspension level", () => {
    expect(riskBudgetState({ ...base, dailyLossFrac: 1 }).label).toBe("BREACHED");
    expect(riskBudgetState({ ...base, ddFrac: 0.13 }).label).toBe("BREACHED");
  });
  it("says entries are halted when a kill is latched, whatever the numbers", () => {
    expect(riskBudgetState({ ...base, killLatched: true }).label).toBe("ENTRIES HALTED");
    expect(riskBudgetState({ ...base, halted: true }).tone).toBe("neg");
  });
});

describe("chartWindow", () => {
  it("is the full session when the time is unknown", () => expect(chartWindow(null)).toEqual([555, 930]));
  it("is at least 90 minutes wide early in the day", () => expect(chartWindow(560)).toEqual([555, 645]));
  it("runs 30 minutes past now later on, and never past the close", () => {
    expect(chartWindow(720)).toEqual([555, 750]);
    expect(chartWindow(925)).toEqual([555, 930]);
  });
});
