#!/usr/bin/env bash
# Builds the watchdog bundle: its handler plus the traveler package it shares code with.
# Output: infra/terraform/.build/watchdog/ (zipped by Terraform). No third-party deps.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
out="$root/infra/terraform/.build/watchdog"
rm -rf "$out"
mkdir -p "$out"
cp "$root/watchdog/handler.py" "$out/handler.py"
cd "$root"
find traveler \( -name '*.py' -o -name '*.json' \) -not -path '*__pycache__*' | while read -r f; do
  mkdir -p "$out/$(dirname "$f")"
  cp "$f" "$out/$f"
done
echo "watchdog bundle: $(du -sh "$out" | cut -f1)"
