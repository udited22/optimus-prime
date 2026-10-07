// Tony's optional voiceover. Off by default; uses only the browser's built-in Web Speech API
// (window.speechSynthesis), so nothing leaves the machine and no external TTS service is involved.
// It speaks Tony's NOW line when it changes and every change of the attention state.

const KEY = "p100c.voice";

export class Voice {
  on = false;
  private lastNow = "";
  private lastLevel = "";
  private lastSpoke = 0;

  constructor() {
    try {
      this.on = localStorage.getItem(KEY) === "on";
    } catch {
      this.on = false;
    }
    if (!this.supported) this.on = false;
  }

  get supported(): boolean {
    return typeof window !== "undefined" && "speechSynthesis" in window && typeof SpeechSynthesisUtterance !== "undefined";
  }

  set(on: boolean): void {
    this.on = on && this.supported;
    try {
      localStorage.setItem(KEY, this.on ? "on" : "off");
    } catch {
      /* private mode: the toggle still works for this tab */
    }
    if (!this.on) window.speechSynthesis?.cancel();
    else this.say("Voiceover on. I will read my current status and any change in attention.", true);
  }

  /** Feed every Tony payload; speaks only on a change, and never while the operator is scrubbing fast. */
  observe(tony: { now?: string; attention?: { level?: string; headline?: string } } | null, quiet: boolean): void {
    if (!tony) return;
    const level = tony.attention?.level ?? "";
    const now = tony.now ?? "";
    const levelChanged = this.lastLevel !== "" && level !== this.lastLevel;
    const nowChanged = this.lastNow !== "" && now !== this.lastNow;
    this.lastLevel = level;
    this.lastNow = now;
    if (!this.on || quiet) return;
    if (levelChanged) {
      const word = level === "INTERVENTION" ? "Intervention required" : level === "ATTENTION" ? "Attention" : "Back to normal";
      this.say(`${word}. ${tony.attention?.headline ?? ""}`, true);
    } else if (nowChanged && performance.now() - this.lastSpoke > 3000) this.say(now, false);
  }

  /** reset the change detector (new session, seek) so a jump does not read out a backlog */
  prime(tony: { now?: string; attention?: { level?: string } } | null): void {
    this.lastNow = tony?.now ?? "";
    this.lastLevel = tony?.attention?.level ?? "";
  }

  private say(text: string, urgent: boolean): void {
    if (!this.supported) return;
    const s = window.speechSynthesis;
    if (urgent) s.cancel();
    const u = new SpeechSynthesisUtterance(text.replaceAll("₹", "rupees ").replaceAll("·", ","));
    u.rate = 1.02;
    u.pitch = 0.95;
    u.lang = "en-IN";
    const v = s.getVoices().find((x) => x.localService && x.lang.startsWith("en"));
    if (v) u.voice = v;
    s.speak(u);
    this.lastSpoke = performance.now();
  }
}
