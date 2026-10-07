"""Basic frontend checks: the only network calls are the whitelisted read-only API + the kill pair, the screen is
labelled SIMULATED, and (when node_modules is installed) `npm run build` produces dist/."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from project100c.observability.dashboard.server import ROUTES, match_route

ROOT = Path(__file__).resolve().parents[2] / "dashboard"
SRC = ROOT / "src"
ORDERISH = re.compile(r"order|trade|buy|sell|place|modify|cancel|exit|flatten|broker|submit", re.I)


def _sources() -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(SRC.glob("*.ts"))}


def test_only_api_ts_touches_the_network() -> None:
    for name, text in _sources().items():
        if name == "api.ts":
            continue
        for needle in ("fetch(", "EventSource(", "XMLHttpRequest", "WebSocket(", "sendBeacon"):
            assert needle not in text, (name, needle)


def test_every_frontend_endpoint_is_a_server_route_and_none_is_order_like() -> None:
    api = _sources()["api.ts"]
    block = api[api.index("export const ENDPOINTS") : api.index("} as const")]
    urls = re.findall(r'"(/api/[^"]*)"', block)
    assert len(urls) == 8
    for u in urls:
        assert not ORDERISH.search(u), u
        probe = u + "x" if u.endswith("/") else u
        assert match_route("GET", probe)[0] or match_route("POST", probe)[0], u
    posts = {u for u in urls if match_route("POST", u)[0]}
    assert posts == {"/api/kill/arm", "/api/kill/confirm"}
    assert {r.path for r in ROUTES if r.method == "POST"} == posts
    assert api.count('method: "POST"') == 2  # arm + confirm, nothing else


def test_screen_is_labelled_simulated_and_the_kill_needs_typed_confirmation() -> None:
    src = _sources()
    assert src["hud.ts"].count("SIMULATED") >= 5
    assert 'input.value !== "MANUAL_MASTER_KILL"' in src["main.ts"]
    assert "MANUAL_MASTER_KILL" in src["hud.ts"]


def _all_sources() -> dict[str, str]:
    """Every TypeScript module under src/ (v3 splits the UI into design/, model/, ui/ and screens/)."""
    return {str(p.relative_to(SRC)): p.read_text() for p in sorted(SRC.rglob("*.ts"))}


def _css() -> dict[str, str]:
    return {str(p.relative_to(SRC)): p.read_text() for p in sorted(SRC.rglob("*.css"))}


SVG_NS = "http://www.w3.org/2000/svg"


def test_no_module_anywhere_under_src_touches_the_network() -> None:
    """The top-level check above predates the v3 sub-folders; this one walks all of src/. Only api.ts may call
    the server, and nothing may load a remote URL (the SVG namespace string is not a fetch)."""
    srcs = _all_sources()
    assert {"graph.ts", "voice.ts", "ui/palette.ts", "screens/centre.ts", "model/mode.ts"} <= set(srcs)
    for name, text in srcs.items():
        if name == "api.ts":
            continue
        for needle in ("fetch(", "EventSource(", "XMLHttpRequest", "WebSocket(", "sendBeacon", "import("):
            assert needle not in text, (name, needle)
        assert not re.search(r"https?://", text.replace(SVG_NS, "")), name


def test_fonts_are_self_hosted_and_the_ui_is_sharp_and_restrained() -> None:
    """UI v3 (1-Oct-2026): Inter + JetBrains Mono bundled from npm (no CDN at runtime), tabular numerals from the
    design tokens, no glow on text. v3.1 brings three.js back for the hero only (bundled, no CDN; the v3.2
    cognitive connectome); the v3 SVG decision graph stays as the fallback."""
    src = _all_sources()
    css = _css()
    html = (ROOT / "index.html").read_text()
    pkg = json.loads((ROOT / "package.json").read_text())
    deps = pkg["dependencies"]
    assert {"@fontsource-variable/inter", "@fontsource-variable/jetbrains-mono"} <= set(deps)
    assert not any("orbitron" in d or "share-tech" in d for d in deps)
    assert '"@fontsource-variable/inter/index.css"' in src["main.ts"]
    assert '"@fontsource-variable/jetbrains-mono/index.css"' in src["main.ts"]
    for name, text in [*src.items(), *css.items(), ("index.html", html)]:
        assert not re.search(r"https?://|//fonts\.|@import\s+url", text.replace(SVG_NS, "")), name  # no CDN
    assert "createElementNS" in src["graph.ts"] and "getContext(" not in src["graph.ts"]
    assert not (SRC / "brain.ts").exists() and not (SRC / "style.css").exists()
    assert {"design/tokens.css", "design/components.css", "design/layout.css"} <= set(css)
    assert "tabular-nums" in css["design/tokens.css"] + css["design/components.css"]
    for name, text in css.items():
        assert "text-shadow" not in text and "drop-shadow" not in text, name


def test_connectome_hero_is_webgl_sharp_pausable_and_falls_back_to_the_svg_graph() -> None:
    """v3.1 (1-Oct-2026): the centre hero is WebGL (three.js, bundled) that uses devicePixelRatio, stops drawing
    while the tab is hidden, honours prefers-reduced-motion, and falls back to the v3 SVG graph. v3.2 renames it
    the cognitive connectome: activation, decay and afterglow live in a GPU state texture read by the shaders (no
    per-point CPU loop), and ``?labels=off`` hides every annotation for the label-free test."""
    src = _all_sources()
    pkg = json.loads((ROOT / "package.json").read_text())
    assert "three" in pkg["dependencies"]
    assert not (SRC / "neural").exists()
    ren, hero = src["connectome/renderer.ts"], src["connectome/hero.ts"]
    assert "WebGLRenderer" in ren and "setPixelRatio" in ren and "devicePixelRatio" in ren
    assert "DataTexture" in ren and "texelFetch" in ren  # state-driven on the GPU
    assert "document.hidden" in hero and "visibilitychange" in hero
    assert "prefers-reduced-motion" in hero
    assert "createElementNS" in src["graph.ts"] and "getContext(" not in src["graph.ts"]
    assert "new DecisionGraph(" in hero and 'force !== "svg"' in hero and "supported()" in hero
    assert 'qs.get("labels") !== "off"' in src["main.ts"] and "labels-off" in hero
    # only the renderer imports three; the event mapping and the geometry are pure (no WebGL, unit-tested)
    for name, text in src.items():
        if re.search(r'from "three"', text):
            assert name == "connectome/renderer.ts", name
    for pure in ("connectome/mapping.ts", "connectome/geometry.ts", "connectome/regions.ts"):
        assert "Math.random" not in src[pure] and "Date.now" not in src[pure], pure
    assert (ROOT / "test" / "connectome.test.ts").exists()


def test_event_to_visual_mapping_is_documented_in_design_17() -> None:
    """The brief's rule: every pulse, pathway, colour and cluster state maps to a real event or state. The table
    lives in docs/architecture/observability.md §17.5 (v3.1, extended for the v3.2 connectome) and names the same flows,
    cognitive states
    and strategy-pattern states as connectome/mapping.ts."""
    doc = (ROOT.parent / "docs" / "architecture" / "observability.md").read_text()
    assert "Event → visual mapping" in doc and "COGNITIVE CONNECTOME" in doc
    section = doc[doc.index("Event → visual mapping") :]
    for needle in (
        "commandsFor",
        "fieldState",
        "terminate",
        "RISK",
        "EXECUTION",
        "BROKER_CONNECTIVITY_KILL",
        "simulate-only",
        "not produced",
        "cognitiveState",
        "strategyPattern",
        "afterglow",
        "labels=off",
    ):
        assert needle in section, needle
    mapping = _all_sources()["connectome/mapping.ts"]
    for kind in ("tick", "regime", "budget", "intent", "approve", "order", "ack", "fill", "report", "promote", "kill"):
        assert f"{kind}:" in mapping, kind
        assert f"`{kind}`" in section, kind
    for state in ("WATCHING", "EVALUATING", "REJECTED", "EXECUTING", "HALTED"):
        assert f'"{state}"' in mapping and f"`{state}`" in section, state
    for pattern in ("dormant", "researching", "candidate", "live", "paused", "killed", "evaluating", "rejected"):
        assert f"{pattern}:" in mapping and f"`{pattern}`" in section, pattern


def test_one_global_mode_indicator_and_live_is_impossible_while_simulated() -> None:
    """Constraint 4: the per-card SIMULATED stamps are gone; one mode indicator lives in the command bar, and the
    LIVE wording is gated behind displayMode(), which needs real (non-simulated) events and no simulated ones."""
    src = _all_sources()
    hud = src["hud.ts"]
    assert 'id="mode-ind"' in hud and "modeText(" in hud
    assert "displayMode({" in src["main.ts"]  # the command bar's mode comes only from the guarded function
    for name, text in src.items():
        assert "ph-sim" not in text, name  # the v2 per-panel badge class
    mode = src["model/mode.ts"]
    assert 'if (serverSaysReal && m.realSeen > 0 && m.simulatedSeen === 0) return "LIVE";' in mode
    assert mode.count('return "LIVE"') == 1
    assert "SIMULAT" in mode


def test_palette_control_actions_are_disabled_with_the_read_only_reason() -> None:
    pal = _all_sources()["model/palette.ts"]
    assert 'READ_ONLY_REASON = "control actions not enabled in read-only mode"' in pal
    for label in ("Pause strategy", "Resume strategy", "Flatten open position"):
        assert label in pal
    assert "disabled: READ_ONLY_REASON" in pal


def test_voiceover_uses_only_the_browser_speech_synthesis_and_is_off_by_default() -> None:
    voice = _all_sources()["voice.ts"]
    assert "speechSynthesis" in voice and "SpeechSynthesisUtterance" in voice
    assert 'localStorage.getItem(KEY) === "on"' in voice  # anything else (including unset) means off


def test_economics_card_is_labelled_and_advisory_only() -> None:
    """docs/risk/system-economics.md card: fed by the ECONOMICS event over the existing stream (no new endpoint), and it
    says on screen
    that the cost-justification advisory is not a kill switch. v3 moved it into the right rail (screens/capital.ts)."""
    src = _all_sources()
    assert '"ECONOMICS"' in src["types.ts"] and 'case "ECONOMICS"' in src["state.ts"]
    assert 'regionHead("Economics · net of everything"' in src["screens/capital.ts"]
    assert "not a kill switch" in src["screens/capital.ts"]
    assert "economics" not in src["api.ts"].lower()


def test_lifecycle_columns_map_onto_every_directive_stage_exactly_once() -> None:
    """Constraint 5: The owner's pipeline labels sit on top of the directive's real stages; no invented stage."""
    from project100c.spec.models import Lifecycle

    lc = _all_sources()["model/lifecycle.ts"]
    staged = re.findall(r"stages: \[([^\]]*)\]", lc)
    names = [x.strip().strip('"') for block in staged for x in block.split(",") if x.strip()]
    assert sorted(names) == sorted(s.value for s in Lifecycle)


@pytest.mark.skipif(
    shutil.which("npm") is None or not (ROOT / "node_modules" / "vitest").is_dir(),
    reason="npm or dashboard/node_modules/vitest missing (run `cd dashboard && npm install`)",
)
def test_frontend_unit_tests_pass() -> None:
    r = subprocess.run(["npm", "test", "--silent"], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(
    shutil.which("npm") is None or not (ROOT / "node_modules").is_dir(),
    reason="npm or dashboard/node_modules missing (run `cd dashboard && npm install`)",
)
def test_npm_build_produces_dist(tmp_path: Path) -> None:
    out = tmp_path / "dist"
    r = subprocess.run(
        ["npm", "run", "build", "--", "--outDir", str(out), "--emptyOutDir"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    html = (out / "index.html").read_text()
    assert "SIMULATED" in html and "/assets/" in html
    css = list((out / "assets").glob("*.css"))
    assert css and all("http" not in p.read_text() for p in css)  # fonts resolve to bundled /assets/*.woff2
    assert list((out / "assets").glob("inter-latin-wght-normal-*.woff2"))
    assert list((out / "assets").glob("jetbrains-mono-latin-wght-normal-*.woff2"))
    js = list((out / "assets").glob("*.js"))
    assert js and any("MANUAL_MASTER_KILL" in p.read_text() for p in js)
