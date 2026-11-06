#!/usr/bin/env bash
# Builds the route writer bundle for Lambda (python3.13, arm64) with awscrt included.
# Output: infra/terraform/.build/route-writer/ (zipped by Terraform).
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
out="$root/infra/terraform/.build/route-writer"
py="${PY:-$root/.venv/Scripts/python}"
[ -x "$py" ] || py="$root/.venv/bin/python"

stamp="$out/.deps-stamp"
want="boto3[crt]==1.43.*"
if [ ! -f "$stamp" ] || [ "$(cat "$stamp")" != "$want" ]; then
  rm -rf "$out"
  mkdir -p "$out"
  "$py" -m pip install -q --target "$out" --platform manylinux2014_aarch64 \
    --python-version 3.13 --implementation cp --only-binary=:all: "$want"
  # Not needed at runtime; keeps the bundle smaller.
  rm -rf "$out"/bin "$out"/*.dist-info/RECORD
  echo "$want" > "$stamp"
fi

mkdir -p "$out/traveler"
cp "$root/route_writer/handler.py" "$out/handler.py"
cp "$root/traveler/__init__.py" "$root/traveler/routing.py" "$out/traveler/"
echo "route writer bundle: $(du -sh "$out" | cut -f1)"
