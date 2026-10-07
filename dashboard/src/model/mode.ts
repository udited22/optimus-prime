// The one global mode indicator (constraint: LIVE must be impossible while the backend is simulated).
// The dashboard knows two transports (the server's stream and a recorded replay); the *label* it shows is
// derived here from the data itself, never from the transport the operator picked.

export type DisplayMode = "SIMULATION" | "REPLAY" | "LIVE";

export interface ModeInputs {
  /** the operator is watching a recorded day */
  replay: boolean;
  /** label the server sends with /api/config and every snapshot ("SIMULATED" today) */
  serverLabel: string | null;
  /** events seen so far whose `simulated` flag is not exactly false */
  simulatedSeen: number;
  /** events seen so far whose `simulated` flag is exactly false */
  realSeen: number;
}

/** LIVE needs positive evidence on every axis: a non-replay transport, a server that does not call itself
 *  SIMULATED, at least one event, and not a single simulated event. Anything else is SIMULATION (or REPLAY). */
export function displayMode(m: ModeInputs): DisplayMode {
  if (m.replay) return "REPLAY";
  const serverSaysReal = m.serverLabel !== null && !/SIMULAT/i.test(m.serverLabel);
  if (serverSaysReal && m.realSeen > 0 && m.simulatedSeen === 0) return "LIVE";
  return "SIMULATION";
}

const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

/** "SIMULATION · 05 OCT 2026 · 09:55:12" from an ISO timestamp with offset (shown in IST). */
export function modeText(mode: DisplayMode, ts: string | null): string {
  if (!ts) return mode;
  const d = new Date(Date.parse(ts) + 5.5 * 3600_000); // IST wall clock, independent of the browser's zone
  const dd = String(d.getUTCDate()).padStart(2, "0");
  const hms = [d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds()].map((x) => String(x).padStart(2, "0")).join(":");
  return `${mode} · ${dd} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()} · ${hms}`;
}
