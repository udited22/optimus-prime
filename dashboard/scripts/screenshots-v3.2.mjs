// Capture the docs/architecture/observability.md v3.2 screenshots (cognitive connectome) from a running SIMULATED dashboard server
// (python -m project100c.observability.dashboard). scripts/screenshots-v3.2.sh restarts the server at the simulated
// minute each live shot needs and calls this script once per shot group.
// usage: node scripts/screenshots-v3.2.mjs <baseUrl> <absolute outDir> <shot-prefix>   (CHROME=/path/to/chrome)
// State shots wait until the hero reports the cognitive state (#graph[data-state]), freeze the hero clock, and save the
// same frame twice: labels off (the visual alone) and labels on. PERF=1 also prints frames per second and the mean
// JS render time per frame over 5 s, and the centre's share of the app area.
// Headless Chrome has no GPU here: WebGL runs on SwiftShader (software), so absolute frame times are pessimistic.
import { chromium } from "playwright-core";
import { mkdirSync } from "node:fs";

const base = process.argv[2] ?? "http://127.0.0.1:8765";
const out = process.argv[3];
if (!out || !out.startsWith("/")) throw new Error("outDir must be an absolute path");
mkdirSync(out, { recursive: true });
const GL = ["--no-sandbox", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"];
const browser = await chromium.launch({ executablePath: process.env.CHROME ?? "/usr/bin/google-chrome", args: GL });
const P1440 = [2560, 1440, 1];
const MBP16 = [1728, 1117, 2];
const ULTRA = [3440, 1440, 1];
// [prefix, url, w, h, dpr, states: [file, state, ms after the state begins] | null, wait ms, selector]
const shots = [
  ["01", "/?mode=replay&day=normal-1&at=10:20:00&speed=10&perf=1", ...P1440, [["01-watching", "WATCHING", 5200]]],
  ["02", "/?mode=live&perf=1", ...P1440, [["02-evaluating", "EVALUATING", 1600], ["04-executing", "EXECUTING", 2300]]],
  ["03", "/?mode=replay&day=normal-2&at=11:59:58&speed=10&perf=1", ...P1440, [["03-rejected", "REJECTED", 1500]]],
  ["05", "/?mode=live&perf=1", ...P1440, null, 3500, "#pos-card", "05-terminal-1440p.png"],
  ["06", "/?mode=live&perf=1", ...MBP16, null, 3500, "#pos-card", "06-terminal-macbook16.png"],
  ["07", "/?mode=live&perf=1", ...ULTRA, null, 3500, "#pos-card", "07-terminal-ultrawide.png"],
  ["08", "/?mode=replay&day=broker-drop&at=11:40:20&autoplay=0&perf=1", ...P1440, null, 3000, null, "08-replay-broker-drop-kill.png"],
  ["09", "/?mode=replay&day=normal-2&at=12:00:30&autoplay=0&open=layer:RISK&perf=1", ...P1440, null, 3500, null, "09-region-focus-risk.png"],
  ["10", "/?mode=replay&day=normal-1&at=10:20:00&autoplay=0&hover=STRATEGY:0&perf=1", ...P1440, null, 2500, null, "10-hover-strategy-assembly.png"],
];
const pick = process.argv[4];
for (const [prefix, path, w, h, dpr, states, wait, sel, file] of shots.filter((s) => !pick || s[0].startsWith(pick))) {
  const page = await browser.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: dpr });
  page.on("console", (m) => m.type() === "error" && console.error("page:", m.text()));
  page.on("pageerror", (e) => console.error("pageerror:", e.message));
  await page.goto(base + path, { waitUntil: "networkidle" });
  await page.waitForSelector("body.ready", { timeout: 30000 });
  if (sel) await page.waitForSelector(sel, { timeout: 120000 });
  if (!path.includes("hover=")) await page.mouse.move(w - 5, h - 5); // no pointer hover in the shot
  if (states) {
    for (const [name, state, after] of states) {
      await page.waitForFunction((s) => document.querySelector("#graph")?.dataset.state === s, state, { timeout: 120000, polling: 50 });
      await page.waitForTimeout(after);
      await page.evaluate(() => window.__hero.freeze(true));
      await page.waitForTimeout(250);
      await page.evaluate(() => window.__hero.setLabels(false));
      await page.waitForTimeout(200);
      // the visual: the hero above the NOW / WHY / NEXT band (the band itself is not part of the connectome)
      const clip = await page.evaluate(() => {
        const g = document.querySelector("#r-graph").getBoundingClientRect();
        const b = document.querySelector("#r-brief").getBoundingClientRect();
        return { x: g.x, y: g.y, width: g.width, height: Math.max(100, b.y - g.y - 8) };
      });
      await page.screenshot({ path: `${out}/${name}-labels-off.png`, clip });
      await page.evaluate(() => window.__hero.setLabels(true));
      await page.waitForTimeout(450);
      await page.screenshot({ path: `${out}/${name}-labels-on.png`, clip });
      const info = await page.evaluate(() => document.querySelector(".nf-thought")?.textContent ?? "");
      console.log("saved", name, state, info);
      await page.evaluate(() => window.__hero.freeze(false));
    }
  } else {
    await page.waitForTimeout(wait);
    await page.screenshot({ path: `${out}/${file}` });
    console.log("saved", file, `${w}x${h}@${dpr}x`);
  }
  if (process.env.PERF === "1") {
    const m = await page.evaluate(async () => {
      const s0 = { ...window.__hero.stats };
      let n = 0;
      const t0 = performance.now();
      await new Promise((res) => {
        const f = () => (++n, performance.now() - t0 < 5000 ? requestAnimationFrame(f) : res(null));
        requestAnimationFrame(f);
      });
      const s1 = window.__hero.stats;
      const dt = (performance.now() - t0) / 1000;
      const frames = s1.frames - s0.frames;
      const js = (s1.meanRenderMs * s1.frames - s0.meanRenderMs * s0.frames) / Math.max(1, frames);
      const g = document.querySelector("#r-graph").getBoundingClientRect();
      const a = document.querySelector(".app").getBoundingClientRect();
      return { fps: +(n / dt).toFixed(1), jsRenderMs: +js.toFixed(2), points: s1.points, segments: s1.segments, centreShare: +((g.width * g.height) / (a.width * a.height)).toFixed(3), scroll: document.documentElement.scrollHeight > innerHeight };
    });
    console.log("perf", prefix, `${w}x${h}@${dpr}x`, JSON.stringify(m));
  }
  await page.close();
}
await browser.close();
