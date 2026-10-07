export type Kind =
  | "SESSION" | "TICK" | "CHAIN" | "REGIME" | "STRATEGIES" | "INTENT" | "DECISION" | "ORDER" | "FILL"
  | "POSITION" | "RISK" | "KILLS" | "WINDOW" | "LOG" | "DAY_END" | "ECONOMICS" | "TONY";

export interface DashEvent {
  seq: number;
  ts: string;
  kind: Kind;
  simulated: boolean;
  label: string;
  data: any; // eslint-disable-line @typescript-eslint/no-explicit-any
  flows: [string, string, string][];
}

export interface Snapshot {
  last_seq: number;
  latest: Partial<Record<Kind, DashEvent>>;
  log: DashEvent[];
  /** every event since the latest SESSION, minus CHAIN and RISK (server.py) */
  session?: DashEvent[];
  label: string;
}

export interface TopoNode { id: string; label: string; role: string; blurb: string }
export interface Topology {
  nodes: TopoNode[];
  edges: [string, string][];
  strategies: { id: string; hypothesis: string; name: string; stage: string; node: string }[];
}
