#!/bin/bash
# Re-shoot docs/screenshots/v3.2 (docs/architecture/observability.md §17.5 v3.2, the cognitive connectome). Restarts the SIMULATED dashboard
# server at the simulated minute each live shot needs. No broker, no credentials: the server is the in-process simulator.
set -e
cd "$(dirname "$0")/../.."
OUT=$(realpath -m "${OUT:-docs/screenshots/v3.2}")
serve() {
  for p in $(ps -eo pid,args | awk '$2 ~ /python/ && /observability.dashboard/ {print $1}'); do kill "$p"; done
  sleep 1
  (.venv/bin/python -m project100c.observability.dashboard --speed 1 --join "$1" --port 8765 > /tmp/dash.log 2>&1 &)
  sleep 3
}
shoot() { (cd dashboard && node scripts/screenshots-v3.2.mjs http://127.0.0.1:8765 "$OUT" "$1"); }
serve 09:54:45 && shoot 02            # (ticks are 15 s bars) S-ORB-001's intent → Governor → order → fill at 09:55:00
shoot 01 && shoot 03                  # replay: a quiet bar sequence; S-VWAPC-001 rejected at 12:00
serve 09:55:30 && shoot 05 && shoot 06 && shoot 07
shoot 08 && shoot 09 && shoot 10
