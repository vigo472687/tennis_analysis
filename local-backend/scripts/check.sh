#!/usr/bin/env bash
# Backend quality gate (BE-028): wraps the V-B layered checks — full test
# suite, static checks, types, and the frozen contract hash verification.
# Fails (non-zero) on the first layer that reports a problem; layers that
# need real media/network are NOT part of this command and are not claimed.
set -uo pipefail
cd "$(dirname "$0")/.."

UV="${UV:-$HOME/.local/bin/uv}"
if ! command -v "$UV" >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
  else
    UV="uv"
  fi
fi

fail=0
run() {
  echo "== $* =="
  if ! "$@"; then
    echo "!! FAILED: $*"
    fail=1
  fi
  echo
}

# Layer 1: full automated test suite (unit, subprocess-stdio, boundaries)
run "$UV" run --frozen --offline --extra real-pose pytest tests

# Layer 2: static checks
run "$UV" run --frozen --offline ruff format --check src tests
run "$UV" run --frozen --offline ruff check src tests

# Layer 3: types (strict)
run "$UV" run --frozen --offline mypy --config-file pyproject.toml src

# Layer 4: frozen contract hashes (20 files, read-only)
run "$UV" run --frozen --offline python - <<'HASH_CHECK'
import hashlib
import json
from pathlib import Path

root = Path.cwd().parent
baseline = json.loads((root / "integration/reports/contract-baseline.json").read_text())
# files: {relative_path: sha256} (frozen baseline format)
files = baseline.get("files", {})
if not isinstance(files, dict):
    raise SystemExit("unexpected baseline format")
bad = 0
for rel, expected in files.items():
    digest = hashlib.sha256((root / rel).read_bytes()).hexdigest()
    if digest != expected:
        print(f"MISMATCH {rel}")
        bad += 1
print(f"contract hashes: {len(files) - bad}/{len(files)} OK")
raise SystemExit(1 if bad else 0)
HASH_CHECK

if [ "$fail" -ne 0 ]; then
  echo "check.sh: FAILED"
  exit 1
fi
echo "check.sh: all layers passed"
