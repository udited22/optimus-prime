const IST = "Asia/Kolkata";
const tf = new Intl.DateTimeFormat("en-GB", { timeZone: IST, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const df = new Intl.DateTimeFormat("en-GB", { timeZone: IST, weekday: "short", day: "2-digit", month: "short", year: "numeric" });

export const istTime = (ts: string | null | undefined): string => (ts ? tf.format(new Date(ts)) : "--:--:--");
export const istHM = (ts: string | null | undefined): string => istTime(ts).slice(0, 5);
export const istDate = (ts: string | null | undefined): string => (ts ? df.format(new Date(ts)) : "");
/** minutes since IST midnight */
export const istMinutes = (ts: string): number => {
  const [h, m, s] = istTime(ts).split(":").map(Number);
  return h * 60 + m + s / 60;
};
export const minToHM = (m: number): string => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(Math.floor(m % 60)).padStart(2, "0")}`;
export const num = (x: unknown, dp = 2): string => {
  if (x === null || x === undefined || x === "") return "—";
  const v = Number(x);
  return Number.isFinite(v) ? v.toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp }) : "—";
};
export const inr = (x: unknown, dp = 2): string => {
  if (x === null || x === undefined || x === "") return "—";
  const v = Number(x);
  if (!Number.isFinite(v)) return "—";
  return `${v < 0 ? "−" : ""}₹${num(Math.abs(v), dp)}`;
};
export const signed = (x: unknown, dp = 2): string => {
  const v = Number(x);
  if (x === null || x === undefined || !Number.isFinite(v)) return "—";
  return (v > 0 ? "+" : v < 0 ? "−" : "") + num(Math.abs(v), dp);
};
/** +₹234.96 / −₹12.00 */
export const inrSigned = (x: unknown, dp = 2): string => {
  const v = Number(x);
  if (x === null || x === undefined || !Number.isFinite(v)) return "—";
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}₹${num(Math.abs(v), dp)}`;
};
export const pct = (x: unknown, dp = 2): string => (x === null || x === undefined || !Number.isFinite(Number(x)) ? "—" : `${num(Number(x) * 100, dp)}%`);
export const pctSigned = (x: unknown, dp = 2): string => (x === null || x === undefined ? "—" : `${signed(Number(x) * 100, dp)}%`);
export { esc } from "./ui/dom";
