// The ONLY module that talks to the server. Every URL the dashboard can call is listed here, and the test suite
// (tests/observability/test_frontend_build.py) checks that no other file calls fetch/EventSource and that this
// list contains no order-like endpoint. Read-only by construction: the single POST pair is MANUAL_MASTER_KILL.
import type { DashEvent, Snapshot, Topology } from "./types";

export const ENDPOINTS = {
  topology: "/api/topology",
  config: "/api/config",
  snapshot: "/api/snapshot",
  stream: "/api/stream",
  replayList: "/api/replay",
  replayDay: "/api/replay/",
  killArm: "/api/kill/arm",
  killConfirm: "/api/kill/confirm",
} as const;

const CONFIRM_HEADER = "X-P100C-Confirm";
const CONFIRM_WORD = "MANUAL_MASTER_KILL";

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url, { method: "GET", cache: "no-store" });
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status} ${await r.text()}`);
  return (await r.json()) as T;
}

export const api = {
  topology: () => getJSON<Topology>(ENDPOINTS.topology),
  config: () => getJSON<Record<string, unknown>>(ENDPOINTS.config),
  snapshot: () => getJSON<Snapshot>(ENDPOINTS.snapshot),
  replayList: () => getJSON<{ days: { name: string; day: string; title: string }[] }>(ENDPOINTS.replayList),
  replayDay: (name: string) =>
    getJSON<{ name: string; events: DashEvent[] }>(ENDPOINTS.replayDay + encodeURIComponent(name)),
  stream: (after: number, onEvent: (e: DashEvent) => void, onState: (s: "open" | "error") => void): EventSource => {
    const es = new EventSource(`${ENDPOINTS.stream}?after=${after}`);
    es.addEventListener("dash", (m) => onEvent(JSON.parse((m as MessageEvent).data) as DashEvent));
    es.onopen = () => onState("open");
    es.onerror = () => onState("error");
    return es;
  },
  killArm: async (): Promise<{ nonce: string; expires_in_s: number }> => {
    const r = await fetch(ENDPOINTS.killArm, { method: "POST", headers: { [CONFIRM_HEADER]: CONFIRM_WORD } });
    if (!r.ok) throw new Error(`arm failed: ${await r.text()}`);
    return r.json();
  },
  killConfirm: async (nonce: string, typed: string, reason: string): Promise<Record<string, unknown>> => {
    const r = await fetch(ENDPOINTS.killConfirm, {
      method: "POST",
      headers: { [CONFIRM_HEADER]: CONFIRM_WORD, "Content-Type": "application/json" },
      body: JSON.stringify({ nonce, confirm: typed, reason }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error ?? `HTTP ${r.status}`);
    return body;
  },
};
