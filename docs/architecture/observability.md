# 17 — Observability and Dashboard Design

## 17.1 Telemetry
- **Metrics (Prometheus):**
  - feed: tick rate, staleness per ATM strike, WS reconnects
  - DQ flag counts
  - latencies: decision → submit → ack → fill, histograms
  - order rejects by reason; rate-limit tokens
  - NAV, day P&L, DD from HWM, open risk, Greeks
  - kill-switch states; reconciliation age and mismatches
  - host: CPU, memory, disk, clock offset
- **Logs:** structured JSON; one `correlation_id` per intent carried through the Governor, OMS and broker calls.
- **Journal:** the system of record. Metrics and logs are derived views.

## 17.2 Dashboards (Grafana, read-only for the owner)
1. **Kernel health (live):** kill-switch panel (big red/green), reconciliation status, feed health heatmap (strike × staleness), latency p50/p99, order-reject log, clock offset.
2. **Risk:** NAV/HWM curve, DD gauges (daily 4%, weekly 8%, HWM 12.5/15%), open position with stop distance, risk-at-stop vs budget, Greeks.
3. **Execution quality:** per-trade intended vs fill, slippage in ticks and ₹, spread at decision, MFE/MAE, fill rate for passive limits.
4. **Strategy marketplace:** status by lifecycle stage, live vs validated expectancy CIs, capital eligibility (`min_capital_inr` vs NAV), drift PSI.
5. **Research:** experiment counts (trial accounting), validation pass/fail funnel.

## 17.3 Alerts (Grafana alerting; channel configured by the owner)
- **P1 (immediate):** any kill switch; reconciliation mismatch; order `UNKNOWN` > 10 s; position without confirmed protective stop > 3 s; broker session invalid during market hours; clock drift > 250 ms.
- **P2:** DQ degraded > 60 s; slippage > 2× on a trade; daily loss > 2%.
- **P3:** research/regulatory tickets, cost-model mismatch vs contract note.

## 17.4 Daily owner report (§17), generated post-market as Markdown/HTML + Streamlit page
```
Date | Mode (PAPER/SHADOW/CANARY/PROD) | Tomorrow: NORMAL | REDUCED RISK | HALTED (+ reason)
NAV ₹ | Day P&L ₹ (net; gross; charges; slippage) | Cumulative P&L | DD from HWM | Week/Month DD
Trades: n | per trade: strategy, contract, entry/exit, reason, net ₹, R, slippage ticks, attribution tag
Active strategies & lifecycle changes (promotions/demotions)
Best / worst attribution (STRATEGY/EXECUTION/RISK/DATA/REGIME/RANDOM_VARIANCE)
Confidence per live strategy (CI of expectancy)
Execution quality: avg slippage vs assumption, fill rate, latency p50/p99
Risk incidents & kill-switch events (with root cause if known)
Data quality summary
Research findings (from CIO report) – clearly labelled as not-yet-validated
Infrastructure costs month-to-date (data, VPS, API, LLM) vs trading P&L -> docs/risk/system-economics.md economics section (net of everything; advisory cost-justification check)
Regulatory watch: changes detected (Y/N)
```

**Built 2-Oct-2026 (K-14, SIMULATED):** `project100c.paper.owner_report(run, loop, econ)` renders every field above as Markdown from a paper run, with the economics section from `economics.render_markdown` on the run's own journal. A field whose component does not exist yet says so rather than being left out: attribution, the DQ summary on synthetic bars, slippage and latency on fake-broker fills, and the regulatory watch. The HTML/Streamlit page and the post-market runbook hook (K-12) remain.

## 17.5 'Jarvis' command centre (prototype, built 1-Oct-2026; v3 the same day; v3.1 neural hero that evening; v3.2 cognitive connectome that night; SIMULATED data only)
A read-only live view that sits alongside the Grafana dashboards in §17.2; it does not replace them.

**Unchanged since v1**
- Live mode is an SSE stream from a simulator that drives the real Governor and runtime harness against the fake broker, using seeded synthetic prices. Replay mode steps through fixed SIMULATED days.
- **Owner control: MANUAL_MASTER_KILL only**, in two steps: arm (a 30 s single-use nonce), then confirm with the typed word and a custom header. It goes to `KernelRuntime.manual_master_kill`. The server has no order endpoints and cannot import the broker or the kernel. `dashboard/src/api.ts` (the only network module) is unchanged since v2.
- The topology test still checks that no strategy or allocator path to the broker bypasses the Governor.

**v3 (1-Oct-2026, owner brief: "an autonomous trading command centre: calm intelligence, precision, control")**.
- **Layout.** A 56 px command bar, then three zones: Market context (left), the decision graph with Tony's NOW / WHY / NEXT under it, then the strategy pipeline and System Activity (centre), and Capital & Risk (right). At ≤1500 px the grid reflows. On ultrawide (≥3000 px) System Activity becomes a fourth column, so the extra width carries information rather than bigger graphics.
- **Design system first.**
  - `dashboard/src/design/tokens.css`: near-black #07090C, surfaces #0D1116 / #11161C, text #E8EDF2 / #8B96A3 / #56616D, one icy-cyan accent, green / amber / red used only for function, one violet for replay, 150–220 ms motion.
  - `components.css` and `layout.css`.
  - `src/ui/` builders: metric, kv, status, micro-bar, risk row, table, chart, activity item, drawer, palette, tooltip.
  - `src/model/`: pure view-models tested with vitest.
  - Fonts: Inter and JetBrains Mono, self-hosted from npm, with no CDN.
- **Tony** (`observability/dashboard/tony.py`) is the Trading CIO's name in the UI. It is **deterministic**: templates and rules over the simulator, Governor verdicts, journal, kill switches and economics, with no language model and no network. Unknown values are shown as "unknown".
  - **Confidence is not shown.** No strategy or simulator produces a calibrated confidence, so the panel says "not produced (rule-based signals; no calibrated model yet)".
  - **When Tony publishes.** A `TONY` event is emitted when the material state changes: level, NOW/WHY/NEXT, facts or broker link.
  - **Ask Tony (⌘K).** The answers to the fixed questions are in the payload. They open in a side drawer.
- **Attention system** (`tony.attention`, shown in the command bar and above NOW):
  - **INTERVENTION REQUIRED:** any kill switch or halt latched; a system-integrity failure; or an open position without a confirmed broker-side stop for more than 60 s.
  - **ATTENTION:** three Governor rejections in a row within 60 min; the cost-justification advisory below threshold; no tick for 5 s; broker link down; a position open within 30 min of the forced flatten; a stop unconfirmed for over 30 s; daily loss at 50% of the stop; or drawdown at the warning level.
  - **NORMAL:** none of the above. The structural cost notice at the canary NAV is shown but does not raise the level.
  - Tested in `tests/observability/test_tony.py` and by scenario in `test_simulator.py`.
- **Simulation honesty.** One global mode indicator: SIMULATION, or REPLAY · 08 OCT 2026 · 11:44:06. The per-card SIMULATED stamps are gone. `displayMode()` can only return LIVE when the server label is not SIMULATED, real events have been seen and no simulated event has been seen; the simulator never meets those conditions. Every event still carries `simulated: true`, and a test asserts it.
- **Lifecycle mapping.** The brief's columns are shown with the directive stage underneath; no state is invented:

  | Brief column | Directive stage(s) |
  |---|---|
  | Discovered | RESEARCH |
  | Researching | BACKTESTED, VALIDATED |
  | Paper | PAPER, SHADOW (simulate-only, no capital) |
  | Approved | CANARY |
  | Live | PRODUCTION |
  | Paused | DEGRADED, QUARANTINED |
  | Retired | RETIRED |

  A pytest checks that the columns cover every `Lifecycle` value exactly once. "Live" here is a pipeline stage label, not the mode indicator.
- **Decision graph.**
  - **Layout:** plain SVG. TONY · Trading CIO is at the centre, the pipeline is Market Intelligence → Data Quality → Allocator → Risk Governor → Execution → Broker, with Validation, Strategy Factory, Post-trade, and strategy satellites.
  - **Edges:** an edge lights only when a real event crosses it, with causal hop delays. A rejection trail ends at the Risk Governor in amber with a red cap. Approvals are cyan and fills are green.
  - **Kills:** a latched kill is drawn as a dashed red edge after the Governor.
  - **Motion:** no rotation and no random particles. Only Tony's halo breathes, and only while the system is alive.
- **Kill control.** A quiet "ARMED" item at the far right of the command bar, meaning the master kill is ready. Clicking it opens the emergency panel: prepare (arm), type `MANUAL_MASTER_KILL`, then execute. Once latched, the item reads "KILL LATCHED" in red.
- **Command palette (⌘/Ctrl+K, `/`).**
  - **Questions:** Ask-Tony questions.
  - **Navigation:** View NIFTY, option chain, positions, risk, kill switches, activity, raw logs.
  - **Modes:** replay days and voice.
  - **Controls:** Pause, Resume, Flatten, Change risk limits and Promote are **listed but disabled**, with "control actions not enabled in read-only mode".
- **Voiceover** (off by default). The browser's own `speechSynthesis` speaks Tony's NOW changes and attention-level changes. There is no external TTS and no network call. The setting is stored in `localStorage`.
- **Other changes kept from v2.**
  - The option chain is now a summary (ATM, straddle, ATM IV, skew) that expands to the strike table.
  - The event log is now System Activity, with an Activity | Raw logs toggle. Readable activity text comes from the simulator (`activity` on LOG events).
  - The price ladder, economics block, nine kill switches and session rules are all still present.
  - The snapshot now carries the session so far (minus CHAIN/RISK) on the same GET route, so a tab opened mid-session can draw the chart.
**v3.1 (1-Oct-2026 evening, owner brief: "I am watching the trading intelligence think")**. The centre SVG graph is replaced by a living neural field. Everything else on the v3 screen stays: command bar, market rail, NOW / WHY / NEXT, pipeline, System Activity, the capital and risk rail, positions, economics, drawers, ⌘K / Ask Tony, attention, replay and the read-only kill flow. `api.ts` and the route and no-order tests are unchanged.
- **What it is.** About 11,000 points in one organism, rendered with WebGL (three.js, bundled; `dashboard/src/neural/`).
  - **Lobes.** Six cognitive lobes: MARKET PERCEPTION, STRATEGY, RISK, PORTFOLIO, EXECUTION and LEARNING / RESEARCH. Each lobe is built from ganglia (dense knots of cells) joined by filaments.
  - **Tony and the broker.** Tony's dense core sits at the centre. The broker is a small external port at the right edge: it is not a cognitive region.
  - **Joining it up.** Faint connective tissue holds the lobes together. Fibre bundles run along the 12 region pairs that real flows use, and no others.
  - **Strategies.** Each strategy is its own small knotted motif inside STRATEGY.
  - **Depth and camera.** Depth fade is on. The camera has a gentle pointer parallax and no manual rotation.
  - **Labels.** Labels appear only near active, alarmed or hovered clusters. At rest each region keeps a faint name, which steps aside for any real annotation.
  - **Hover and click.** Hover reveals a region or a strategy pattern. A click zooms into the region and opens its operational layer (`screens/layers.ts`):
    - Perception → signals and data
    - Strategy → hypotheses
    - Risk → constraints and rejection rationale
    - Portfolio → allocation
    - Execution → broker, protective stop and fills
    - Research → experiments and candidates
    - The same layers are in ⌘K under "Neural field", and `?open=layer:RISK` links to one directly.
- **Tony's current thought.** The line "TONY · VERB · subject" sits close to the field.
  - **Where it comes from.** It comes from `tony.py` (`cognition` in the TONY payload). The verbs are:
    - OBSERVING · The open / NIFTY
    - WATCHING · Opening-range breakout · volatility compression / NIFTY
    - EXECUTING · S-ORB-001 entry order
    - MANAGING · S-ORB-001 / <symbol>
    - WAITING · No valid opportunity / S-ORB-001 stood down
    - HALTED · <latch>
    - RESTING · Session closed / flat
  - **While a chain plays.** While a real chain is playing, the line shows that step instead (`transientCognition`): EVALUATING for an intent or a rejection, EXECUTING for an approval, order or fill.
  - **Confidence.** No confidence figure appears anywhere, because nothing produces one. The brief's "73% confidence" is therefore not produced, and a test asserts that no label contains one.
- **Event → visual mapping** (`neural/mapping.ts`, renamed `connectome/mapping.ts` in v3.2; pure and unit-tested in `dashboard/test/connectome.test.ts`).
  - **Two layers.** `commandsFor(event)` gives the transient activations an event causes. `fieldState(state)` gives the standing state, recomputed from the folded stream, so a replay seek shows the same picture a live stream would at that moment.
  - **Brightness.** Brightness is activity. No command or state reads P&L, and a winning fill maps to exactly the same commands as a losing one. Confidence is not produced by any component, so no size, colour or label encodes it.

  | Real stream item | Flow `kind` (from → to) | Visual (region path, tone, strength) |
  |---|---|---|
  | TICK | `tick` broker → market_intel → data_quality; `clean` data_quality → strategies | BROKER → PERCEPTION pathway and a PERCEPTION fire, cyan, 0.10–0.40 (scaled by \|Δ\|); faint cyan feed shimmer on the three feed-receiving strategy motifs. No label. |
  | REGIME (every 5 min) | `regime` market_intel → cio → strategy_factory; `budget` cio → allocator | PERCEPTION → CORE, CORE → RESEARCH (cyan) and CORE → PORTFOLIO (white); PERCEPTION fires. The label "Market perception / <tags> · 30 min ±x% · VIX y" uses Market Intelligence's own tags. |
  | INTENT | `intent` strat:X → allocator → risk_governor | Motif X fires violet; STRATEGY → PORTFOLIO → RISK pathways in violet; RISK fires amber (evaluating). Labels: "X proposes BUY … (simulate-only)" at STRATEGY and "Risk / evaluating X · stop …" at RISK. |
  | DECISION, rejected | `reject` risk_governor → allocator | **terminate at RISK**: RISK flashes red, motif X decays to amber, nothing travels past RISK, and the label "Risk · rejected / X: <the Governor's own explain text>" appears. |
  | DECISION, approved (CANARY+) | `approve` risk_governor → execution | RISK → EXECUTION in green. The label "Risk · approved / X · risk at stop ₹a of ₹b" appears, then EXECUTION fires. |
  | DECISION, approved simulate-only (PAPER / SHADOW) | `approve` risk_governor → allocator | RISK → PORTFOLIO in green, labelled "approved, simulate-only · no order is sent". **Nothing reaches EXECUTION**, and Tony stays EVALUATING. |
  | ORDER | `order` execution → broker | EXECUTION → BROKER in cyan, with the label "Entry order sent / Protective stop sent / Exit order sent: side qty symbol @ price". |
  | FILL | `fill` broker → execution → post_trade | BROKER → EXECUTION in green (the broker confirmation returns), EXECUTION → RESEARCH, and PORTFOLIO fires green. Label: "Broker confirmed / filled q @ p". |
  | LOG: broker ack | `ack` broker → execution | BROKER → EXECUTION, green 0.6. |
  | LOG: post-trade / validation / factory | `report` post_trade → validation; `promote` strategy_factory → validation | Short-range firing inside RESEARCH (white, violet), with a "Learning / research" label quoting the activity text. |
  | KILLS (newly latched only) | `kill` risk_governor → cio, → execution | Only the switch's home region fires red and is labelled. A re-sent KILLS event with the same latch does nothing. |
  | TONY | — | The CORE fires faintly: cyan at NORMAL, amber at ATTENTION, red at INTERVENTION. |
  | ECONOMICS | — | A faint RESEARCH fire. |

  - **Kill switch → region.**
    - DATA_QUALITY_KILL and ABNORMAL_MARKET_KILL → PERCEPTION
    - STRATEGY_KILL → STRATEGY. A scoped kill affects only that strategy's motif, which turns red and goes dark.
    - PORTFOLIO_KILL → PORTFOLIO
    - DAILY_LOSS_KILL → RISK
    - BROKER_CONNECTIVITY_KILL and POSITION_RECONCILIATION_KILL → EXECUTION
    - SYSTEM_INTEGRITY_KILL and MANUAL_MASTER_KILL → CORE
  - **Standing states (`fieldState`).** A critical condition changes only the affected region's state; the UI as a whole never flashes red.
    - **alarm** (red): the latched kill's home region; RISK while a halt is active; CORE on an integrity failure.
    - **degraded** (amber): EXECUTION when the broker link is down (and the BROKER port goes quiet) or a protective stop is not yet confirmed; PERCEPTION when there has been no tick for ≥ 5 s.
    - **sustain**: an open position keeps PORTFOLIO (green) and EXECUTION lit.
    - **quiet**: every region after the close.
  - **Strategy motifs.** Resting brightness follows the lifecycle stage: RETIRED is nearly dark, then RESEARCH, BACKTESTED / VALIDATED, PAPER / SHADOW, CANARY and PRODUCTION; DEGRADED and QUARANTINED are amber. On top of that comes activity from the strategy's decisions in the last 120 s.
  - **Decision trail.** The last decision leaves a fading trail for 120 simulated seconds. A rejection runs STRATEGY → PORTFOLIO → RISK, with the last hop red, and its terminal is RISK. An approval runs on through EXECUTION to the broker in green. In a live stream the trail waits until the chain that caused it has finished playing.
  - **Causal timing.** The kernel decides intent → verdict → order → fill within one simulated instant (one timestamp). The `Choreographer` therefore plays those real steps one hop apart (700 ms per hop) in their true causal order. That order is strategy → allocator (PORTFOLIO) → Risk Governor (RISK) → execution → broker → execution. It differs from the brief's sketch (Strategy → Risk → Portfolio) because in this architecture the allocator sizes an intent before the Governor rules on it.
  - **Ambient motion.** The only motion without an event behind it is a very slow breathing (±16% over about 19 s) and a rare faint single-cell flicker. Both are off under `prefers-reduced-motion`, which also makes pathway pulses near-instant.
- **Semantic colour.** The field is mostly dim greys and cool cyan.
  - cyan = perception / system activity
  - soft violet (`--strategy`) = strategy / reasoning
  - amber = evaluation / uncertainty
  - green = approved / executed / confirmed
  - red = rejected / risk intervention
  - Replay keeps its own violet accent in the command bar only.
- **Performance and fallback.**
  - **Rendering.** devicePixelRatio is used, capped at 2. Drawing stops while `document.hidden`, the canvas resizes with a ResizeObserver, and there is one draw call each for points, synapses and sparks.
  - **Fallback.** If WebGL is unavailable, or with `?hero=svg`, the v3 SVG decision graph is used unchanged.
  - **Measured speed.** On the build box (no GPU; headless Chrome on SwiftShader software GL) it measured about 20 fps at 2560×1440, 29 fps at 1728×1117@2× and 16 fps at 3440×1440, with roughly 1.4–2.4 ms of JS-side render per frame. 60 fps on a real GPU has not been measured here.
- **Layout.** The hero spans the graph and brief rows, and NOW / WHY / NEXT floats over its lower edge (the camera centres the organism above it). The columns were rebalanced and the lower row reduced from 27% to 24% (23% ultrawide). The hero now takes about 42% of the app area at 2560×1440, 41% at the 16-inch MacBook and 40% at 3440×1440. No panel was removed.
- Screenshots: `docs/screenshots/v3.1/` (re-shoot with `dashboard/scripts/screenshots-v3.1.sh`).

**v3.2 (1-Oct-2026 night, owner brief: "evolve the neural field into a COGNITIVE CONNECTOME")**. The concept is renamed from NEURAL FIELD to **COGNITIVE CONNECTOME** in code (`dashboard/src/connectome/`: `regions.ts`, `mapping.ts`, `geometry.ts`, `renderer.ts`, `hero.ts`; tests in `dashboard/test/connectome.test.ts`), in the UI eyebrow and in ⌘K ("Connectome"). The rest of the screen is unchanged: command bar, both rails, NOW / WHY / NEXT, pipeline, replay controls and System Activity. `api.ts` and the route and no-order tests are unchanged.
- **One organism, three scales** (`geometry.ts`, pure and deterministic, seed 320; about 15,600 points and 15,600 segments for three strategies, built in ≈ 0.1 s).
  - **Macro.** The six regions, Tony's core and the broker port sit inside one bilateral density envelope (`cortexDensity`): two lobes, a lower-density but never empty midline that is bridged only at Tony's core, and curved gyral ridges. There is no outline anywhere; the silhouette exists only as density. Region tissue bleeds into about 6,800 cortical tissue cells, 560 defocused deep cells and 640 short association tracts that wrap around each lobe, so there are no dead gaps between regions.
  - **Meso.** Each region holds named assemblies (`regions.ts` `ASSEMBLIES`, 28 fixed plus one per strategy, up to 8). PERCEPTION: feed, data quality, regime, volatility, option chain. RISK: mandate, budget, headroom, position, window, market, kills. PORTFOLIO: sizing, budget, position. EXECUTION: entry, stop, exit, broker link. RESEARCH: post-trade, validation, factory, economics. CORE: posture, budget, attention, voice. BROKER: port.
  - **Micro.** The micro scale is made of cells, dendritic trees, intra-assembly synapses, micro-neurites on about 22% of the tissue cells, and nearest-neighbour synapse links.
  - **Per-region morphology.**
    - PERCEPTION has long, thin sensory dendrite fans at the left edge.
    - STRATEGY holds competing lobed knots, one per strategy.
    - RISK is a compact 3 + 2 + 2 gating lattice.
    - PORTFOLIO is an elongated allocation mesh between cognition and action.
    - EXECUTION is a short column with few, clear outbound bundles.
    - RESEARCH is a deep, diffuse upper sheet.
    - Tony's core is a dense, interconnected nexus rather than an orb: 4 irregular nuclei, 8 elongated sub-nuclei and about 3,200 short cross-links.
  - **Axonal bundles.** The 12 real pathways carry 6–26 filaments each (`FILAMENTS`). The filaments are Catmull-Rom curves that fasciculate in the middle and converge on specific assemblies at both ends. Long tracts loosen and meander. About 20% of filaments terminate early, 42% grow a collateral branch with a terminal bouton, and about 25% of those beyond the third are dormant (never lit).
  - **Routing around the core.** STRATEGY–PORTFOLIO is routed **through** Tony's core, and PORTFOLIO–RISK **around** its right side. That is how the brief's "CIO → Risk" and "CIO → Portfolio → Execution" are honoured: as routing only. The causal order the fibres carry is unchanged: Strategy → Portfolio sizing → Risk → Execution → Broker (→ Execution for the confirmation). Tony's core lights only for real regime, budget, kill and TONY events, plus the state-driven arousal described below.
- **Rendering (`renderer.ts`, GPU-driven).**
  - **State texture.** All activation, decay and afterglow is evaluated in the vertex shaders from a 64 × 9 float state texture (`DataTexture`, read with `texelFetch`). Its rows hold:
    - assembly fire and memory
    - assembly tone and sustain
    - region fire, tone and status
    - 4 impulse slots per pathway (start, duration, amplitude, direction, tone and filter assembly)
    - the decision trail
    - strategy patterns (formation, base, activity and tone)
  - **CPU work per frame.** The CPU writes a texel only when an event or the standing state changes. Each frame it only sets uniforms (time, arousal, camera). There is no per-point loop.
  - **Impulses.** An impulse has a front, a refractory dip and a tail, with per-filament asynchrony. Fibres that do not lead to the impulse's assembly carry only 20% of it, so a strategy's own filaments light up.
  - **Depth and optics.** There are three depth layers (foreground neurons, middle assemblies, deep tissue). Opacity fades with depth, and particle size varies slightly. Very restrained bloom is a soft halo term, and defocus grows a point's size while lowering its alpha. There is a slow camera drift (≤ 0.06) and a subtle pointer parallax, with no rotation or drag.
  - **Darkness.** At rest about 82% of vertices sit below `BARELY` (0.15 alpha); a test asserts 80–92%. In the label-free screenshots, 93–97% of hero pixels have luminance below 25/255.
  - **`?labels=off`.** Hides every annotation, the region names, Tony's thought line and the NOW / WHY / NEXT overlay, for the label-free test.
- **Event → visual mapping, v3.2 extension** (still the single source of truth: `commandsFor`, `fieldState`, `cognitiveState`, `strategyPattern`, all pure and unit-tested). The v3.1 flow table above stands. v3.2 makes each command land on a meso assembly:

  | Real stream item | v3.2 assembly activation |
  |---|---|
  | TICK (`tick`, `clean`) | P.FEED fires cyan. Each feed-receiving strategy assembly then fires faintly in violet (0.22): ideas competing for attention. The strength is `min(0.3, 0.08 + \|Δ\|/30)`. |
  | REGIME (`regime`, `budget`) | P.REGIME and P.VOL fire. The label reads "MARKET PERCEPTION / Range-bound · volatility compression / 30 min ±x% · VIX y", using Market Intelligence's own tags. The regime arrives at C.POSTURE, and the budget flow lands on PF.BUDGET. |
  | CHAIN | P.CHAIN. |
  | INTENT (`intent`) | The strategy's assembly fires violet ("Proposes …", and "simulate-only" for PAPER / SHADOW). PF.SIZING fires one hop later. RISK fires **amber** two hops later ("RISK / evaluating"). |
  | DECISION, rejected (`reject`) | **terminate at RISK.** Each assembly that maps from the Governor's reason codes fires red (`REASON_ASSEMBLY` covers all 39 `Reason` codes; for example `RISK_BUDGET_EXCEEDED` → R.BUDGET, `DAILY_HEADROOM` → R.HEADROOM). The strategy's assembly fades to amber. The label reads "RISK / REJECTED / Per-trade exposure exceeds the permitted budget / <strategy>: <the Governor's explain text>". PORTFOLIO, EXECUTION and the broker stay dark. Only PF.SIZING, which really sized the intent, has lit. |
  | DECISION, approved (`approve`) | RISK and R.BUDGET go green ("RISK / approved / risk at stop ₹a of ₹b"). EXECUTION fires green one hop later. In the simulate-only case only R.BUDGET goes green and nothing reaches EXECUTION. |
  | ORDER (`order`) / FILL (`fill`) / ack (`ack`) | ORDER lands on E.ENTRY, E.STOP or E.EXIT and travels EXECUTION → BROKER. The confirmation returns BROKER → EXECUTION in green. A FILL also fires PF.POSITION green two hops later. |
  | KILLS (`kill`, newly latched) | The home region fires, plus its assembly (`KILL_ASSEMBLY`): PORTFOLIO → PF.BUDGET, DAILY_LOSS → R.HEADROOM, DATA_QUALITY → P.DQ, BROKER_CONNECTIVITY_KILL → E.LINK, ABNORMAL_MARKET → P.VOL, POSITION_RECONCILIATION → E.LINK, SYSTEM_INTEGRITY and MANUAL_MASTER → C.ATTN. A scoped STRATEGY_KILL hits only S:<id>. |
  | LOG (`report`, `promote`) / ECONOMICS | RS.POST, RS.VALID and RS.FACTORY by actor; RS.ECON for ECONOMICS. |
  | TONY | C.ATTN 0.18 and C.VOICE 0.1, toned by attention. |
  | Standing (`fieldState`) | A latched kill's assembly is red (0.6) and R.KILLS is red while halted. E.LINK is amber when the broker is down, and P.FEED amber when the feed is stale. An open position keeps PF.POSITION green, and E.STOP green once the stop is confirmed (amber until then). |

  - **Arrivals.** The arrival fires that v3.1 queued in the hero now live in the mapping. A flow lands on its assembly one hop later at ×0.8, or a strong path fires the destination region at ×0.45.
- **The cognitive states (`cognitiveState(verb, phase, now)`).** They are derived from Tony's real cognition verb and the real chain step that played last. Nothing is timed for effect.

  | State | Driven by | Visual |
  |---|---|---|
  | `WATCHING` | Tony's OBSERVING, WATCHING, WAITING, MANAGING, RESTING or unknown, with no chain step in flight. | Dark and calm. PERCEPTION shows faint cyan from real ticks, and occasional low impulses reach the strategy assemblies. Tony's core has a slow resting pulse (period 4.8 s). |
  | `EVALUATING` | An INTENT (or a simulate-only approval), held 4 s after the step. | The active pathway Perception → Strategy → (through the core) Portfolio sizing → (around the core) Risk, in amber at Risk. Several strategy assemblies flicker from the competing ticks, and one fires. The core is more aroused (1.5 s). |
  | `REJECTED` | A rejecting DECISION, held 15 s. | Concentrated red at the rejecting Risk assemblies, with the pathway terminating there. PORTFOLIO, EXECUTION and the broker stay dark. The pattern decays through the afterglow rather than vanishing. |
  | `EXECUTING` | An approving DECISION, ORDER or FILL (held 12 s), or Tony's own EXECUTING verb. | Risk turns green, then Execution and the broker. The confirmation returns broker → execution. Everything else stays calm (core period 2.6 s). |
  | `HALTED` (extra) | Tony's HALTED (a latched kill). | It overrides the other states. The kill's region and assembly are red, and the core is subdued. |

  The state is published as `#graph[data-state]`. The verb in the thought line comes from `transientCognition`, which now says REJECTED for a rejection. After the step it reverts to Tony's own verb even while the visual state is still held.
- **Strategy thought patterns (`strategyPattern`).** Each strategy is a lobed micro-assembly at its own angle inside STRATEGY. `formation` (0 = scattered cells, 1 = formed) is applied in the shader. The states map honestly onto the directive lifecycle and the strategy's last real Governor decision:

  | Pattern | From | Formation / base / tone |
  |---|---|---|
  | `dormant` | RETIRED | 0.12 / 0.02 / white: barely visible |
  | `researching` | RESEARCH, BACKTESTED, VALIDATED | 0.5 / 0.10 / violet: subtle |
  | `candidate` | PAPER, SHADOW | 0.75 / 0.17 / violet: stronger formation |
  | `live` | CANARY, PRODUCTION | 1 / 0.22 / violet: persistent but restrained |
  | `paused` | DEGRADED, QUARANTINED | 0.5 / 0.08 / amber |
  | `killed` | a latched STRATEGY_KILL scoped to it | 0.3 / 0.06 / red, no activity |
  | `evaluating` | an APPROVE within the last 30 s | 1 / 0.36 / violet, actively firing |
  | `rejected` | a reject within the last 30 s | Starts at 0.9 / 0.30 / red and comes apart toward below its resting formation over 30 s. |

  The brief's six states are dormant, researching, candidate, evaluating, rejected-decaying and live. Paused and killed are added because the lifecycle has them.
- **Afterglow.** `afterglow(amp, dt) = amp·(e^(−dt/0.55) + 0.2·e^(−dt/6))`. Every activation has a fast flash and a 6 s memory term that stays visible for about 5–20 s. It is driven only by real activations: a firing moves the previous glow into a memory slot, capped at 0.5, so it never becomes a heatmap. Brightness is still activity, never P&L, and no confidence value is encoded anywhere.
- **Ambient motion.** Only a rare faint flicker, a slow breathing and the camera drift. All are off under `prefers-reduced-motion`, and the hidden-tab pause, devicePixelRatio (capped at 2) and the SVG fallback are kept.
- **Labels embedded in the field.** At rest, faint region names sit at 0.32 opacity. When a region activates, a short scientific annotation surfaces at the assembly that fired, with a hairline leader, in up to three lines (title / text / detail). The DOM is touched only when an annotation changes.
- **Layout (centre +10–15%).** The columns were reflowed to `16fr 36fr 25fr 17fr` (ultrawide ≥ 3000 px: `12fr 62fr 14fr 14fr`) and the lower row reduced to 21% (20%). No data was dropped. The centre's share of the app area is now **46.9% at 2560×1440** (v3.1: 42.4%, so +10.6%), 45.8% at 1728×1117 and 44.2% at 3440×1440, with no scroll.
  - **Backdrop blur.** NOW / WHY / NEXT lost its backdrop blur (0.9 opaque instead). Re-blurring the live canvas behind it every frame cost about 25% of frame time on the larger centre.
- **Performance** (build box, headless Chrome on SwiftShader with no GPU; replay at 10×; rAF fps over 6 s, A/B against the v3.1 build on the same box):

  | Size | v3.1 fps | v3.2 fps |
  |---|---|---|
  | 2560×1440 | 20.1 | 25.6 |
  | 1728×1117@2× | 28.6 | 29.3 |
  | 3440×1440 | 17.8 | 22.4 |

  JS per frame is about 0.3 ms in both. 60 fps on a real GPU has not been measured here.
- **Screenshots.** `docs/screenshots/v3.2/` holds:
  - the four states with labels off and on
  - full terminals at 1440p, the 16-inch MacBook and ultrawide
  - the replay broker-drop kill
  - the RISK focus
  - a strategy-assembly hover

  Re-shoot with `dashboard/scripts/screenshots-v3.2.sh`.

- Tests: `tests/observability/` (pytest) and `dashboard/test/` (vitest, also run from pytest). Screenshots: `docs/screenshots/v3/` (1440p, 16-inch MacBook, ultrawide, replay intervention, attention, palette with a Tony answer, position thesis).
- History: v1 had a three.js neural HUD; v2 brought minimalist styling, self-hosted fonts and the economics card; v3 rebuilt the design system and the information hierarchy and added Tony; v3.1 brought WebGL back, this time as an event-driven neural field in which every activation maps to a stream item. v3.2 evolved it into the cognitive connectome: one organism at three scales, axonal bundles, GPU-side afterglow, and four cognitive states derived from real events.
- Not done yet:
  - wiring to the real journal/metrics (K-13)
  - authentication (localhost-only for now)
  - latency and execution-quality panels
  - a real regime classifier (K-11); the tags shown are a simple SIMULATED rule
  - open interest and PCR (not in the simulator)
