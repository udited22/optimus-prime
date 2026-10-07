// Capture the docs/architecture/observability.md v3.1 screenshots (neural hero) from a running dashboard server
// (python -m project100c.observability.dashboard). The live shots need the server restarted at the right
// simulated minute; scripts/screenshots-v3.1.sh does that and calls this script once per shot.
// usage: node scripts/screenshots.mjs [baseUrl] [outDir] [name-prefix]   (needs a local Chrome; CHROME=/path/to/chrome)
//   BURST=n,everyMs  takes n frames (name-00.png …) so the frame where a real chain reaches a region can be picked
// Headless Chrome has no GPU here: WebGL runs on SwiftShader (software), so frame times are not representative.
import { chromium } from "playwright-core";
import { mkdirSync } from "node:fs";

const base = process.argv[2] ?? "http://127.0.0.1:8765";
const out = process.argv[3] ?? "../docs/screenshots/v3.1";
mkdirSync(out, { recursive: true });
const GL = ["--no-sandbox", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"];
const browser = await chromium.launch({ executablePath: process.env.CHROME ?? "/usr/bin/google-chrome", args: GL });
// [file, url, viewport w, h, devicePixelRatio, wait ms, selector to wait for]
const P1440 = [2560, 1440, 1];
const MBP16 = [1728, 1117, 2];
const ULTRA = [3440, 1440, 1];
const shots = [
  // live (server restarted by the .sh at the right minute)
  ["01-live-rest-quiet.png", "/?mode=live&perf=1", ...P1440, 4000],
  ["02-live-approved-to-execution.png", "/?mode=live&perf=1", ...P1440, 6500],
  // replay (any server)
  ["03-replay-risk-rejection.png", "/?mode=replay&day=normal-2&at=11:59:58&speed=10&perf=1", ...P1440, 500],
  ["04-replay-broker-drop-kill.png", "/?mode=replay&day=broker-drop&at=11:40:20&autoplay=0&perf=1", ...P1440, 3000],
  ["05-hover-label.png", "/?mode=replay&day=normal-1&at=10:20:00&autoplay=0&hover=RISK", ...P1440, 2500],
  ["06-region-focus-risk.png", "/?mode=replay&day=normal-2&at=12:00:30&autoplay=0&open=layer:RISK", ...P1440, 3500],
  // the three reference sizes, live with a position open
  ["07-live-1440p.png", "/?mode=live&perf=1", ...P1440, 3000, "#pos-card"],
  ["08-live-macbook16.png", "/?mode=live&perf=1", ...MBP16, 3000, "#pos-card"],
  ["09-live-ultrawide.png", "/?mode=live&perf=1", ...ULTRA, 3000, "#pos-card"],
];
const [bn, bevery] = (process.env.BURST ?? "1,0").split(",").map(Number);
for (const [name, path, w, h, dpr, wait, sel] of process.argv[4] ? shots.filter((s) => s[0].startsWith(process.argv[4])) : shots) {
  const page = await browser.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: dpr });
  page.on("console", (m) => m.type() === "error" && console.error("page:", m.text()));
  page.on("pageerror", (e) => console.error("pageerror:", e.message));
  await page.goto(base + path, { waitUntil: "networkidle" });
  await page.waitForSelector("body.ready", { timeout: 30000 });
  if (sel) await page.waitForSelector(sel, { timeout: 120000 });
  if (!path.includes("hover=")) await page.mouse.move(w - 5, h - 5); // no pointer hover in the shot
  await page.waitForTimeout(wait);
  for (let i = 0; i < bn; i++) {
    const file = bn > 1 ? name.replace(".png", `-${String(i).padStart(2, "0")}.png`) : name;
    await page.screenshot({ path: `${out}/${file}` });
    const info = await page.evaluate(() => (document.querySelector(".nf-thought")?.textContent ?? "") + " | " + JSON.stringify(window.__hero?.stats ?? {}));
    console.log("saved", `${out}/${file}`, `${w}x${h}@${dpr}x`, info);
    if (bevery) await page.waitForTimeout(bevery);
  }
  await page.close();
}
await browser.close();
