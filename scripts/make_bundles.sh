#!/usr/bin/env bash
# Rebuild and package the demo: code bundle + data bundle, from /opt/firestarter.
set -euo pipefail
REPO=/opt/firestarter
DATA=${FIRESTARTER_DATA:-/opt/firestarter-data}
OUT=${1:-$HOME/bundles}
STAMP=$(date +%Y%m%d-%H%M)
mkdir -p "$OUT"

# 1. sync the edited inputs back into the repo (the repo is the source of truth)
for p in topology.xlsx services simulation compliance; do
  if [ -e "$DATA/$p" ]; then
    rm -rf "$REPO/data/$p"; cp -a "$DATA/$p" "$REPO/data/$p"
  else
    echo "skip $p (not in $DATA)"
  fi
done
rm -rf "$REPO/data/compliance/reports" "$REPO/data/app.db-shm" "$REPO/data/app.db-wal"

# 2. prove the code alone can rebuild the data: tests + bootstrap into a scratch root
cd "$REPO"
.venv/bin/python -m pytest -q
SCRATCH=$(mktemp -d); cp -a "$REPO/data/." "$SCRATCH/"
FIRESTARTER_DATA="$SCRATCH" FIRESTARTER_DEMO=1 .venv/bin/python scripts/bootstrap_demo.py

# 3. package
cd /opt
zip -qr "$OUT/firestarter-demo-$STAMP.zip" firestarter \
  -x 'firestarter/.venv/*' 'firestarter/.git/*' '*/__pycache__/*' \
     'firestarter/.pytest_cache/*' 'firestarter/.ruff_cache/*' 'firestarter/*.egg-info/*'
( cd "$SCRATCH" && zip -qr "$OUT/firestarter-data-demo-$STAMP.zip" . )
rm -rf "$SCRATCH"
ls -la "$OUT"
