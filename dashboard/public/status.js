// The public status page: reads the engine's public view (/api/public/status, through a same-origin proxy) and
// writes it with textContent only. It has no other network call and no way to send anything.
const FIELDS = [
  ["mode", "Mode"],
  ["real_money", "Real money"],
  ["phase", "Market phase"],
  ["next_event", "Next event"],
  ["gate", "Daily token gate"],
  ["feed", "Market-data feed"],
  ["kill_latched", "Kill switch latched"],
  ["halted", "Halted"],
  ["last_step_at", "Last engine step"],
  ["started_at", "Engine started"],
];

function row(label, value) {
  const tr = document.createElement("tr");
  const a = document.createElement("td");
  const b = document.createElement("td");
  a.textContent = label;
  b.textContent = value;
  tr.append(a, b);
  return tr;
}

async function refresh() {
  const table = document.getElementById("rows");
  try {
    const r = await fetch("/api/public/status", { method: "GET", cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const s = await r.json();
    table.replaceChildren(...FIELDS.map(([k, label]) => row(label, s[k] === undefined ? "—" : String(s[k]))));
    document.getElementById("labels").textContent = (s.labels || []).join(" · ");
  } catch (e) {
    table.replaceChildren(row("status", `engine unreachable (${e.message}); real money is OFF regardless`));
  }
}

refresh();
setInterval(refresh, 30000);
