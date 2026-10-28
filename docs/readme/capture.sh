#!/usr/bin/env bash
# Re-shoot the current worktree's extension; no live Workflowy/browser session.
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
stage="${NEXUS_CAPTURE_STAGE:-/mnt/c/tmp/nexus-readme-001/stage}"
out="${NEXUS_CAPTURE_OUT:-/mnt/c/tmp/nexus-readme-001/shots}"
mkdir -p "$stage/h6/example" "$stage/src" "$out"
# Fresh code, never the old bake-off PNGs or Dan's real-data.js.
rm -rf -- "$stage/src/workflowy-glimpse-cache"
cp -R -- "$repo/workflowy-glimpse-cache" "$stage/src/workflowy-glimpse-cache"
for file in index.html fake-bridge.js harness.js capture.py example-data.js; do
  cp -- "$repo/docs/ui-bakeoff/fixture/$file" "$stage/h6/$file"
done
if [[ -f "$repo/docs/ui-bakeoff/fixture/example-jobs.js" ]]; then
  cp -- "$repo/docs/ui-bakeoff/fixture/example-jobs.js" "$stage/h6/example-jobs.js"
fi
cp -- "$repo/docs/readme/example/"*.json "$stage/h6/example/"
if [[ $# -eq 0 ]]; then
  set -- readme-hero readme-docks readme-slices readme-help readme-hero-dark readme-tip readme-rod readme-jobs readme-palette
fi
python3 "$stage/h6/capture.py" --scale 2 --budget 6000 --out "$out" --shots "$@"
for shot in "$@"; do cp -- "$out/$shot.png" "$repo/docs/readme/$shot.png"; done
